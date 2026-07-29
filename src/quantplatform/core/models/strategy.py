"""Strategy contract metadata.

The metadata lives in the core domain rather than in the strategies package so that
orchestration can reason about a strategy's requirements — history depth, features,
timeframes, markets and parameter schema — without importing strategy implementations.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, model_validator

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.base import DomainModel, SemanticVersion, StrategyId, Text

__all__ = ["StrategyMetadata"]


class StrategyMetadata(DomainModel):
    """Self-description that every strategy must declare."""

    strategy_id: StrategyId
    version: SemanticVersion
    name: Text
    description: Text
    required_history: int = Field(ge=1)
    """Minimum number of closed bars the strategy needs before it may emit signals."""

    required_features: tuple[str, ...] = ()
    supported_timeframes: tuple[Timeframe, ...] = Field(min_length=1)
    supported_market_types: tuple[MarketType, ...] = Field(min_length=1)
    parameter_schema: type[BaseModel]
    operates_intrabar: bool = False
    """Must stay ``False`` unless the strategy explicitly declares intrabar operation."""

    allows_short: bool = False

    @model_validator(mode="after")
    def _validate_declarations(self) -> Self:
        """Reject duplicate declarations, which would make capability checks ambiguous."""
        for label, values in (
            ("required_features", self.required_features),
            ("supported_timeframes", self.supported_timeframes),
            ("supported_market_types", self.supported_market_types),
        ):
            if len(set(values)) != len(values):
                msg = f"{label} must not contain duplicates"
                raise ValueError(msg)
        if self.allows_short and all(
            not market_type.allows_short for market_type in self.supported_market_types
        ):
            msg = "a short-capable strategy must support at least one shortable market type"
            raise ValueError(msg)
        return self

    def supports(self, *, timeframe: Timeframe, market_type: MarketType) -> bool:
        """Return whether the strategy declares support for a market and timeframe.

        Args:
            timeframe: Bar interval the orchestrator intends to run.
            market_type: Market the orchestrator intends to trade.

        Returns:
            ``True`` when both are declared as supported.
        """
        return timeframe in self.supported_timeframes and market_type in self.supported_market_types
