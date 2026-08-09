"""The immutable shape of a daily report.

Every model here is frozen and forbids unknown fields, for the same reason the trading
domain models are: a report is a record of what a day *was*, and a record that can be edited
after the fact is not evidence of anything.

**A metric that could not be computed is ``None``, never zero.** A Sharpe ratio of zero on a
day with two bars looks like an answer; ``None`` says there wasn't one. This follows the rule
already set by :class:`~quantplatform.backtesting.metrics.PerformanceSummary`, and the daily
figures are computed through the same function so the two can never drift apart.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from quantplatform.core.enums import AlertSeverity, OrderSide
from quantplatform.core.models.base import DomainModel, Symbol, Text, UtcDatetime
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.core.numeric import Fee, Money, NonNegativeMoney

__all__ = [
    "Alert",
    "AlertCode",
    "DailyAlerts",
    "DailyComparison",
    "DailyHealth",
    "DailyReport",
    "DailySeries",
    "DailyStatistics",
    "DailySummary",
    "FeedDiagnostics",
    "HealthCheck",
    "HealthCheckName",
    "HealthLevel",
    "RoundTrip",
    "SeriesPoint",
]


class HealthLevel(StrEnum):
    """How worried an operator should be."""

    GREEN = "green"
    """Nothing observed outside its threshold."""

    YELLOW = "yellow"
    """Something is outside its threshold and should be looked at before the next session."""

    RED = "red"
    """Something is far enough outside its threshold that the session's output is suspect."""

    @property
    def rank(self) -> int:
        """Return a sortable severity, higher being worse."""
        return _HEALTH_RANK[self]

    @classmethod
    def worst(cls, levels: tuple[HealthLevel, ...]) -> HealthLevel:
        """Return the most severe level in a group, green when the group is empty.

        A day on which no check could be evaluated is reported green rather than unknown:
        every check has an explicit skip path, so an empty group means nothing applied.
        """
        return max(levels, key=lambda level: level.rank, default=cls.GREEN)


_HEALTH_RANK: dict[HealthLevel, int] = {
    HealthLevel.GREEN: 0,
    HealthLevel.YELLOW: 1,
    HealthLevel.RED: 2,
}


class HealthCheckName(StrEnum):
    """The operational questions a daily report answers."""

    FEED_STABILITY = "feed_stability"
    GAP_COUNT = "gap_count"
    HEARTBEAT_FAILURES = "heartbeat_failures"
    RUNTIME_EXCEPTIONS = "runtime_exceptions"
    RECONNECTS = "reconnects"
    ORDER_REJECTION_RATIO = "order_rejection_ratio"
    MISSING_BARS = "missing_bars"
    SESSION_INTERRUPTIONS = "session_interruptions"
    CLOCK_DRIFT = "clock_drift"


_FEED_CHECKS: frozenset[HealthCheckName] = frozenset(
    {
        HealthCheckName.FEED_STABILITY,
        HealthCheckName.GAP_COUNT,
        HealthCheckName.HEARTBEAT_FAILURES,
        HealthCheckName.RECONNECTS,
        HealthCheckName.MISSING_BARS,
    }
)
"""The checks that describe the data stream rather than the process around it."""


class AlertCode(StrEnum):
    """Conditions a day can raise."""

    DRAWDOWN_EXCEEDED = "drawdown_exceeded"
    LARGE_LOSS = "large_loss"
    GAP_DETECTED = "gap_detected"
    MULTIPLE_RECONNECTS = "multiple_reconnects"
    MISSING_DATA = "missing_data"
    RISK_REJECTION_SPIKE = "risk_rejection_spike"
    BROKER_REJECTION_SPIKE = "broker_rejection_spike"
    RUNTIME_EXCEPTION = "runtime_exception"
    LOW_ACCEPTANCE_RATE = "low_acceptance_rate"
    ABNORMAL_SLIPPAGE = "abnormal_slippage"
    ABNORMAL_COMMISSION = "abnormal_commission"


class FeedDiagnostics(DomainModel):
    """What the feed and the process did, as observed from outside the session.

    Reaches a report from outside the session, because the session deliberately cannot see
    its data source — that is the property Phase 7A exists to preserve. The feed's own
    counters arrive as a :class:`~quantplatform.core.models.telemetry.FeedMetricsSnapshot`,
    a neutral core type, and :meth:`from_feed_metrics` maps them here. Reporting never
    imports the market-data package, so nothing in this file knows what a WebSocket is.

    ``out_of_order_candles`` and ``unknown_symbols`` still have no counterpart in the feed's
    counters: the feed *raises* on those rather than counting them, so a non-zero value here
    was recorded by an error handler further out. Same for ``runtime_exceptions``,
    ``session_interruptions`` and ``broker_rejections``, which describe the process and the
    venue adapter rather than the stream.
    """

    reconnects: int = Field(default=0, ge=0)
    gaps_detected: int = Field(default=0, ge=0)
    heartbeat_failures: int = Field(default=0, ge=0)
    duplicate_candles: int = Field(default=0, ge=0)
    out_of_order_candles: int = Field(default=0, ge=0)
    unknown_symbols: int = Field(default=0, ge=0)
    missing_bars: int = Field(default=0, ge=0)
    runtime_exceptions: int = Field(default=0, ge=0)
    session_interruptions: int = Field(default=0, ge=0)
    broker_rejections: int = Field(default=0, ge=0)
    """Orders the venue adapter refused, as opposed to those risk declined to send."""

    rejected_frames: int = Field(default=0, ge=0)
    """Frames carrying data the feed refused, malformed ones included."""

    malformed_frames: int = Field(default=0, ge=0)
    """Frames that could not be parsed, or whose candle failed validation."""

    candles_received: int = Field(default=0, ge=0)
    candles_accepted: int = Field(default=0, ge=0)
    candles_rejected: int = Field(default=0, ge=0)

    clock_drift_seconds: Money | None = None
    """Measured local-versus-venue skew, or ``None`` when nothing measured it.

    Nothing in the platform measures this yet, so it is normally ``None`` and the
    corresponding health check reports itself skipped rather than passing.
    """

    @classmethod
    def from_feed_metrics(
        cls, snapshot: FeedMetricsSnapshot, **observed: object
    ) -> FeedDiagnostics:
        """Build diagnostics from a feed's own reading of itself.

        Every field the feed measures is taken from the snapshot rather than defaulted, so
        a day with reconnects can no longer report zero of them and pass as healthy. The
        fields the feed cannot measure — out-of-order candles, unknown symbols, runtime
        exceptions, interruptions, broker rejections, clock drift — stay at their defaults
        unless a caller supplies them, because there is nothing to read them from.

        Args:
            snapshot: The feed's counters.
            **observed: Fields the feed cannot measure, supplied by whoever can.

        Returns:
            Diagnostics carrying the feed's real numbers.
        """
        return cls(
            reconnects=snapshot.reconnect_count,
            gaps_detected=snapshot.detected_gaps,
            heartbeat_failures=snapshot.heartbeat_timeouts,
            duplicate_candles=snapshot.duplicate_candles,
            rejected_frames=snapshot.rejected_frames,
            malformed_frames=snapshot.malformed_frames,
            candles_received=snapshot.candles_received,
            candles_accepted=snapshot.candles_accepted,
            candles_rejected=snapshot.candles_rejected,
            **observed,  # type: ignore[arg-type]
        )

    @property
    def feed_acceptance_rate(self) -> Decimal | None:
        """Return the share of parsed candles the feed delivered, or ``None`` if it saw none."""
        if self.candles_received == 0:
            return None
        return Decimal(self.candles_accepted) / Decimal(self.candles_received)


class RoundTrip(DomainModel):
    """One position lifecycle that ended: opened, then reduced back to flat.

    A fill is not a trade. An entry still open has no outcome to be right or wrong about,
    and counting it would move the win rate every time a position was merely scaled into.
    A round trip is attributed to the day it *closed*, so a position opened on Monday and
    sold on Tuesday belongs to Tuesday.
    """

    symbol: Symbol
    side: OrderSide
    """Direction of the opening fills. Always ``BUY`` on spot, which cannot go short."""

    opened_at: UtcDatetime
    closed_at: UtcDatetime
    quantity: NonNegativeMoney
    entry_price: Money
    exit_price: Money
    gross_pnl: Money
    fees: Fee
    net_pnl: Money

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the lifecycle runs forwards.

        Raises:
            ValueError: If the trade closed before it opened.
        """
        if self.closed_at < self.opened_at:
            msg = "a round trip cannot close before it opens"
            raise ValueError(msg)
        return self

    @property
    def holding_seconds(self) -> int:
        """Return how long the position was held."""
        return int((self.closed_at - self.opened_at).total_seconds())

    @property
    def is_win(self) -> bool:
        """Return whether the trade closed net positive after fees."""
        return self.net_pnl > 0


class HealthCheck(DomainModel):
    """One operational question and its answer."""

    name: HealthCheckName
    level: HealthLevel
    message: Text
    observed: Money | None = None
    threshold: Money | None = None
    skipped: bool = False
    """True when nothing measured the input, so the check neither passed nor failed."""


class Alert(DomainModel):
    """Something about the day worth saying out loud."""

    code: AlertCode
    severity: AlertSeverity
    message: Text
    observed: Money | None = None
    threshold: Money | None = None


class DailyStatistics(DomainModel):
    """Everything countable about one day.

    Trading figures and operational figures sit side by side but never blend: a day can be
    flat and profitable while dropping every third candle, and a single health-adjusted
    return number would hide exactly that.

    **Not every counter can be day-scoped, and the difference is stated rather than
    smoothed over.** Account, trade, cost and order-flow figures are computed from the
    day's own bars and fills. Three of the process counters cannot be: the session records
    a rejected bar without attributing it to a day, so ``bars_rejected``,
    ``acceptance_rate``, ``session_interruptions`` and ``report_failures`` are
    session-cumulative as of the rollover. The feed counters — reconnects, gaps, heartbeat
    failures, duplicate and out-of-order candles, unknown symbols, missing bars, runtime
    exceptions — cover whatever window the caller measured when it supplied
    :class:`FeedDiagnostics`. Each is labelled below; inventing day-scoped values for them
    would be more comfortable to read and less true.
    """

    # --- Account ---------------------------------------------------------------------------
    opening_equity: Money
    daily_equity: Money
    daily_pnl: Money
    daily_return: Money | None = None

    # --- Trades ----------------------------------------------------------------------------
    trade_count: int = Field(default=0, ge=0)
    long_trades: int = Field(default=0, ge=0)
    short_trades: int = Field(default=0, ge=0)
    """Structurally zero while the platform is spot-only; reported so the shape survives."""

    winning_trades: int = Field(default=0, ge=0)
    losing_trades: int = Field(default=0, ge=0)
    win_rate: Money | None = None
    average_win: Money | None = None
    average_loss: Money | None = None
    profit_factor: Money | None = None
    expectancy: Money | None = None
    largest_winner: Money | None = None
    largest_loser: Money | None = None
    average_position_size: Money | None = None
    average_holding_seconds: Money | None = None
    exposure_utilization: Money | None = None
    """Marked position value over equity at the close of the day."""

    # --- Risk-adjusted ---------------------------------------------------------------------
    max_drawdown: Money = Decimal(0)
    """Deepest intraday decline from the day's own running peak, as a fraction."""

    sharpe_ratio: Money | None = None
    sortino_ratio: Money | None = None

    # --- Costs -----------------------------------------------------------------------------
    commission_paid: Fee = Decimal(0)
    slippage_paid: Money = Decimal(0)
    traded_notional: NonNegativeMoney = Decimal(0)

    # --- Order flow ------------------------------------------------------------------------
    approved_orders: int = Field(default=0, ge=0)
    resized_orders: int = Field(default=0, ge=0)
    rejected_orders: int = Field(default=0, ge=0)
    risk_rejections: int = Field(default=0, ge=0)
    broker_rejections: int = Field(default=0, ge=0)

    # --- Process ---------------------------------------------------------------------------
    runtime_seconds: Money = Decimal(0)
    """Day-scoped: wall-clock span the day's processed bars cover, first close to last."""

    bars_processed: int = Field(default=0, ge=0)
    """Day-scoped."""

    acceptance_rate: Money | None = None
    """Session-cumulative: a rejected bar is never attributed to a day."""

    bars_rejected: int = Field(default=0, ge=0)
    """Session-cumulative, for the same reason."""

    out_of_order_candles: int = Field(default=0, ge=0)
    unknown_symbols: int = Field(default=0, ge=0)
    missing_bars: int = Field(default=0, ge=0)
    runtime_exceptions: int = Field(default=0, ge=0)
    session_interruptions: int = Field(default=0, ge=0)
    report_failures: int = Field(default=0, ge=0)
    clock_drift_seconds: Money | None = None

    # --- Feed, daily ---------------------------------------------------------------------------
    # Every counter below covers *this day only*, obtained by subtracting the feed reading
    # at the day's start from the reading at its end. The feed's own counters are cumulative
    # and never reset; naming these `daily_` is what stops a reader mistaking one for the
    # other, which is exactly the confusion that let day one's reconnects haunt day two.
    daily_reconnects: int = Field(default=0, ge=0)
    daily_heartbeat_failures: int = Field(default=0, ge=0)
    daily_gaps: int = Field(default=0, ge=0)
    daily_rejected_frames: int = Field(default=0, ge=0)
    daily_malformed_frames: int = Field(default=0, ge=0)
    daily_candles_received: int = Field(default=0, ge=0)
    daily_candles_accepted: int = Field(default=0, ge=0)
    daily_candles_rejected: int = Field(default=0, ge=0)
    daily_duplicate_candles: int = Field(default=0, ge=0)

    daily_feed_acceptance_rate: Money | None = None
    """Today's accepted candles over today's received candles.

    Recomputed from the daily counts, never differenced: the change in a ratio is not the
    ratio of the change. ``None`` when the feed delivered nothing today.
    """

    feed_metrics_available: bool = False
    """Whether a feed reading reached this report at all.

    The distinction that makes the rest of this section readable. Before Phase 7B.1 a day
    with eleven reconnects and a day with no feed attached both reported zero, and the
    report could not tell them apart. ``False`` means nobody measured; zeros beside
    ``True`` mean the feed was genuinely clean.
    """

    @property
    def observed_acceptance_rate(self) -> Decimal | None:
        """Return the acceptance rate feed-stability should be judged on.

        The feed's daily rate when a reading reached this report, and the session's
        cumulative bar-acceptance rate otherwise. The two count different things — the feed
        counts candles the venue sent that never became bars, the session counts bars it
        refused for its own reasons — so the measured one wins whenever it exists.
        """
        if self.feed_metrics_available:
            return self.daily_feed_acceptance_rate
        return self.acceptance_rate

    @property
    def is_profitable(self) -> bool:
        """Return whether the day ended above where it started."""
        return self.daily_pnl > 0

    @property
    def total_rejections(self) -> int:
        """Return every order refused, by risk or by the venue adapter."""
        return self.risk_rejections + self.broker_rejections

    @property
    def order_rejection_ratio(self) -> Decimal | None:
        """Return refused orders as a share of every order decision made.

        ``None`` on a day with no decisions: a rejection rate over zero attempts is
        undefined, and reporting it as zero would read like a clean day.
        """
        total = self.approved_orders + self.total_rejections
        if total == 0:
            return None
        return Decimal(self.total_rejections) / Decimal(total)


class DailyHealth(DomainModel):
    """The operational verdict on a day."""

    level: HealthLevel
    checks: tuple[HealthCheck, ...] = ()

    @property
    def failing(self) -> tuple[HealthCheck, ...]:
        """Return every check that came back worse than green."""
        return tuple(check for check in self.checks if check.level is not HealthLevel.GREEN)

    @property
    def feed_level(self) -> HealthLevel:
        """Return the worst level among the checks that describe the data stream.

        A narrower question than overall health: an operator deciding whether to trust
        today's *data* does not care that the session restarted or that the clock drifted.
        """
        return HealthLevel.worst(
            tuple(check.level for check in self.checks if check.name in _FEED_CHECKS)
        )

    @property
    def skipped(self) -> tuple[HealthCheck, ...]:
        """Return every check nothing could evaluate."""
        return tuple(check for check in self.checks if check.skipped)


class DailyAlerts(DomainModel):
    """Everything the day raised."""

    alerts: tuple[Alert, ...] = ()

    @property
    def count(self) -> int:
        """Return how many alerts fired."""
        return len(self.alerts)

    @property
    def critical(self) -> tuple[Alert, ...]:
        """Return the alerts at critical severity."""
        return tuple(alert for alert in self.alerts if alert.severity is AlertSeverity.CRITICAL)

    def by_code(self, code: AlertCode) -> Alert | None:
        """Return the alert raised under a code, if any."""
        return next((alert for alert in self.alerts if alert.code is code), None)


class SeriesPoint(DomainModel):
    """One observation on a plotted series."""

    at: UtcDatetime
    value: Money


class DailySeries(DomainModel):
    """The day's shape, kept alongside its summary.

    Stored in the report rather than recomputed at render time, so a chart drawn today and
    a chart drawn from the same JSON next month are the same chart.
    """

    equity: tuple[SeriesPoint, ...] = ()
    drawdown: tuple[SeriesPoint, ...] = ()
    returns: tuple[SeriesPoint, ...] = ()
    exposure: tuple[SeriesPoint, ...] = ()
    trade_pnl: tuple[Money, ...] = ()
    """Net result of each round trip that closed today, in the order they closed."""

    @property
    def is_empty(self) -> bool:
        """Return whether the day produced nothing to plot."""
        return not self.equity


class DailyComparison(DomainModel):
    """How today moved against the last day that reported.

    Deltas only, and no judgement about whether a change is good — a smaller drawdown on a
    day that placed no trades is not an improvement, it is an absence.
    """

    previous_day: date
    pnl_delta: Money
    win_rate_delta: Money | None = None
    drawdown_delta: Money | None = None
    sharpe_delta: Money | None = None
    trade_count_delta: int = 0
    acceptance_rate_delta: Money | None = None
    health_delta: int = 0
    """Change in health rank: positive means the day got operationally worse."""

    deteriorations: tuple[Text, ...] = ()
    """Plain statements of what moved the wrong way, for the summary to quote."""

    @property
    def health_deteriorated(self) -> bool:
        """Return whether operational health moved to a worse level."""
        return self.health_delta > 0


class DailySummary(DomainModel):
    """The human-readable account of a day.

    Operational observations only. This never says what to trade, how much to allocate or
    where a market is going: a reporting layer that offered a view on those would be giving
    investment advice, which it is in no position to give and is not permitted to.
    """

    headline: Text
    profit_line: Text
    health_line: Text
    recommendations: tuple[Text, ...] = ()
    """Operational next steps — investigate a feed, check a threshold — never trading calls."""

    anomalies: tuple[Text, ...] = ()


class DailyReport(DomainModel):
    """One trading day, as it happened."""

    session_id: Text
    strategy_id: Text
    day: date
    generated_at: UtcDatetime
    quote_asset: Text
    timezone: Text

    statistics: DailyStatistics
    health: DailyHealth
    alerts: DailyAlerts
    summary: DailySummary
    series: DailySeries = DailySeries()
    trades: tuple[RoundTrip, ...] = ()
    comparison: DailyComparison | None = None
    """Absent on the first reported day, when there is nothing to compare against."""

    @property
    def has_trades(self) -> bool:
        """Return whether any position closed today."""
        return bool(self.trades)

    @property
    def is_quiet(self) -> bool:
        """Return whether the day processed bars but closed nothing."""
        return self.statistics.bars_processed > 0 and not self.trades
