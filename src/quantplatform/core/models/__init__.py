"""Immutable domain models shared by every layer of the platform.

The models are the platform's ubiquitous language. They carry validation, not behaviour
that belongs to an engine: a model refuses to represent an impossible state, while
deciding what to do about that state is the responsibility of the owning domain.
"""

from quantplatform.core.models.base import (
    AssetCode,
    DomainModel,
    SemanticVersion,
    StrategyId,
    Symbol,
    Text,
    UtcDatetime,
    VenueId,
)
from quantplatform.core.models.health import (
    CircuitBreakerStatus,
    ComponentHealth,
    HealthStatus,
)
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order, OrderIntent
from quantplatform.core.models.portfolio import Balance, PortfolioSnapshot, Position
from quantplatform.core.models.risk import RiskCheckResult, RiskDecision
from quantplatform.core.models.signals import Signal, StrategyContext

__all__ = [
    "ApprovedOrder",
    "AssetCode",
    "Balance",
    "CircuitBreakerStatus",
    "ComponentHealth",
    "DomainModel",
    "Fill",
    "HealthStatus",
    "MarketBar",
    "Order",
    "OrderIntent",
    "PortfolioSnapshot",
    "Position",
    "RiskCheckResult",
    "RiskDecision",
    "SemanticVersion",
    "Signal",
    "StrategyContext",
    "StrategyId",
    "Symbol",
    "SymbolRules",
    "Text",
    "UtcDatetime",
    "VenueId",
]
