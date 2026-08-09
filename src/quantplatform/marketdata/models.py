"""Value objects the live feed produces alongside the bars themselves.

A feed that only returned bars would be untrustworthy in exactly the way that matters: an
operator could not tell a quiet market from a broken socket, or a clean run from one that
silently dropped forty candles. These types are what make the feed's own behaviour
observable — what it received, what it refused, and why.

Nothing here describes an order, a balance or an account. The market-data layer has no
vocabulary for those, which is what makes "read-only" a structural property rather than a
promise.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from quantplatform.core.enums import DataQualityIssue, Timeframe
from quantplatform.core.models.market import MarketBar

__all__ = [
    "CandleAdmission",
    "CandleOutcome",
    "FeedMetrics",
    "GapReport",
    "RejectedCandle",
    "StreamSubscription",
]


class CandleOutcome(StrEnum):
    """What the sequence rules decided about one parsed candle.

    Only conditions that are *normal traffic on a healthy stream* appear here. Anything
    that indicates the data itself is wrong — malformed fields, impossible prices, a
    timestamp that went backwards, a revision of a candle already acted on — raises a
    domain error instead, because continuing past those would mean trading on a series the
    venue never published.
    """

    ACCEPTED = "accepted"
    """A new closed candle, in sequence. The only outcome that reaches the pipeline."""

    FORMING = "forming"
    """The candle is still open. Every venue sends these continuously; they are counted
    and discarded, never delivered."""

    DUPLICATE = "duplicate"
    """A closed candle already accepted, republished byte-for-byte. Expected after a
    reconnect, when the venue resends the most recent candle."""

    GAP = "gap"
    """The candle is newer than the next one expected: at least one candle is missing."""


@dataclass(frozen=True, slots=True)
class StreamSubscription:
    """One instrument's live candle subscription."""

    symbol: str
    """Canonical platform symbol, ``BTC/USDT``."""

    venue_symbol: str
    """The venue's spelling, ``BTCUSDT``."""

    stream_name: str
    """The venue stream identifier, ``btcusdt@kline_1h``."""

    timeframe: Timeframe


@dataclass(frozen=True, slots=True)
class GapReport:
    """A break in the candle series, described precisely enough to act on.

    Deliberately carries no repaired data. The platform never synthesises a candle to
    bridge a hole: an invented bar is indistinguishable downstream from a real one, and
    every feature, signal and fill computed from it would be fiction that looks like
    history.
    """

    symbol: str
    timeframe: Timeframe
    expected_open_time: datetime
    """The open time the next candle should have carried."""

    received_open_time: datetime
    """The open time that actually arrived, later than expected."""

    missing_bars: int
    """How many candle intervals fall in the hole."""

    detected_at: datetime
    """When the break was observed, from the injected clock."""

    @property
    def issue(self) -> DataQualityIssue:
        """Return the data-quality issue this report represents."""
        return DataQualityIssue.MISSING_BAR

    def describe(self) -> str:
        """Return a one-line human-readable summary for logs and error messages."""
        return (
            f"{self.missing_bars} missing {self.timeframe.value} candle(s) for {self.symbol}: "
            f"expected {self.expected_open_time.isoformat()}, "
            f"received {self.received_open_time.isoformat()}"
        )


@dataclass(frozen=True, slots=True)
class RejectedCandle:
    """A candle the feed refused to deliver, kept for the audit trail."""

    symbol: str
    open_time: datetime
    outcome: CandleOutcome
    issue: DataQualityIssue | None
    """The catalogued data-quality issue, when the rejection maps to one."""


@dataclass(frozen=True, slots=True)
class CandleAdmission:
    """The sequence rules' verdict on one parsed candle."""

    outcome: CandleOutcome
    bar: MarketBar
    gap: GapReport | None = None
    """Present exactly when ``outcome`` is :attr:`CandleOutcome.GAP`."""

    @property
    def is_deliverable(self) -> bool:
        """Return whether this candle may be handed to the trading pipeline."""
        return self.outcome is CandleOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class FeedMetrics:
    """Counters describing what the feed did, separate from what the market did.

    Kept apart from trading performance for the same reason paper trading keeps runtime
    metrics apart from returns: a run that reconnected eleven times and suppressed two
    hundred duplicates may have produced a perfectly ordinary equity curve, and the curve
    would never say so.
    """

    frames_received: int = 0
    """Text frames read from the transport, including control and non-candle frames."""

    control_frames: int = 0
    """Frames that carried no candle, such as subscription acknowledgements."""

    candles_parsed: int = 0
    bars_emitted: int = 0
    forming_suppressed: int = 0
    duplicates_suppressed: int = 0
    gaps_detected: int = 0
    heartbeat_timeouts: int = 0
    reconnects: int = 0
    connection_attempts: int = 0
    subscriptions_sent: int = 0

    def record(self, **deltas: int) -> FeedMetrics:
        """Return a copy with the named counters incremented.

        Args:
            **deltas: Counter name to increment amount.

        Returns:
            A new instance; the metrics object is never mutated in place.

        Raises:
            ValueError: If a name does not match a counter.
        """
        updates: dict[str, int] = {}
        for name, delta in deltas.items():
            current = getattr(self, name, None)
            if not isinstance(current, int):
                msg = f"unknown feed metric {name!r}"
                raise ValueError(msg)
            updates[name] = current + delta
        return replace(self, **updates)

    @property
    def acceptance_rate(self) -> Decimal | None:
        """Return the share of parsed candles that reached the pipeline.

        Returns:
            The ratio, or ``None`` before any candle has been parsed — an undefined rate
            is reported as undefined rather than as zero, which would read as "everything
            was rejected".
        """
        if self.candles_parsed == 0:
            return None
        return Decimal(self.bars_emitted) / Decimal(self.candles_parsed)

    @property
    def is_clean(self) -> bool:
        """Return whether the feed ran without a gap, a reconnect or a heartbeat loss."""
        return self.gaps_detected == 0 and self.reconnects == 0 and self.heartbeat_timeouts == 0

    @property
    def total_suppressed(self) -> int:
        """Return how many parsed candles were refused for any reason."""
        return self.forming_suppressed + self.duplicates_suppressed
