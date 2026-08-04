"""Immutable records of what a backtest did.

Everything a run produced, stored as it was produced. The event sequence in particular is
kept verbatim rather than reconstructed afterwards from orders and fills: a reconstruction is
a second implementation of the truth, and the two drift.

Every collection is a tuple, so a result cannot be edited after the fact by whatever reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.backtesting.metrics import EquityPoint, PerformanceSummary
from quantplatform.core.events import DomainEvent
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order, OrderIntent
from quantplatform.core.models.portfolio import PortfolioSnapshot
from quantplatform.core.models.risk import RiskDecision
from quantplatform.core.models.signals import Signal

__all__ = ["BacktestResult", "BarOutcome", "ComponentCallCounts"]


@dataclass(frozen=True, slots=True)
class ComponentCallCounts:
    """How many times the engine invoked each collaborator.

    Present so a test can assert the chain was actually traversed — that the strategy really
    was consulted once per bar and the risk engine really did see every intent — rather than
    inferring it from outputs that a short-circuit could also produce.
    """

    feature_computations: int = 0
    strategy_invocations: int = 0
    risk_evaluations: int = 0
    broker_submissions: int = 0
    broker_bars_processed: int = 0
    portfolio_fills_applied: int = 0


@dataclass(frozen=True, slots=True)
class BarOutcome:
    """Everything one bar produced, in the order it happened."""

    index: int
    bar: MarketBar
    signals: tuple[Signal, ...] = ()
    intents: tuple[OrderIntent, ...] = ()
    decisions: tuple[RiskDecision, ...] = ()
    submitted: tuple[ApprovedOrder, ...] = ()
    fills: tuple[Fill, ...] = ()
    orders: tuple[Order, ...] = ()
    events: tuple[DomainEvent, ...] = ()
    snapshot: PortfolioSnapshot | None = None

    @property
    def approved_count(self) -> int:
        """Return how many decisions on this bar authorised an order."""
        return sum(1 for decision in self.decisions if decision.is_executable)

    @property
    def rejected_count(self) -> int:
        """Return how many decisions on this bar refused the intent."""
        return sum(1 for decision in self.decisions if not decision.is_executable)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The complete, immutable record of a finished run."""

    config: BacktestConfig
    bars: tuple[BarOutcome, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()
    snapshots: tuple[PortfolioSnapshot, ...] = ()
    signals: tuple[Signal, ...] = ()
    intents: tuple[OrderIntent, ...] = ()
    decisions: tuple[RiskDecision, ...] = ()
    approved_orders: tuple[ApprovedOrder, ...] = ()
    orders: tuple[Order, ...] = ()
    fills: tuple[Fill, ...] = ()
    events: tuple[DomainEvent, ...] = ()
    """Every event any component emitted, in the exact order the run produced them."""

    calls: ComponentCallCounts = field(default_factory=ComponentCallCounts)
    performance: PerformanceSummary | None = None
    """``None`` only for a run over an empty dataset, which produced nothing to summarise."""

    started_at: datetime | None = None
    """Close time of the first processed bar; ``None`` for an empty run."""

    ended_at: datetime | None = None
    """Close time of the last processed bar; ``None`` for an empty run."""

    @property
    def bars_processed(self) -> int:
        """Return how many bars the run consumed."""
        return len(self.bars)

    @property
    def rejected_decisions(self) -> tuple[RiskDecision, ...]:
        """Return every decision that refused to authorise an order."""
        return tuple(decision for decision in self.decisions if not decision.is_executable)

    @property
    def approved_decisions(self) -> tuple[RiskDecision, ...]:
        """Return every decision that authorised an order, resized or not."""
        return tuple(decision for decision in self.decisions if decision.is_executable)
