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
    "InconsistentSeedStateError",
    "InsufficientBalanceError",
    "InsufficientPositionError",
    "InvalidFillSideError",
    "LiveTradingNotAuthorizedError",
    "NegativeBalanceError",
    "OrderNotFoundError",
    "OrderSubmissionError",
    "OutOfOrderDataError",
    "OutOfOrderFillError",
    "PortfolioError",
    "QuantPlatformError",
    "ReconciliationError",
    "RiskError",
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
    "UnknownOrderStateError",
    "UnsupportedFeeAssetError",
    "UnsupportedMarketTypeError",
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
