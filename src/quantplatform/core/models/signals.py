"""Strategy input context and output signal models."""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.models.base import (
    DomainModel,
    SemanticVersion,
    StrategyId,
    Symbol,
    Text,
    UtcDatetime,
)
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.numeric import Money, Rate

__all__ = ["Signal", "StrategyContext"]


class StrategyContext(DomainModel):
    """Everything a strategy is allowed to observe when generating signals.

    The context deliberately excludes cash, balances, order state, venue credentials and
    the execution mode. A strategy therefore cannot distinguish backtest from live, cannot
    size against the account and cannot reach the venue. Position state is exposed as a
    coarse enum so that exit logic is expressible without revealing account financials.
    """

    symbol: Symbol
    market_type: MarketType
    timeframe: Timeframe
    as_of: UtcDatetime
    bars: tuple[MarketBar, ...] = Field(min_length=1)
    features: dict[str, Money] = Field(default_factory=dict)
    position_state: PositionState
    symbol_rules: SymbolRules

    @model_validator(mode="after")
    def _validate_history(self) -> Self:
        """Check that history is closed, ordered, consistent and ends at ``as_of``."""
        previous_open = None
        for bar in self.bars:
            if bar.symbol != self.symbol:
                msg = f"bar symbol {bar.symbol!r} does not match context symbol {self.symbol!r}"
                raise ValueError(msg)
            if bar.timeframe is not self.timeframe:
                msg = "every bar must share the context timeframe"
                raise ValueError(msg)
            if not bar.is_closed:
                msg = "strategy context must contain only closed bars"
                raise ValueError(msg)
            if previous_open is not None and bar.open_time <= previous_open:
                msg = "bars must be strictly increasing in open_time"
                raise ValueError(msg)
            previous_open = bar.open_time
        if self.bars[-1].close_time != self.as_of:
            msg = "as_of must equal the close_time of the most recent bar"
            raise ValueError(msg)
        if self.symbol_rules.symbol != self.symbol:
            msg = "symbol_rules must describe the context symbol"
            raise ValueError(msg)
        return self

    @property
    def latest_bar(self) -> MarketBar:
        """Return the most recent closed bar."""
        return self.bars[-1]

    @property
    def history_length(self) -> int:
        """Return the number of closed bars available."""
        return len(self.bars)

    def closes(self) -> tuple[Decimal, ...]:
        """Return the closing prices in chronological order."""
        return tuple(bar.close for bar in self.bars)


class Signal(DomainModel):
    """A directional intent produced by a strategy from closed market data.

    A signal is an opinion, not an order. It carries no sizing, no venue detail and no
    authority: only the risk engine can turn one into something executable.
    """

    signal_id: UUID
    strategy_id: StrategyId
    strategy_version: SemanticVersion
    symbol: Symbol
    market_type: MarketType
    timeframe: Timeframe
    bar_close_time: UtcDatetime
    generated_at: UtcDatetime
    action: SignalAction
    confidence: Rate
    reason: Text
    features: dict[str, Money] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_causality(self) -> Self:
        """Reject signals that claim to predate the bar that produced them."""
        if self.generated_at < self.bar_close_time:
            msg = "generated_at must not precede the close of the bar that produced the signal"
            raise ValueError(msg)
        return self

    @property
    def is_actionable(self) -> bool:
        """Return whether the signal can be converted into an order intent."""
        return self.action.is_actionable
