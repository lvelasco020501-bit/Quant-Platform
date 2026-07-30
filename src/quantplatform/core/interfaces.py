"""Ports of the platform.

These protocols are the only contracts that cross domain boundaries. High-level policy —
orchestration, risk, portfolio accounting — depends on these abstractions; adapters for
exchanges, databases and data vendors depend on them in the opposite direction. No
implementation detail of a venue or storage engine may leak through these signatures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from quantplatform.core.enums import ExecutionMode, MarketType, Timeframe
from quantplatform.core.events import DomainEvent
from quantplatform.core.models.data import BarWriteResult, DataQualityFinding, IngestionRun
from quantplatform.core.models.health import ComponentHealth
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order, OrderIntent
from quantplatform.core.models.portfolio import Balance, PortfolioSnapshot, Position
from quantplatform.core.models.risk import RiskContext, RiskDecision
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata

__all__ = [
    "DataUnitOfWork",
    "EventPublisher",
    "ExecutionAdapter",
    "IngestionRunRepository",
    "MarketBarRepository",
    "MarketDataProvider",
    "PortfolioEngine",
    "RiskEngine",
    "Strategy",
]


@runtime_checkable
class MarketDataProvider(Protocol):
    """Source of historical and current market data.

    Implementations are responsible for venue-specific transport only. Integrity
    validation and normalisation belong to the data layer, not to the provider.
    """

    @property
    def name(self) -> str:
        """Return the stable identifier of this data source, recorded on every bar."""
        ...

    async def fetch_bars(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        market_type: MarketType,
    ) -> Sequence[MarketBar]:
        """Fetch bars whose open time falls in the half-open interval ``[start, end)``.

        Args:
            symbol: Canonical platform symbol.
            timeframe: Bar interval.
            start: Inclusive lower bound, timezone-aware.
            end: Exclusive upper bound, timezone-aware.
            market_type: Market to read from.

        Returns:
            Bars ordered by ascending open time.
        """
        ...

    async def fetch_symbol_rules(self, *, symbol: str, market_type: MarketType) -> SymbolRules:
        """Fetch the venue trading rules currently in force for a symbol."""
        ...

    async def health(self) -> ComponentHealth:
        """Report the reachability and freshness of this data source."""
        ...


@runtime_checkable
class Strategy(Protocol):
    """A pure decision function over closed market data.

    Implementations receive a :class:`~quantplatform.core.models.signals.StrategyContext`
    and return signals. They perform no input or output, hold no account state and cannot
    observe the execution mode, so the same implementation runs unchanged in backtest,
    paper, shadow and live.
    """

    @property
    def metadata(self) -> StrategyMetadata:
        """Return the strategy's self-description."""
        ...

    @property
    def parameters(self) -> BaseModel:
        """Return the validated parameters this instance was constructed with."""
        ...

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        """Produce signals for the closed bar at ``context.as_of``.

        Args:
            context: Market and feature context, containing only closed bars.

        Returns:
            Zero or more signals. An empty result means no opinion.
        """
        ...


@runtime_checkable
class RiskEngine(Protocol):
    """Final authority over every order intent.

    The engine is the only component permitted to produce an
    :class:`~quantplatform.core.models.orders.ApprovedOrder`.
    """

    def evaluate(self, intent: OrderIntent, context: RiskContext) -> RiskDecision:
        """Approve, resize or reject an order intent.

        Implementations must record every check they evaluated, including passed and
        skipped ones, and must never raise for an ordinary rejection.

        Args:
            intent: Proposed order to evaluate.
            context: Observable system, market and portfolio state.

        Returns:
            A decision carrying an approved order when, and only when, every check passed.
        """
        ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Translates approved orders into venue operations.

    Adapters never generate signals and never make risk decisions. The same protocol backs
    the simulated broker, the paper and shadow adapters and any live venue adapter.
    """

    @property
    def mode(self) -> ExecutionMode:
        """Return the execution mode this adapter implements."""
        ...

    @property
    def venue(self) -> str:
        """Return the venue identifier, or a simulation marker for simulated adapters."""
        ...

    async def submit(self, order: ApprovedOrder) -> Order:
        """Submit an approved order.

        Implementations must be idempotent with respect to
        :attr:`~quantplatform.core.models.orders.ApprovedOrder.client_order_id`: resubmitting
        the same client order id must return the existing order rather than create a second
        one at the venue.

        Args:
            order: Risk-approved order.

        Returns:
            The resulting order state.
        """
        ...

    async def cancel(self, *, client_order_id: str) -> Order:
        """Cancel a working order and return its resulting state."""
        ...

    async def get_order(self, *, client_order_id: str) -> Order:
        """Return the current state of a previously submitted order."""
        ...

    async def open_orders(self, *, symbol: str | None = None) -> Sequence[Order]:
        """Return every order still working at the venue."""
        ...

    async def fetch_balances(self) -> Sequence[Balance]:
        """Return the venue's view of account balances, used for reconciliation."""
        ...

    async def fetch_fills(self, *, symbol: str, since: datetime) -> Sequence[Fill]:
        """Return fills executed at or after ``since``, used for reconciliation."""
        ...

    async def fetch_symbol_rules(self, *, symbol: str, market_type: MarketType) -> SymbolRules:
        """Return the venue trading rules currently in force for a symbol."""
        ...

    async def health(self) -> ComponentHealth:
        """Report venue reachability and any degraded condition."""
        ...


@runtime_checkable
class PortfolioEngine(Protocol):
    """Applies fills and maintains balances, positions and PnL."""

    def has_applied(self, fill_id: UUID) -> bool:
        """Return whether a fill has already been incorporated into portfolio state."""
        ...

    def apply_fill(self, fill: Fill) -> None:
        """Incorporate a fill into balances, positions and realised PnL.

        Implementations must be idempotent: applying a previously seen ``fill_id`` must
        leave state unchanged rather than double-count it.

        Args:
            fill: Executed trade to apply.
        """
        ...

    def positions(self) -> Sequence[Position]:
        """Return the current positions."""
        ...

    def balances(self) -> Sequence[Balance]:
        """Return the current asset balances."""
        ...

    def snapshot(
        self,
        *,
        as_of: datetime,
        mark_prices: Mapping[str, Decimal],
    ) -> PortfolioSnapshot:
        """Produce an immutable marked snapshot of the account.

        Args:
            as_of: Instant the snapshot represents, from the injected clock.
            mark_prices: Price per symbol used to value open positions.

        Returns:
            A snapshot whose equity equals cash plus marked position value.
        """
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Fan-out point for domain events."""

    async def publish(self, event: DomainEvent) -> None:
        """Publish a single event to every interested subscriber."""
        ...

    async def publish_many(self, events: Sequence[DomainEvent]) -> None:
        """Publish events in order."""
        ...


@runtime_checkable
class MarketBarRepository(Protocol):
    """Persists and retrieves normalised market bars.

    The natural key of a bar is ``(symbol, market_type, timeframe, open_time)``. Adding a
    bar that already exists under that key with identical OHLCV values is a no-op; adding
    one with different values must never silently overwrite the stored bar. Implementations
    must not commit or roll back a transaction themselves: the caller owns that boundary.
    """

    async def add_bars(self, bars: Sequence[MarketBar]) -> Sequence[BarWriteResult]:
        """Idempotently stage bars for persistence.

        Args:
            bars: Normalised bars to add, in any order.

        Returns:
            One :class:`~quantplatform.core.models.data.BarWriteResult` per input bar,
            in the same order, reporting whether it was inserted, was an exact duplicate,
            or conflicted with an already-stored bar.
        """
        ...

    async def get_bars(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketBar]:
        """Return stored bars whose open time falls in the half-open interval ``[start, end)``.

        Args:
            symbol: Canonical platform symbol.
            market_type: Market the bars belong to.
            timeframe: Bar interval.
            start: Inclusive lower bound, timezone-aware.
            end: Exclusive upper bound, timezone-aware.

        Returns:
            Bars ordered deterministically by ascending open time.
        """
        ...

    async def get_latest_bar(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
    ) -> MarketBar | None:
        """Return the most recent stored bar for a symbol, market and timeframe, if any."""
        ...

    async def exists(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        open_time: datetime,
    ) -> bool:
        """Return whether a bar is already stored under this natural key."""
        ...

    async def count_bars(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
    ) -> int:
        """Return the number of stored bars for a symbol, market and timeframe."""
        ...


@runtime_checkable
class IngestionRunRepository(Protocol):
    """Persists ingestion provenance and the findings raised while producing it.

    Implementations must not commit or roll back a transaction themselves: the ingestion
    service owns that boundary, and writes the run together with its findings as a single
    atomic unit after every bar-write attempt has already concluded, whether that attempt
    succeeded or was rolled back.
    """

    async def record_run(
        self,
        run: IngestionRun,
        findings: Sequence[DataQualityFinding],
    ) -> None:
        """Persist a concluded ingestion run together with every finding it raised.

        Args:
            run: The run's final provenance and outcome.
            findings: Every finding raised while processing the run, in any order.
        """
        ...

    async def get_run(self, run_id: UUID) -> IngestionRun | None:
        """Return a previously persisted run by id, if it exists."""
        ...

    async def get_findings(self, run_id: UUID) -> Sequence[DataQualityFinding]:
        """Return every finding recorded against a run, in any order."""
        ...


@runtime_checkable
class DataUnitOfWork(Protocol):
    """A single transactional scope over the market-data repositories.

    This port is what gives the ingestion service explicit transaction ownership. The
    repositories it exposes never commit on their own, so nothing reaches the database
    until the service calls :meth:`commit`; leaving the context without committing rolls
    everything back. That is what makes "no bars are persisted when a fatal finding is
    raised" a structural guarantee rather than a matter of remembering to clean up.
    """

    @property
    def bars(self) -> MarketBarRepository:
        """Return the market bar repository bound to this transaction."""
        ...

    @property
    def runs(self) -> IngestionRunRepository:
        """Return the ingestion run repository bound to this transaction."""
        ...

    async def __aenter__(self) -> DataUnitOfWork:
        """Begin the transactional scope."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back and release the scope unless it was explicitly committed."""
        ...

    async def commit(self) -> None:
        """Commit everything staged in this scope."""
        ...

    async def rollback(self) -> None:
        """Discard everything staged in this scope."""
        ...
