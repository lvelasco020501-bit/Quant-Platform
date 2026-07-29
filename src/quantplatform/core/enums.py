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
    "CircuitBreakerReason",
    "DataQualityIssue",
    "Environment",
    "EventType",
    "ExecutionMode",
    "IngestionStatus",
    "LogFormat",
    "MarketType",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PositionState",
    "ReconciliationStatus",
    "RiskCheckCode",
    "RiskCheckStatus",
    "RiskOutcome",
    "SignalAction",
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
        )

    @property
    def can_produce_fills(self) -> bool:
        """Return whether the status permits fills to be attributed to the order."""
        return self in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        )


class RiskCheckStatus(StrEnum):
    """Outcome of an individual risk check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Not applicable in the current mode or configuration; recorded for auditability."""


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
    DATA_FRESHNESS = "data_freshness"
    CLOSED_CANDLE = "closed_candle"
    DUPLICATE_SIGNAL = "duplicate_signal"
    DUPLICATE_ORDER = "duplicate_order"
    PENDING_ORDERS = "pending_orders"
    AVAILABLE_BALANCE = "available_balance"
    ALLOWED_SYMBOL = "allowed_symbol"
    ALLOWED_MARKET_TYPE = "allowed_market_type"
    QUANTITY_PRECISION = "quantity_precision"
    PRICE_PRECISION = "price_precision"
    MINIMUM_QUANTITY = "minimum_quantity"
    MAXIMUM_QUANTITY = "maximum_quantity"
    MINIMUM_NOTIONAL = "minimum_notional"
    MAX_POSITION_COUNT = "max_position_count"
    MAX_EXPOSURE = "max_exposure"
    MAX_DAILY_ORDERS = "max_daily_orders"
    MAX_HOURLY_ORDERS = "max_hourly_orders"
    DAILY_DRAWDOWN = "daily_drawdown"
    TOTAL_DRAWDOWN = "total_drawdown"
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
    CLOCK_DESYNCHRONIZATION = "clock_desynchronization"
    REPEATED_PROCESS_CRASHES = "repeated_process_crashes"
    DATABASE_FAILURE = "database_failure"
    INVALID_CONFIGURATION = "invalid_configuration"


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
    """A stored bar was overwritten with different prices or volume by a later fetch."""


class IngestionStatus(StrEnum):
    """Terminal outcome of a market data ingestion run."""

    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_FINDINGS = "succeeded_with_findings"
    FAILED = "failed"

    @property
    def is_successful(self) -> bool:
        """Return whether the run persisted its data."""
        return self is not IngestionStatus.FAILED


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
    INGESTION_COMPLETED = "ingestion_completed"
    FEATURES_COMPUTED = "features_computed"
    SIGNAL_GENERATED = "signal_generated"
    ORDER_INTENT_CREATED = "order_intent_created"
    RISK_DECISION_MADE = "risk_decision_made"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_STATUS_CHANGED = "order_status_changed"
    FILL_RECEIVED = "fill_received"
    PORTFOLIO_UPDATED = "portfolio_updated"
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"
    SYSTEM_STATE_CHANGED = "system_state_changed"
    ALERT_RAISED = "alert_raised"
