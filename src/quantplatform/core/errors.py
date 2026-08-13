"""Exception hierarchy for the platform.

Every error raised by platform code derives from :class:`QuantPlatformError` and carries a
stable ``code`` plus a structured ``details`` mapping. This keeps failures machine-readable
for structured logging, alerting and persistence, and avoids relying on message parsing.
"""

from __future__ import annotations

__all__ = [
    "AccountingInvariantError",
    "CircuitBreakerTrippedError",
    "ClockDesynchronizationError",
    "ConfigurationError",
    "DataError",
    "DataGapError",
    "DataIntegrityError",
    "DataProviderError",
    "DomainValidationError",
    "DuplicateFillError",
    "DuplicateOrderError",
    "ExchangeUnavailableError",
    "ExecutionError",
    "FeedTelemetryRegressionError",
    "InconsistentSeedStateError",
    "InsufficientBalanceError",
    "InsufficientPositionError",
    "InsufficientReservationError",
    "InvalidFillSideError",
    "InvalidRiskConfigurationError",
    "LiveTradingNotAuthorizedError",
    "MarketDataConnectionError",
    "MarketDataSubscriptionError",
    "MatchingError",
    "MissingRiskContextError",
    "NegativeBalanceError",
    "OrderNotFoundError",
    "OrderStateTransitionError",
    "OrderSubmissionError",
    "OutOfOrderDataError",
    "OutOfOrderFillError",
    "PaperSessionStateError",
    "PortfolioError",
    "QuantPlatformError",
    "ReconciliationError",
    "RiskError",
    "RiskInvariantError",
    "RiskRejectionError",
    "StaleDataError",
    "StorageError",
    "StrategyAlreadyRegisteredError",
    "StrategyContextError",
    "StrategyError",
    "StrategyNotFoundError",
    "StrategyParameterError",
    "SymbolMismatchError",
    "SymbolRuleViolationError",
    "SystemHaltedError",
    "TelemetryNotConfiguredError",
    "UnknownOrderStateError",
    "UnsupportedFeeAssetError",
    "UnsupportedMarketTypeError",
    "UnsupportedOrderTypeError",
    "UnsupportedRiskInputError",
    "UnsupportedTimeInForceError",
]


class QuantPlatformError(Exception):
    """Base class for every error raised by the platform.

    Args:
        message: Human-readable description of the failure.
        details: Structured, non-sensitive context safe to log and persist.
    """

    code: str = "quantplatform_error"

    def __init__(self, message: str, /, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = details

    def __str__(self) -> str:
        """Return the message followed by sorted structured details, when present."""
        if not self.details:
            return self.message
        rendered = ", ".join(f"{key}={self.details[key]!r}" for key in sorted(self.details))
        return f"{self.message} ({rendered})"

    def to_dict(self) -> dict[str, object]:
        """Return a serialisable representation for logging and API responses."""
        return {"code": self.code, "message": self.message, "details": dict(self.details)}

    def log_extra(self) -> dict[str, object]:
        """Return a representation safe to pass as ``extra=`` to the stdlib logger.

        :meth:`to_dict` is for callers that serialise or return the error, and its
        top-level ``"message"`` key is exactly what makes it unsafe to hand to
        ``logging.Logger.error(..., extra=...)`` directly: ``logging`` raises
        ``KeyError`` when ``extra`` carries any key already set on the record it is
        building, and every record's own message is computed under that exact name. A
        caller that logged ``extra=exc.to_dict()`` to report a caught error crashed
        instead, on the handler meant to report it -- with the caught error never
        reaching a log at all.

        Nesting everything under one key that can never be a reserved ``LogRecord``
        attribute is what makes this safe *unconditionally*: nothing inside
        :meth:`to_dict` -- present, or added by a subclass yet to be written -- is ever
        inspected by ``makeRecord``, because only this method's own top-level key is.
        No information is dropped; ``to_dict()``'s full output is still there, one level
        down.

        Returns:
            ``{"error": self.to_dict()}``.
        """
        return {"error": self.to_dict()}


# --- Configuration ---------------------------------------------------------------------


class ConfigurationError(QuantPlatformError):
    """Raised when configuration is missing, contradictory or unsafe."""

    code = "configuration_error"


class LiveTradingNotAuthorizedError(ConfigurationError):
    """Raised when live execution is requested without complete explicit authorisation."""

    code = "live_trading_not_authorized"


# --- Domain ----------------------------------------------------------------------------


class DomainValidationError(QuantPlatformError, ValueError):
    """Raised when a domain invariant would be violated.

    Also derives from :class:`ValueError` so that the same guard functions can be reused
    inside pydantic validators, where they surface as a regular ``ValidationError``.
    """

    code = "domain_validation_error"


# --- Data ------------------------------------------------------------------------------


class DataError(QuantPlatformError):
    """Base class for market data failures."""

    code = "data_error"


class DataProviderError(DataError):
    """Raised when a market data provider fails to serve a request."""

    code = "data_provider_error"


class DataIntegrityError(DataError):
    """Raised when a bar or series violates structural integrity rules."""

    code = "data_integrity_error"


class DataGapError(DataError):
    """Raised when an expected bar is missing from a contiguous series."""

    code = "data_gap_error"


class StaleDataError(DataError):
    """Raised when the most recent bar is older than the freshness budget."""

    code = "stale_data_error"


class OutOfOrderDataError(DataError):
    """Raised when a bar arrives with a timestamp earlier than the previous bar."""

    code = "out_of_order_data_error"


class FeedTelemetryRegressionError(DataError):
    """Raised when feed counters move backwards between two readings.

    Feed counters only ever climb, so a smaller number than last time means the two
    readings do not describe the same continuous run — a restarted adapter, a swapped
    feed, or two sessions' snapshots crossed. Subtracting them anyway would produce a
    negative daily count, and a report is worse than useless once it contains one.
    """

    code = "feed_telemetry_regression"


class TelemetryNotConfiguredError(ConfigurationError):
    """Raised when a live feed is wired up without a way to read its health.

    A deterministic replay needs no telemetry: nothing about it can degrade. A real stream
    can drop, stall and skip, and a paper run that cannot see any of that produces reports
    which look clean because nothing was measured. Refusing at wiring time is the only
    point where that is still cheap to fix.
    """

    code = "telemetry_not_configured"


class MarketDataConnectionError(DataError):
    """Raised when a streaming market-data transport cannot be established or kept alive.

    Distinct from :class:`ExchangeUnavailableError`, which concerns the *execution* venue.
    A market-data feed being unreachable stops new decisions; it says nothing about whether
    orders could be placed, and conflating the two would let a data outage read as an
    execution outage in the audit trail.
    """

    code = "market_data_connection_error"


class MarketDataSubscriptionError(DataError):
    """Raised when a stream subscription is refused, or names an instrument the feed cannot map.

    Always a configuration or wiring fault rather than a transient one: retrying a
    subscription the venue has rejected produces the same rejection.
    """

    code = "market_data_subscription_error"


# --- Strategy --------------------------------------------------------------------------


class StrategyError(QuantPlatformError):
    """Base class for strategy failures."""

    code = "strategy_error"


class StrategyNotFoundError(StrategyError):
    """Raised when a strategy id is not present in the registry."""

    code = "strategy_not_found"


class StrategyAlreadyRegisteredError(StrategyError):
    """Raised when two strategies claim the same registry key."""

    code = "strategy_already_registered"


class StrategyParameterError(StrategyError):
    """Raised when strategy parameters fail their declared schema."""

    code = "strategy_parameter_error"


class StrategyContextError(StrategyError):
    """Raised when the supplied context does not satisfy the strategy contract."""

    code = "strategy_context_error"


# --- Risk ------------------------------------------------------------------------------


class RiskError(QuantPlatformError):
    """Base class for risk engine failures."""

    code = "risk_error"


class RiskRejectionError(RiskError):
    """Raised when an order intent is rejected and the caller demanded execution."""

    code = "risk_rejection"


class SymbolRuleViolationError(RiskError):
    """Raised when an order violates venue trading rules for the symbol."""

    code = "symbol_rule_violation"


class InvalidRiskConfigurationError(RiskError):
    """Raised when risk limits are internally contradictory or unsafe to trade under."""

    code = "invalid_risk_configuration"


class MissingRiskContextError(RiskError):
    """Raised when the risk engine is handed a context lacking data a check requires.

    Reserved for data the engine cannot proceed *at all* without. A missing optional metric
    that a check can record and move past is a failed or skipped
    :class:`~quantplatform.core.models.risk.RiskCheckResult`, not an exception.
    """

    code = "missing_risk_context"


class RiskInvariantError(RiskError):
    """Raised when the risk engine would emit a decision that violates its own invariants.

    A programming error, never an ordinary rejection: an intent the engine declines is a
    ``REJECTED`` decision, not a raised exception.
    """

    code = "risk_invariant_violation"


class UnsupportedRiskInputError(RiskError):
    """Raised when an intent is structurally outside anything the engine can reason about."""

    code = "unsupported_risk_input"


class SystemHaltedError(RiskError):
    """Raised when an operation is attempted while the system is halted."""

    code = "system_halted"


class CircuitBreakerTrippedError(RiskError):
    """Raised when a circuit breaker condition forces trading to stop."""

    code = "circuit_breaker_tripped"


# --- Execution -------------------------------------------------------------------------


class ExecutionError(QuantPlatformError):
    """Base class for execution adapter failures."""

    code = "execution_error"


class OrderSubmissionError(ExecutionError):
    """Raised when an order could not be submitted to the venue."""

    code = "order_submission_error"


class OrderNotFoundError(ExecutionError):
    """Raised when an order cannot be located locally or at the venue."""

    code = "order_not_found"


class DuplicateOrderError(ExecutionError):
    """Raised when the same client order id is submitted more than once."""

    code = "duplicate_order"


class UnknownOrderStateError(ExecutionError):
    """Raised when the venue state of an order cannot be determined."""

    code = "unknown_order_state"


class ExchangeUnavailableError(ExecutionError):
    """Raised when the venue is unreachable or reports itself unhealthy."""

    code = "exchange_unavailable"


class OrderStateTransitionError(ExecutionError):
    """Raised when an order is moved between two states that no lifecycle path connects."""

    code = "order_state_transition"


class UnsupportedOrderTypeError(ExecutionError):
    """Raised when an order type is outside the set the adapter can execute."""

    code = "unsupported_order_type"


class UnsupportedTimeInForceError(ExecutionError):
    """Raised when a time-in-force instruction is outside the set the adapter honours."""

    code = "unsupported_time_in_force"


class MatchingError(ExecutionError):
    """Raised when a bar cannot be matched against an order, for example on a symbol clash."""

    code = "matching_error"


class InsufficientReservationError(ExecutionError):
    """Raised when releasing more of a reservation than an order still holds."""

    code = "insufficient_reservation"


# --- Portfolio -------------------------------------------------------------------------


class PortfolioError(QuantPlatformError):
    """Base class for portfolio accounting failures."""

    code = "portfolio_error"


class InsufficientBalanceError(PortfolioError):
    """Raised when an operation would consume more of an asset than is available."""

    code = "insufficient_balance"


class NegativeBalanceError(PortfolioError):
    """Raised when accounting would drive a balance or position quantity below zero."""

    code = "negative_balance"


class DuplicateFillError(PortfolioError):
    """Raised when a fill that was already applied is presented again."""

    code = "duplicate_fill"


class InsufficientPositionError(PortfolioError):
    """Raised when a sell fill would reduce a position below zero exposure."""

    code = "insufficient_position"


class UnsupportedFeeAssetError(PortfolioError):
    """Raised when a fill carries a non-zero fee in an asset other than the quote asset."""

    code = "unsupported_fee_asset"


class SymbolMismatchError(PortfolioError):
    """Raised when a fill's symbol is unknown to the engine or spans an unexpected quote asset."""

    code = "symbol_mismatch"


class UnsupportedMarketTypeError(PortfolioError):
    """Raised when a fill is presented for a market type other than spot."""

    code = "unsupported_market_type"


class InvalidFillSideError(PortfolioError):
    """Raised when a fill carries a side outside the supported buy/sell set."""

    code = "invalid_fill_side"


class AccountingInvariantError(PortfolioError):
    """Raised when applying a fill would leave the ledger in an inconsistent state."""

    code = "accounting_invariant_violation"


class InconsistentSeedStateError(PortfolioError):
    """Raised when an engine would be constructed with an inconsistent starting state."""

    code = "inconsistent_seed_state"


class OutOfOrderFillError(PortfolioError):
    """Raised when a fill's ``executed_at`` precedes the engine's last applied fill."""

    code = "out_of_order_fill"


class PaperSessionStateError(PortfolioError):
    """Raised when a paper session's persisted state cannot be safely resumed."""

    code = "paper_session_state"


class ReconciliationError(PortfolioError):
    """Raised when local state cannot be safely reconciled with the venue."""

    code = "reconciliation_error"


# --- Infrastructure --------------------------------------------------------------------


class StorageError(QuantPlatformError):
    """Raised when persistence fails."""

    code = "storage_error"


class ClockDesynchronizationError(QuantPlatformError):
    """Raised when local time drifts beyond the tolerated skew."""

    code = "clock_desynchronization"
