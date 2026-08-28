"""Domain enumerations shared across every layer of the platform.

All enumerations derive from :class:`enum.StrEnum` so that they serialise to stable,
human-readable strings in logs, JSON payloads and database columns.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

from quantplatform.core.constants import (
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
)

__all__ = [
    "AlertSeverity",
    "AssetClass",
    "BarWriteOutcome",
    "CircuitBreakerReason",
    "CommissionModel",
    "DataQualityIssue",
    "Environment",
    "EventType",
    "ExecutionMode",
    "FindingSeverity",
    "IngestionStatus",
    "LogFormat",
    "MarketDataFeedState",
    "MarketType",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PositionState",
    "ReconciliationStatus",
    "RiskCheckCode",
    "RiskCheckSeverity",
    "RiskCheckStatus",
    "RiskOutcome",
    "SignalAction",
    "SlippageModel",
    "SystemState",
    "TimeInForce",
    "Timeframe",
]


class Environment(StrEnum):
    """Deployment environment of the running process."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    """Rendering format for the structured logger."""

    JSON = "json"
    TEXT = "text"


class AssetClass(StrEnum):
    """Asset class of a tradable instrument."""

    CRYPTO = "crypto"
    EQUITY = "equity"
    FUTURES = "futures"
    FX = "fx"


class MarketType(StrEnum):
    """Market microstructure category of an instrument."""

    SPOT = "spot"
    MARGIN = "margin"
    FUTURES = "futures"
    PERPETUAL = "perpetual"

    @property
    def allows_leverage(self) -> bool:
        """Return whether the market type can be traded with leverage."""
        return self is not MarketType.SPOT

    @property
    def allows_short(self) -> bool:
        """Return whether the market type supports short exposure."""
        return self is not MarketType.SPOT


class Timeframe(StrEnum):
    """Bar aggregation interval."""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H12 = "12h"
    D1 = "1d"
    W1 = "1w"

    @property
    def seconds(self) -> int:
        """Return the exact duration of the timeframe in seconds."""
        return _TIMEFRAME_SECONDS[self]

    @property
    def duration(self) -> timedelta:
        """Return the exact duration of the timeframe as a :class:`~datetime.timedelta`."""
        return timedelta(seconds=self.seconds)


_TIMEFRAME_SECONDS: Final[dict[Timeframe, int]] = {
    Timeframe.M1: SECONDS_PER_MINUTE,
    Timeframe.M3: 3 * SECONDS_PER_MINUTE,
    Timeframe.M5: 5 * SECONDS_PER_MINUTE,
    Timeframe.M15: 15 * SECONDS_PER_MINUTE,
    Timeframe.M30: 30 * SECONDS_PER_MINUTE,
    Timeframe.H1: SECONDS_PER_HOUR,
    Timeframe.H2: 2 * SECONDS_PER_HOUR,
    Timeframe.H4: 4 * SECONDS_PER_HOUR,
    Timeframe.H6: 6 * SECONDS_PER_HOUR,
    Timeframe.H12: 12 * SECONDS_PER_HOUR,
    Timeframe.D1: SECONDS_PER_DAY,
    Timeframe.W1: 7 * SECONDS_PER_DAY,
}


class ExecutionMode(StrEnum):
    """How generated decisions are turned into orders."""

    BACKTEST = "backtest"
    """Simulated clock and simulated broker over historical data."""

    PAPER = "paper"
    """Real market data, simulated order fills."""

    SHADOW = "shadow"
    """Real market data, decisions recorded only; nothing is executed anywhere."""

    LIVE = "live"
    """Real orders submitted to a real venue."""

    @property
    def uses_real_time_data(self) -> bool:
        """Return whether the mode consumes a live market data feed."""
        return self is not ExecutionMode.BACKTEST

    @property
    def submits_external_orders(self) -> bool:
        """Return whether the mode sends orders to an external venue."""
        return self is ExecutionMode.LIVE

    @property
    def simulates_fills(self) -> bool:
        """Return whether the mode produces simulated fills.

        Shadow mode deliberately produces no fills at all: it only records the
        hypothetical decision.
        """
        return self in (ExecutionMode.BACKTEST, ExecutionMode.PAPER)


class SystemState(StrEnum):
    """Operating state of the platform as a whole."""

    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    HALTED = "halted"
    RECONCILIATION_REQUIRED = "reconciliation_required"

    @property
    def allows_new_orders(self) -> bool:
        """Return whether new order submission is permitted in this state."""
        return self is SystemState.HEALTHY


class SignalAction(StrEnum):
    """Directional intent expressed by a strategy."""

    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    ENTER_SHORT = "enter_short"
    EXIT_SHORT = "exit_short"
    HOLD = "hold"

    @property
    def is_actionable(self) -> bool:
        """Return whether the action can be converted into an order intent."""
        return self is not SignalAction.HOLD

    @property
    def requires_short_selling(self) -> bool:
        """Return whether the action can only be expressed with short exposure."""
        return self is SignalAction.ENTER_SHORT


class PositionState(StrEnum):
    """Coarse position state exposed to strategies.

    Deliberately free of balance or cash information: strategies must not observe
    account financials.
    """

    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class OrderSide(StrEnum):
    """Side of an order."""

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> OrderSide:
        """Return the opposing side."""
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class OrderType(StrEnum):
    """Order type supported by the platform."""

    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"

    @property
    def requires_limit_price(self) -> bool:
        """Return whether the type requires an explicit limit price."""
        return self in (OrderType.LIMIT, OrderType.STOP_LIMIT)

    @property
    def requires_stop_price(self) -> bool:
        """Return whether the type requires an explicit stop trigger price."""
        return self in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)


class TimeInForce(StrEnum):
    """Order lifetime instruction."""

    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(StrEnum):
    """Lifecycle state of an order."""

    PENDING_NEW = "pending_new"
    """Accepted locally, not yet acknowledged by the venue."""

    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"

    PENDING_CANCEL = "pending_cancel"
    """A cancel request has been sent to the venue but not yet confirmed.

    Non-terminal: the venue may still confirm the cancel, but it may equally deliver a fill
    that was already in flight when the cancel was requested, so this status does not by
    itself reject new fills. It can transition to :attr:`CANCELED` (the cancel was
    honoured), :attr:`PARTIALLY_FILLED` or :attr:`FILLED` (a race was lost to a fill that
    was already in flight), or :attr:`UNKNOWN` (the venue's response could not be
    determined and reconciliation is required).
    """

    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    """Venue state could not be determined; requires reconciliation before trading."""

    @property
    def is_terminal(self) -> bool:
        """Return whether no further state transition is expected."""
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def is_open(self) -> bool:
        """Return whether the order still consumes venue capacity."""
        return self in (
            OrderStatus.PENDING_NEW,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_CANCEL,
        )

    @property
    def can_produce_fills(self) -> bool:
        """Return whether the status permits *new* fills to be attributed to the order.

        ``PENDING_CANCEL`` is included deliberately: a cancel request in flight must not,
        by itself, cause a fill that the venue already executed to be rejected as invalid.
        """
        return self in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.FILLED,
        )

    @property
    def may_carry_fills(self) -> bool:
        """Return whether an order in this status may report a non-zero ``filled_quantity``.

        Broader than :attr:`can_produce_fills`, which asks whether a *new* fill may be
        attributed right now. An order that traded and was then cancelled or expired keeps
        the quantity it already executed: partial execution followed by cancellation of the
        remainder is ordinary venue behaviour (every ``IOC`` order that does not fill
        completely ends exactly this way), so those terminal states must be able to carry
        the fills that happened while the order was still live. Only ``PENDING_NEW`` (never
        reached the venue) and ``REJECTED`` (never became a working order) genuinely cannot.
        """
        return self is not OrderStatus.PENDING_NEW and self is not OrderStatus.REJECTED

    def can_transition_to(self, target: OrderStatus) -> bool:
        """Return whether moving from this status to ``target`` is a legal transition.

        A terminal status admits no transition at all, including to itself. The
        self-transition ``PARTIALLY_FILLED -> PARTIALLY_FILLED`` is legal and expected: each
        further partial execution on an already partly filled order reports a status change
        carrying its new cumulative quantity.
        """
        return target in _ORDER_TRANSITIONS[self]


_ORDER_TRANSITIONS: Final[dict[OrderStatus, frozenset[OrderStatus]]] = {
    OrderStatus.PENDING_NEW: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.PENDING_CANCEL,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PENDING_CANCEL: frozenset(
        {
            OrderStatus.CANCELED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    # Reconciliation is the only way out of UNKNOWN: it resolves to whatever the venue
    # actually did, which may be any outcome including none.
    OrderStatus.UNKNOWN: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}
"""Legal order-status transitions. Terminal statuses map to the empty set."""


class SlippageModel(StrEnum):
    """Deterministic price-adjustment model applied by the simulated broker.

    Both values are fully deterministic; the platform deliberately offers no probabilistic
    slippage, because a backtest whose fills depend on a random draw is not reproducible.
    """

    OFF = "off"
    """Execute at the unadjusted matched price."""

    FIXED_BPS = "fixed_bps"
    """Move the execution price against the taker by a fixed number of basis points."""


class CommissionModel(StrEnum):
    """Deterministic commission model applied by the simulated broker."""

    NONE = "none"
    """Charge nothing."""

    BASIS_POINTS = "basis_points"
    """Charge a rate in basis points of the executed notional, where ``10`` is 0.1 percent.

    Named for the unit it is actually expressed in. Calling this "percentage" while reading
    a basis-point field is exactly the ambiguity that turns a 0.1 percent fee into a 10
    percent one.
    """

    FLAT = "flat"
    """Charge a fixed quote-asset amount once per order, independent of size.

    Charged on an order's **first** fill and never again, so the total commission of an
    order is knowable before it starts executing. A per-fill flat fee would not be: the
    number of partial fills a resting order will need is not known when its funds are
    reserved, so reserving for it would mean either under-reserving (and failing to settle
    a fill the venue already executed) or holding an arbitrary multiple of the fee hostage.
    """


class RiskCheckStatus(StrEnum):
    """Outcome of an individual risk check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Not applicable in the current mode or configuration; recorded for auditability."""


class RiskCheckSeverity(StrEnum):
    """Whether a failing risk check blocks the order or is merely recorded.

    Separating severity from :class:`RiskCheckStatus` is what lets the engine evaluate and
    report a check whose failure is informative but not disqualifying, without that report
    silently becoming a veto. A decision is rejected if and only if at least one
    ``BLOCKING`` check failed.
    """

    BLOCKING = "blocking"
    """A failure rejects the intent."""

    ADVISORY = "advisory"
    """A failure is recorded for the audit trail but never rejects on its own."""


class RiskOutcome(StrEnum):
    """Final verdict of the risk engine over an order intent."""

    APPROVED = "approved"
    RESIZED = "resized"
    REJECTED = "rejected"

    @property
    def is_approved(self) -> bool:
        """Return whether an executable order was produced."""
        return self in (RiskOutcome.APPROVED, RiskOutcome.RESIZED)


class RiskCheckCode(StrEnum):
    """Catalogue of every risk check the engine is able to perform.

    The catalogue is defined in the core domain so that risk decisions remain
    auditable and comparable across engine versions.
    """

    SYSTEM_STATE = "system_state"
    CONFIGURATION_VALID = "configuration_valid"
    EXECUTION_MODE = "execution_mode"
    DATA_FRESHNESS = "data_freshness"
    CLOSED_CANDLE = "closed_candle"
    SYMBOL_RULES_FRESHNESS = "symbol_rules_freshness"
    REFERENCE_PRICE = "reference_price"
    DUPLICATE_SIGNAL = "duplicate_signal"
    DUPLICATE_ORDER = "duplicate_order"
    PENDING_ORDERS = "pending_orders"
    CONFLICTING_ORDER = "conflicting_order"
    AVAILABLE_BALANCE = "available_balance"
    ACCOUNTING_INVARIANT = "accounting_invariant"
    ALLOWED_SYMBOL = "allowed_symbol"
    ALLOWED_MARKET_TYPE = "allowed_market_type"
    ALLOWED_ORDER_TYPE = "allowed_order_type"
    ALLOWED_TIME_IN_FORCE = "allowed_time_in_force"
    QUANTITY_PRECISION = "quantity_precision"
    PRICE_PRECISION = "price_precision"
    MINIMUM_QUANTITY = "minimum_quantity"
    MAXIMUM_QUANTITY = "maximum_quantity"
    MINIMUM_NOTIONAL = "minimum_notional"
    MAXIMUM_NOTIONAL = "maximum_notional"
    MAX_ORDER_NOTIONAL = "max_order_notional"
    MAX_POSITION_COUNT = "max_position_count"
    MAX_EXPOSURE = "max_exposure"
    PROTECTIVE_STOP = "protective_stop"
    RISK_BUDGET = "risk_budget"
    MAX_SYMBOL_EXPOSURE = "max_symbol_exposure"
    MARKET_BUY_CAP = "market_buy_cap"
    MAX_DAILY_ORDERS = "max_daily_orders"
    MAX_HOURLY_ORDERS = "max_hourly_orders"
    DAILY_DRAWDOWN = "daily_drawdown"
    TOTAL_DRAWDOWN = "total_drawdown"
    MAX_DAILY_LOSS = "max_daily_loss"
    MAX_TOTAL_DRAWDOWN_BREAKER = "max_total_drawdown_breaker"
    MAX_CONSECUTIVE_LOSSES = "max_consecutive_losses"
    EXCESSIVE_SPREAD = "excessive_spread"
    EXCESSIVE_VOLATILITY = "excessive_volatility"
    EXCHANGE_HEALTH = "exchange_health"
    RECONCILIATION_STATUS = "reconciliation_status"
    CONSECUTIVE_API_FAILURES = "consecutive_api_failures"
    SHORT_SELLING_PROHIBITED = "short_selling_prohibited"
    LEVERAGE_PROHIBITED = "leverage_prohibited"


class CircuitBreakerReason(StrEnum):
    """Condition that automatically halts trading."""

    STALE_MARKET_DATA = "stale_market_data"
    REPEATED_API_FAILURES = "repeated_api_failures"
    UNKNOWN_ORDER_STATE = "unknown_order_state"
    DUPLICATE_ORDERS = "duplicate_orders"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    UNEXPLAINED_BALANCE_DIFFERENCE = "unexplained_balance_difference"
    EXCESSIVE_DRAWDOWN = "excessive_drawdown"
    EXCESSIVE_SLIPPAGE = "excessive_slippage"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    POSITION_EXPOSURE_LIMIT = "position_exposure_limit"
    CLOCK_DESYNCHRONIZATION = "clock_desynchronization"
    REPEATED_PROCESS_CRASHES = "repeated_process_crashes"
    DATABASE_FAILURE = "database_failure"
    INVALID_CONFIGURATION = "invalid_configuration"


class StopKind(StrEnum):
    """How a position's protective exit is expressed.

    A stop belongs to the *intent* rather than to the strategy's private state, so that the
    risk engine can size a position against it and an execution layer can honour it even if
    the strategy never speaks again. That separation is the whole reason this enum exists:
    week 5 held a position for four days with no protection of any kind, because the only
    exit that existed lived inside the strategy's own crossover logic.
    """

    HARD = "hard"
    """A fixed level. Once breached the position is closed, without consulting anything."""

    TRAILING = "trailing"
    """A level that follows the favourable extreme, never retreating."""

    BREAK_EVEN = "break_even"
    """A level moved to entry once a configured advance is reached."""

    TIME = "time"
    """A maximum holding duration, independent of price."""


class RiskActionKind(StrEnum):
    """What the risk engine has decided must happen to existing exposure.

    Deliberately small. Risk may reduce, close or stop opening — it may never *open*, size
    up, or choose an instrument. Those are the strategy's decisions, and a risk engine able
    to make them would be a second strategy nobody audited.
    """

    NONE = "none"
    REDUCE = "reduce"
    CLOSE = "close"
    HALT_NEW_ENTRIES = "halt_new_entries"


class DataQualityIssue(StrEnum):
    """Integrity problem detected on inbound market data."""

    MISSING_BAR = "missing_bar"
    DUPLICATE_BAR = "duplicate_bar"
    OUT_OF_ORDER_BAR = "out_of_order_bar"
    STALE_DATA = "stale_data"
    INVALID_OHLC = "invalid_ohlc"
    NEGATIVE_VOLUME = "negative_volume"
    UNEXPECTED_TIMEFRAME = "unexpected_timeframe"
    UNEXPECTED_SYMBOL = "unexpected_symbol"
    UNEXPECTED_MARKET_TYPE = "unexpected_market_type"
    OPEN_CANDLE = "open_candle"
    CLOSURE_CONFLICT = "closure_conflict"
    """The provider's closure flag and the platform clock disagree about the bar."""

    MALFORMED_RECORD = "malformed_record"
    """A raw record could not be parsed into a valid bar."""

    REVISED_BAR = "revised_bar"
    """An incoming bar conflicts with a stored bar under the same natural key.

    Named for the situation it describes — the source has revised a candle it already
    published — not for an action taken: the platform never overwrites the stored bar, it
    preserves it and records the conflict.
    """

    EMPTY_DATASET = "empty_dataset"
    """A source produced no usable rows, either as delivered or after validation."""


class MarketDataFeedState(StrEnum):
    """Connection and synchronisation state of a streaming market-data feed.

    Connectivity and *trustworthiness* are separate questions, which is why
    :attr:`PAUSED` exists alongside :attr:`DISCONNECTED`. A feed that is perfectly
    connected but has discovered a hole in the candle series must stop delivering bars
    just as firmly as one whose socket has dropped: trading through a gap means deciding
    on a history the market did not actually print.
    """

    DISCONNECTED = "disconnected"
    """No transport is open; nothing has been attempted yet, or everything was released."""

    CONNECTING = "connecting"
    RECONNECTING = "reconnecting"
    """A previous connection dropped and the backoff schedule is being worked through."""

    CONNECTED = "connected"
    """The transport is open but no subscription has been confirmed yet."""

    STREAMING = "streaming"
    """Subscribed and delivering candles."""

    PAUSED = "paused"
    """A continuity break was detected; recovery must be acknowledged before bars resume."""

    STOPPED = "stopped"
    """Deliberately shut down. Terminal for the feed instance."""

    @property
    def delivers_bars(self) -> bool:
        """Return whether the feed may hand a bar to the trading pipeline in this state."""
        return self is MarketDataFeedState.STREAMING

    @property
    def is_connected(self) -> bool:
        """Return whether a transport is currently expected to be open."""
        return self in (
            MarketDataFeedState.CONNECTED,
            MarketDataFeedState.STREAMING,
            MarketDataFeedState.PAUSED,
        )


class IngestionStatus(StrEnum):
    """Terminal outcome of a market data ingestion run."""

    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_FINDINGS = "succeeded_with_findings"
    FAILED = "failed"

    @property
    def is_successful(self) -> bool:
        """Return whether the run persisted its data."""
        return self is not IngestionStatus.FAILED


class FindingSeverity(StrEnum):
    """Severity of a data-quality finding, defining what ingestion does about it.

    ``INFO`` and ``WARNING`` are purely informational: ingestion continues and the
    affected record, if any, is still persisted. ``ERROR`` rejects the specific record
    that triggered it, but the rest of the dataset may still be ingested. ``FATAL`` fails
    the entire ingestion run: no bars from that run are persisted, though the run and its
    findings still are, so the failure remains auditable.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"

    @property
    def blocks_record(self) -> bool:
        """Return whether a finding at this severity causes its record to be rejected."""
        return self in (FindingSeverity.ERROR, FindingSeverity.FATAL)

    @property
    def blocks_ingestion(self) -> bool:
        """Return whether a finding at this severity fails the entire ingestion run."""
        return self is FindingSeverity.FATAL


class BarWriteOutcome(StrEnum):
    """Result of attempting to persist a single normalised bar.

    Reused by the bar repository to report, per bar, whether it was newly inserted, was
    an exact repeat of an already-stored bar (idempotent no-op), or conflicted with an
    already-stored bar that has different OHLCV values (never silently overwritten).
    """

    INSERTED = "inserted"
    EXACT_DUPLICATE = "exact_duplicate"
    CONFLICTING = "conflicting"


class ReconciliationStatus(StrEnum):
    """Result of comparing local state against the execution venue."""

    NEVER_RUN = "never_run"
    IN_SYNC = "in_sync"
    DRIFT_DETECTED = "drift_detected"
    FAILED = "failed"

    @property
    def allows_trading(self) -> bool:
        """Return whether trading may proceed given this reconciliation result."""
        return self is ReconciliationStatus.IN_SYNC


class AlertSeverity(StrEnum):
    """Severity of an operational alert."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventType(StrEnum):
    """Stable identifier for every domain event emitted by the platform."""

    MARKET_BAR_RECEIVED = "market_bar_received"
    DATA_QUALITY_ISSUE_DETECTED = "data_quality_issue_detected"
    INGESTION_STARTED = "ingestion_started"
    INGESTION_COMPLETED = "ingestion_completed"
    INGESTION_FAILED = "ingestion_failed"
    FEATURES_COMPUTED = "features_computed"
    SIGNAL_GENERATED = "signal_generated"
    ORDER_INTENT_CREATED = "order_intent_created"
    RISK_DECISION_MADE = "risk_decision_made"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_STATUS_CHANGED = "order_status_changed"
    FILL_RECEIVED = "fill_received"
    POSITION_CHANGED = "position_changed"
    PORTFOLIO_UPDATED = "portfolio_updated"
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"
    SYSTEM_STATE_CHANGED = "system_state_changed"
    ALERT_RAISED = "alert_raised"
