"""Restoring a session's market context, and the record that it happened.

**Warm-start restores market context. It never restores an account.** That is not a
convention this module asks callers to respect — it is what the types here make possible:
:class:`MarketHistory` holds bars and identifying metadata, and a
:class:`~quantplatform.core.models.market.MarketBar` has no field capable of expressing a
balance, a position, an order, a fill, a fee or a breaker. There is no shape for money to
travel in.

The distinction from ``resume`` is total. Resume restores an *account* and is refused after
any financial mutation, because the portfolio engine is flat-start and a resumed process
would silently trade a rebuilt account instead of the real one. Warm-start restores a
*window of candles* so the strategy is not blind for its whole warm-up period, and it starts
flat with the capital configuration declares — every time, with no exceptions to arrange.
"""

from __future__ import annotations

import hashlib
from typing import Self

from pydantic import Field, model_validator

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.base import DomainModel, Text, UtcDatetime
from quantplatform.core.models.market import MarketBar

__all__ = [
    "MarketHistory",
    "MarketHistoryManifest",
    "WarmStartRecord",
    "WarmStartStatus",
    "history_digest",
]


def history_digest(bars: tuple[MarketBar, ...]) -> str:
    """Return a stable digest of exactly these bars, in this order.

    Built from each bar's identifying fields and its prices rather than from a serialisation
    of the whole model, so the digest answers "is this the same market history?" and does
    not change when an unrelated field is added to the model. Order is part of the identity:
    the same candles in a different order are a different history, and for a recursive
    indicator they are a different answer.
    """
    engine = hashlib.sha256()
    for bar in bars:
        engine.update(
            "|".join(
                (
                    bar.symbol,
                    bar.market_type.value,
                    bar.timeframe.value,
                    bar.open_time.isoformat(),
                    bar.close_time.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                )
            ).encode()
        )
        engine.update(b"\n")
    return engine.hexdigest()


class MarketHistoryManifest(DomainModel):
    """What a persisted history file claims to be, written once before any bar.

    Every field here exists to make loading the wrong file impossible rather than unlikely.
    A history is bound to the session that wrote it and to the instrument it describes, and
    a loader that finds any of them disagreeing with its own configuration refuses instead
    of proceeding with candles from somewhere else.
    """

    source_session_id: Text
    symbol: Text
    market_type: MarketType
    timeframe: Timeframe
    created_at: UtcDatetime


class MarketHistory(DomainModel):
    """A validated window of candles, bound to the session and instrument it came from.

    Constructing one is what proves the file was coherent: the manifest and the bars have to
    agree with each other, the bars have to be closed, strictly ordered and contiguous, and
    the derived summary has to match what is actually held. A caller receiving this instance
    is receiving a history that has already been refused if it was wrong.
    """

    manifest: MarketHistoryManifest
    bars: tuple[MarketBar, ...] = Field(min_length=1)
    bars_count: int = Field(ge=1)
    first_bar_close_time: UtcDatetime
    last_bar_close_time: UtcDatetime
    digest: Text

    @model_validator(mode="after")
    def _validate_summary(self) -> Self:
        """Check the file's own summary describes the bars it actually holds.

        Separate from the sequence check because it answers a different question: not "are
        these candles coherent" but "is this file telling the truth about itself". A count
        or a digest that disagrees with the content means the file was truncated, padded or
        altered, and none of those are worth reading further.

        Raises:
            ValueError: If the count, the endpoints or the digest disagree with the bars.
        """
        if self.bars_count != len(self.bars):
            msg = (
                f"the history claims {self.bars_count} bars but holds {len(self.bars)}; a "
                "count that disagrees with the content is a truncated or padded file"
            )
            raise ValueError(msg)
        if self.first_bar_close_time != self.bars[0].close_time:
            msg = "the history's first bar is not the one it names"
            raise ValueError(msg)
        if self.last_bar_close_time != self.bars[-1].close_time:
            msg = "the history's last bar is not the one it names"
            raise ValueError(msg)
        actual = history_digest(self.bars)
        if actual != self.digest:
            msg = (
                "the history's digest does not match its content; the file was altered or "
                "written by something that does not agree on what the bars are"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_sequence(self) -> Self:
        """Check the bars form one contiguous, strictly ordered window of one instrument.

        Raises:
            ValueError: If a candle is unclosed, belongs to another instrument, repeats or
                goes backwards in time, is missing, or has a different duration from its
                neighbours.
        """
        manifest = self.manifest
        expected_span = self.bars[0].close_time - self.bars[0].open_time
        previous: MarketBar | None = None
        for bar in self.bars:
            if not bar.is_closed:
                msg = (
                    f"the history holds an unclosed candle at {bar.close_time.isoformat()}; a "
                    "candle still forming is not a fact and cannot seed an indicator"
                )
                raise ValueError(msg)
            if bar.symbol != manifest.symbol:
                msg = f"the history holds {bar.symbol!r} but claims to describe {manifest.symbol!r}"
                raise ValueError(msg)
            if bar.market_type is not manifest.market_type:
                msg = "the history holds a bar of a market type its manifest does not name"
                raise ValueError(msg)
            if bar.timeframe is not manifest.timeframe:
                msg = "the history holds a bar of a timeframe its manifest does not name"
                raise ValueError(msg)
            if previous is not None:
                if bar.close_time <= previous.close_time:
                    msg = (
                        "the history is not strictly ordered by close time; a recursive "
                        "indicator reads it forwards and out-of-order candles are a "
                        "different answer, not an untidy one"
                    )
                    raise ValueError(msg)
                if bar.open_time != previous.close_time:
                    missing = bar.open_time - previous.close_time
                    msg = (
                        f"the history has a gap of {missing} before "
                        f"{bar.close_time.isoformat()}; the platform never fills a gap, and "
                        "an indicator computed across one is silently wrong"
                    )
                    raise ValueError(msg)
                if bar.close_time - bar.open_time != expected_span:
                    msg = "the history holds candles of differing durations"
                    raise ValueError(msg)
            previous = bar
        return self


class WarmStartStatus(DomainModel):
    """Why a session did or did not start with its market context restored.

    Modelled rather than reduced to a boolean because the three outcomes are genuinely
    different and an operator needs to tell them apart: applied, deliberately not used, and
    refused. Collapsing the last two would report a rejected history and a fresh deployment
    with no history as the same thing.
    """

    applied: bool
    reason: Text
    """Plain language. On a refusal this names the condition that failed and its values."""


class WarmStartRecord(DomainModel):
    """Audit of a warm-start, persisted alongside the session's own state.

    **Describes; never authorises.** A snapshot carrying this does not become resumable,
    does not relax any check, and does not by itself restore anything — it is evidence that
    a start-up happened a particular way, kept so an operator can tell a session that
    continued its market context from one that began blind.
    """

    applied_at: UtcDatetime
    source_session_id: Text
    symbol: Text
    market_type: MarketType
    timeframe: Timeframe
    bars_loaded: int = Field(ge=1)
    required_history: int = Field(ge=1)
    first_bar_close_time: UtcDatetime
    last_bar_close_time: UtcDatetime
    digest: Text
    first_live_bar_close_time: UtcDatetime | None = None
    """Set once the first live candle has been accepted, which is when the seam is proven.
    ``None`` means warm-start was applied but no live candle has arrived yet."""

    financial_state_restored: bool = False
    """Always ``False``. Persisted as an explicit assertion rather than left implicit, so a
    reader of the record does not have to take the contract on trust."""

    @model_validator(mode="after")
    def _validate_record(self) -> Self:
        """Refuse a record that claims warm-start restored money.

        Raises:
            ValueError: If ``financial_state_restored`` is true, which the contract makes
                impossible and which therefore indicates a corrupt or forged record.
        """
        if self.financial_state_restored:
            msg = (
                "a warm-start record cannot claim financial state was restored: warm-start "
                "carries market candles only, and a record asserting otherwise describes "
                "something this platform cannot do"
            )
            raise ValueError(msg)
        return self
