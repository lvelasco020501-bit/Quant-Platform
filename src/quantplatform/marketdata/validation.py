"""Turning venue frames into bars, and deciding which bars may be trusted.

Two independent jobs, kept in two classes because they fail for different reasons and at
different layers. :class:`CandleParser` asks whether a frame *is* a candle and whether that
candle is internally coherent. :class:`CandleSequenceValidator` asks whether the candle
belongs where it arrived, given everything already delivered.

**Nothing is repaired.** A malformed field is never coerced, an impossible price is never
clamped, and a missing candle is never synthesised. A bar invented to smooth over a hole is
indistinguishable downstream from a real one, and every feature, signal and fill computed
from it would be fiction wearing the costume of history.

**What raises and what does not.** Conditions that are ordinary traffic on a healthy stream
— a still-forming candle, a candle republished verbatim after a reconnect — are reported as
outcomes and counted, never raised: a feed that died on its venue's normal behaviour would
be useless. Conditions that mean the data is *wrong* — unparseable frames, impossible
values, a timestamp that moved backwards, a revision of a candle already acted on — raise a
domain error, because there is no correct way to continue past them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from quantplatform.core.enums import DataQualityIssue, MarketType, Timeframe
from quantplatform.core.errors import (
    DataIntegrityError,
    DataProviderError,
    MarketDataSubscriptionError,
    OutOfOrderDataError,
)
from quantplatform.core.models.market import MarketBar
from quantplatform.core.numeric import to_decimal
from quantplatform.core.timeutils import bar_close_time
from quantplatform.marketdata.clock import FeedClock
from quantplatform.marketdata.models import (
    CandleAdmission,
    CandleOutcome,
    GapReport,
)

__all__ = ["CandleParser", "CandleSequenceValidator"]

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

_KLINE_EVENT: Final[str] = "kline"
_KLINE_FIELD: Final[str] = "k"
_REQUIRED_KLINE_FIELDS: Final[tuple[str, ...]] = ("t", "T", "s", "i", "o", "h", "l", "c", "v", "x")

_VENUE_CLOSE_IS_INCLUSIVE_BY: Final[timedelta] = timedelta(milliseconds=1)
"""Binance reports a candle's close as the last millisecond it contains.

The platform's ``close_time`` is exclusive, so the two differ by exactly one millisecond.
The parser does not trust the venue's value at all — it derives the close from the open time
and the timeframe — but it checks the venue's against its own, because a mismatch means the
frame's interval is not the interval that was subscribed to.
"""


def _to_utc(milliseconds: int) -> datetime:
    """Convert a venue epoch-millisecond timestamp to a UTC instant.

    Built by addition rather than ``fromtimestamp`` so no binary float ever touches a
    timestamp that has to land exactly on a candle grid.

    Args:
        milliseconds: Epoch milliseconds.

    Returns:
        The timezone-aware UTC instant.
    """
    return _EPOCH + timedelta(milliseconds=milliseconds)


class CandleParser:
    """Reads one venue frame and returns the candle it carries, if it carries one."""

    def __init__(
        self,
        *,
        symbols: Mapping[str, str],
        timeframe: Timeframe,
        market_type: MarketType,
        source: str,
    ) -> None:
        """Create a parser bound to one subscription shape.

        Args:
            symbols: Venue symbol to canonical platform symbol, ``{"BTCUSDT": "BTC/USDT"}``.
            timeframe: The interval that was subscribed to.
            market_type: Market the stream belongs to.
            source: Provenance recorded on every produced bar.
        """
        self._symbols = dict(symbols)
        self._timeframe = timeframe
        self._market_type = market_type
        self._source = source

    @property
    def symbols(self) -> Mapping[str, str]:
        """Return the venue-to-platform symbol map in force."""
        return dict(self._symbols)

    def register(self, *, venue_symbol: str, symbol: str) -> None:
        """Teach the parser a symbol added to the subscription after construction."""
        self._symbols[venue_symbol] = symbol

    def forget(self, venue_symbol: str) -> None:
        """Drop a symbol removed from the subscription; unknown symbols are ignored."""
        self._symbols.pop(venue_symbol, None)

    def parse(self, text: str) -> MarketBar | None:
        """Return the candle a frame carries.

        Args:
            text: One raw text frame.

        Returns:
            The parsed bar, or ``None`` when the frame carries no candle — a subscription
            acknowledgement, a ping payload or any other event type. Those are ordinary and
            must not be errors.

        Raises:
            DataProviderError: If the frame is not valid JSON or not a JSON object.
            DataIntegrityError: If a candle is present but malformed, carries impossible
                values, or does not match the subscribed interval.
            MarketDataSubscriptionError: If the candle names an unsubscribed instrument.
        """
        payload = self._decode(text)
        kline = self._extract_kline(payload)
        if kline is None:
            return None
        return self._build_bar(kline)

    def _decode(self, text: str) -> dict[str, Any]:
        """Decode a frame into a JSON object.

        Raises:
            DataProviderError: If the text is not valid JSON or is not an object.
        """
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DataProviderError(
                "market-data frame is not valid JSON", frame_length=len(text)
            ) from exc
        if not isinstance(decoded, dict):
            raise DataProviderError(
                "market-data frame is not a JSON object", frame_type=type(decoded).__name__
            )
        return decoded

    def _extract_kline(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return the candle object inside a frame, unwrapping the combined-stream envelope.

        Returns:
            The kline mapping, or ``None`` when the frame is not a candle event.

        Raises:
            DataIntegrityError: If a kline event carries a non-object candle field.
        """
        inner = payload.get("data")
        body = inner if isinstance(inner, dict) else payload
        if body.get("e") not in (_KLINE_EVENT, None):
            return None
        kline = body.get(_KLINE_FIELD)
        if kline is None:
            return None
        if not isinstance(kline, dict):
            raise DataIntegrityError(
                "candle field is not an object", field_type=type(kline).__name__
            )
        return kline

    def _build_bar(self, kline: Mapping[str, Any]) -> MarketBar:
        """Build a validated bar from a candle mapping.

        Raises:
            DataIntegrityError: If fields are missing, unparseable, inconsistent with the
                subscribed interval, or describe an impossible candle.
            MarketDataSubscriptionError: If the instrument was never subscribed to.
        """
        missing = [field for field in _REQUIRED_KLINE_FIELDS if field not in kline]
        if missing:
            raise DataIntegrityError(
                "candle is missing required fields",
                missing=sorted(missing),
                issue=DataQualityIssue.MALFORMED_RECORD.value,
            )

        venue_symbol = kline["s"]
        symbol = self._symbols.get(str(venue_symbol))
        if symbol is None:
            raise MarketDataSubscriptionError(
                "received a candle for an instrument that was never subscribed",
                venue_symbol=str(venue_symbol),
                subscribed=sorted(self._symbols),
            )

        interval = str(kline["i"])
        if interval != self._timeframe.value:
            raise DataIntegrityError(
                "candle interval does not match the subscription",
                expected=self._timeframe.value,
                received=interval,
                issue=DataQualityIssue.UNEXPECTED_TIMEFRAME.value,
            )

        open_time = self._timestamp(kline, "t")
        close_time = bar_close_time(open_time, self._timeframe)
        self._check_venue_close(kline, close_time)

        is_closed = kline["x"]
        if not isinstance(is_closed, bool):
            raise DataIntegrityError(
                "candle closed flag is not a boolean", received=type(is_closed).__name__
            )

        try:
            return MarketBar(
                symbol=symbol,
                market_type=self._market_type,
                timeframe=self._timeframe,
                open_time=open_time,
                close_time=close_time,
                open=self._decimal(kline, "o"),
                high=self._decimal(kline, "h"),
                low=self._decimal(kline, "l"),
                close=self._decimal(kline, "c"),
                volume=self._decimal(kline, "v"),
                quote_volume=self._optional_decimal(kline, "q"),
                trade_count=self._optional_count(kline, "n"),
                source=self._source,
                is_closed=is_closed,
            )
        except ValidationError as exc:
            # Impossible prices, negative volume, an inverted high/low and a candle whose
            # duration contradicts its timeframe all surface here. They are integrity
            # failures of the venue's data, not programming errors, so they are re-raised
            # as domain errors rather than leaking pydantic's exception upwards.
            raise DataIntegrityError(
                "candle failed domain validation",
                symbol=symbol,
                open_time=open_time.isoformat(),
                errors=[error["msg"] for error in exc.errors()],
                issue=DataQualityIssue.INVALID_OHLC.value,
            ) from exc

    def _check_venue_close(self, kline: Mapping[str, Any], close_time: datetime) -> None:
        """Check the venue's own close timestamp against the derived one.

        Raises:
            DataIntegrityError: If the venue's candle does not span the subscribed interval.
        """
        venue_close = self._timestamp(kline, "T")
        if venue_close + _VENUE_CLOSE_IS_INCLUSIVE_BY != close_time:
            raise DataIntegrityError(
                "candle duration does not match the subscribed timeframe",
                expected_close=close_time.isoformat(),
                venue_close=venue_close.isoformat(),
                timeframe=self._timeframe.value,
                issue=DataQualityIssue.UNEXPECTED_TIMEFRAME.value,
            )

    def _timestamp(self, kline: Mapping[str, Any], field: str) -> datetime:
        """Read an epoch-millisecond field.

        Raises:
            DataIntegrityError: If the value is not an integer number of milliseconds.
        """
        value = kline[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise DataIntegrityError(
                "candle timestamp is not an integer",
                field=field,
                received=type(value).__name__,
                issue=DataQualityIssue.MALFORMED_RECORD.value,
            )
        return _to_utc(value)

    def _decimal(self, kline: Mapping[str, Any], field: str) -> Decimal:
        """Read a required decimal field.

        Raises:
            DataIntegrityError: If the value cannot be represented exactly as a decimal.
        """
        value = kline[field]
        try:
            return to_decimal(value)
        except (ValueError, TypeError) as exc:
            raise DataIntegrityError(
                "candle value is not a valid decimal",
                field=field,
                received=repr(value),
                issue=DataQualityIssue.MALFORMED_RECORD.value,
            ) from exc

    def _optional_decimal(self, kline: Mapping[str, Any], field: str) -> Decimal | None:
        """Read an optional decimal field, returning ``None`` when it is absent."""
        if field not in kline or kline[field] is None:
            return None
        return self._decimal(kline, field)

    def _optional_count(self, kline: Mapping[str, Any], field: str) -> int | None:
        """Read an optional non-negative integer field.

        Raises:
            DataIntegrityError: If present but not a non-negative integer.
        """
        if field not in kline or kline[field] is None:
            return None
        value: object = kline[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DataIntegrityError(
                "candle trade count is not a non-negative integer",
                field=field,
                received=repr(value),
                issue=DataQualityIssue.MALFORMED_RECORD.value,
            )
        return value


class CandleSequenceValidator:
    """Decides whether a parsed candle belongs where it arrived.

    Holds the last accepted candle per symbol. That single piece of state is what makes
    reconnection safe: the stream that comes back is checked against the stream that went
    away, so a venue that replays the last candle is suppressed, one that resumes further
    ahead is caught as a gap, and one that rewinds is refused outright.
    """

    def __init__(self, *, timeframe: Timeframe, clock: FeedClock) -> None:
        """Create a validator.

        Args:
            timeframe: The subscribed interval, used to measure the size of a gap.
            clock: Feed clock, consulted to confirm a candle's interval has elapsed.
        """
        self._timeframe = timeframe
        self._clock = clock
        self._last: dict[str, MarketBar] = {}

    def last_accepted(self, symbol: str) -> MarketBar | None:
        """Return the most recent candle delivered for a symbol, if any."""
        return self._last.get(symbol)

    @property
    def tracked_symbols(self) -> tuple[str, ...]:
        """Return every symbol with an accepted candle, in sorted order."""
        return tuple(sorted(self._last))

    def resynchronize(self, symbol: str | None = None) -> None:
        """Forget continuity state so the next candle starts a fresh series.

        The deliberate escape hatch from a detected gap. It is a separate, explicit call
        because accepting a discontinuity is a decision with consequences — the bars before
        and after the hole are no longer one series — and nothing should be able to make it
        implicitly.

        Args:
            symbol: Symbol to reset, or ``None`` to reset every symbol.
        """
        if symbol is None:
            self._last.clear()
        else:
            self._last.pop(symbol, None)

    def admit(self, bar: MarketBar) -> CandleAdmission:
        """Decide what to do with one parsed candle.

        Args:
            bar: The candle to judge.

        Returns:
            The verdict. Only :attr:`~quantplatform.marketdata.models.CandleOutcome.ACCEPTED`
            may reach the pipeline, and only then is the candle recorded as the new
            continuity anchor.

        Raises:
            OutOfOrderDataError: If the candle predates the last one accepted.
            DataIntegrityError: If it revises a candle already delivered, or overlaps one.
        """
        if not self._is_final(bar):
            return CandleAdmission(outcome=CandleOutcome.FORMING, bar=bar)

        last = self._last.get(bar.symbol)
        if last is None:
            return self._accept(bar)

        if bar.open_time == last.open_time:
            return self._judge_repeat(bar, last)
        if bar.open_time < last.open_time:
            raise OutOfOrderDataError(
                "candle predates the last one accepted",
                symbol=bar.symbol,
                received_open_time=bar.open_time.isoformat(),
                last_open_time=last.open_time.isoformat(),
                issue=DataQualityIssue.OUT_OF_ORDER_BAR.value,
            )
        if bar.open_time < last.close_time:
            raise DataIntegrityError(
                "candle overlaps the last one accepted",
                symbol=bar.symbol,
                received_open_time=bar.open_time.isoformat(),
                last_close_time=last.close_time.isoformat(),
            )
        if bar.open_time > last.close_time:
            return self._report_gap(bar, last)
        return self._accept(bar)

    def _is_final(self, bar: MarketBar) -> bool:
        """Return whether the candle is finished at the venue *and* on our clock.

        Both must agree. The venue's flag alone would trust a provider that mislabels a
        forming candle; the clock alone would trust an aggregation window we cannot see
        inside.
        """
        return bar.is_closed and self._clock.is_bar_final(bar)

    def _judge_repeat(self, bar: MarketBar, last: MarketBar) -> CandleAdmission:
        """Classify a candle bearing an already-seen open time.

        Raises:
            DataIntegrityError: If the repeat differs from what was delivered. A venue
                revising a candle the pipeline has already traded on cannot be reconciled
                by replacing it: the decisions it produced have already happened.
        """
        if self._same_candle(bar, last):
            return CandleAdmission(outcome=CandleOutcome.DUPLICATE, bar=bar)
        raise DataIntegrityError(
            "venue revised a candle that was already delivered",
            symbol=bar.symbol,
            open_time=bar.open_time.isoformat(),
            issue=DataQualityIssue.REVISED_BAR.value,
        )

    def _report_gap(self, bar: MarketBar, last: MarketBar) -> CandleAdmission:
        """Describe the hole between the last accepted candle and this one."""
        missing_seconds = (bar.open_time - last.close_time).total_seconds()
        missing_bars = int(missing_seconds // self._timeframe.seconds)
        report = GapReport(
            symbol=bar.symbol,
            timeframe=self._timeframe,
            expected_open_time=last.close_time,
            received_open_time=bar.open_time,
            missing_bars=missing_bars,
            detected_at=self._clock.now(),
        )
        return CandleAdmission(outcome=CandleOutcome.GAP, bar=bar, gap=report)

    def _accept(self, bar: MarketBar) -> CandleAdmission:
        """Record a candle as the new continuity anchor and admit it."""
        self._last[bar.symbol] = bar
        return CandleAdmission(outcome=CandleOutcome.ACCEPTED, bar=bar)

    @staticmethod
    def _same_candle(left: MarketBar, right: MarketBar) -> bool:
        """Return whether two candles carry identical market values.

        Compares only what the market printed. Provenance and the closed flag are excluded
        deliberately: a candle republished after a reconnect may legitimately carry a
        different source tag, and it is the prices and volume that a strategy acted on.
        """
        return (
            left.open == right.open
            and left.high == right.high
            and left.low == right.low
            and left.close == right.close
            and left.volume == right.volume
        )
