"""Ports of the platform.

These protocols are the only contracts that cross domain boundaries. High-level policy —
orchestration, risk, portfolio accounting — depends on these abstractions; adapters for
exchanges, databases and data vendors depend on them in the opposite direction. No
implementation detail of a venue or storage engine may leak through these signatures.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from quantplatform.core.enums import (
    ExecutionMode,
    MarketDataFeedState,
    MarketType,
    Timeframe,
)
from quantplatform.core.events import DomainEvent
from quantplatform.core.models.data import BarWriteResult, DataQualityFinding, IngestionRun
from quantplatform.core.models.health import ComponentHealth
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order, OrderIntent
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.portfolio import Balance, PortfolioSnapshot, Position
from quantplatform.core.models.risk import RiskContext, RiskDecision
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.core.models.telemetry import FeedMetricsSnapshot, SymbolRulesTelemetry

__all__ = [
    "CandleStreamTransport",
    "DataUnitOfWork",
    "EventPublisher",
    "ExecutionAdapter",
    "FeaturePipeline",
    "FeedMetricsReader",
    "IngestionRunRepository",
    "MarketBarRepository",
    "MarketDataProvider",
    "PaperMarketDataFeed",
    "PaperStateRepository",
    "PortfolioEngine",
    "RiskEngine",
    "SettlementLedger",
    "Strategy",
    "StreamingMarketDataProvider",
    "SymbolRulesMaintainer",
    "SymbolRulesProvider",
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
class CandleStreamTransport(Protocol):
    """A duplex text channel to a market-data stream, and nothing more.

    This port exists to keep the network out of the feed's reasoning. Everything that makes
    a live feed hard — reconnection, heartbeats, duplicate suppression, gap detection — is
    logic that must be exercised deterministically, and none of it can be if the only way to
    produce a dropped socket in a test is to actually drop a socket.

    The contract is deliberately dumb: it moves text and reports failure. It cannot describe
    an order, an account or a credential, so no amount of change on the far side of it can
    turn a market-data adapter into a trading one.
    """

    @property
    def is_connected(self) -> bool:
        """Return whether a channel is currently open."""
        ...

    def connect(self, url: str) -> None:
        """Open the channel.

        Args:
            url: Endpoint to connect to.

        Raises:
            MarketDataConnectionError: If the channel cannot be opened.
        """
        ...

    def send(self, payload: str) -> None:
        """Send one text frame.

        Raises:
            MarketDataConnectionError: If the channel is closed or the send fails.
        """
        ...

    def receive(self, timeout_seconds: float) -> str | None:
        """Return the next text frame, or ``None`` when none arrived in time.

        A timeout is an ordinary, expected outcome on a quiet market — it is how the caller
        learns to check its heartbeat — so it is reported as ``None`` rather than raised.
        A genuine transport failure is raised.

        Args:
            timeout_seconds: How long to wait for a frame.

        Returns:
            The frame's text, or ``None`` on timeout.

        Raises:
            MarketDataConnectionError: If the channel failed while waiting.
        """
        ...

    def close(self) -> None:
        """Release the channel; safe to call more than once, and never raises."""
        ...


@runtime_checkable
class FeedMetricsReader(Protocol):
    """Anything that can report how healthy a market-data feed has been.

    Separate from :class:`StreamingMarketDataProvider` because the two answer different
    questions and not every feed can answer this one. A deterministic replay has no health
    to report — nothing about it can drop, stall or skip — and forcing it to invent
    counters would put fiction where a report expects measurement.

    A live feed is expected to implement both. A paper runner given a streaming provider
    without a reader refuses to start, because a run that cannot see its own data quality
    produces reports that look clean for the wrong reason.
    """

    def read_feed_metrics(self) -> FeedMetricsSnapshot:
        """Return the feed's cumulative counters as of now.

        Cumulative, never reset: a caller wanting a window subtracts two readings. A feed
        that reset its own counters would erase any window nobody had reported yet.
        """
        ...


@runtime_checkable
class SymbolRulesProvider(Protocol):
    """Anything that can read the venue's current trading rules.

    Read-only, and narrowly so. Fetching what a venue will accept requires no credential
    and touches no account: a public metadata endpoint answers it. Nothing in this protocol
    can place, cancel or inspect an order, and an implementation that needed an API key to
    satisfy it would be answering a different question than the one being asked.

    Batched on purpose. Venues publish every instrument's rules in one document and rate
    limit by request, so asking for six symbols individually costs six times as much as
    asking once and is six times as likely to be throttled partway through.

    Distinct from :meth:`MarketDataProvider.fetch_symbol_rules`, which is asynchronous and
    belongs to the historical data path. A paper session's refresh loop is synchronous —
    the platform runs no event loop — and bridging the two would mean starting one just to
    make a metadata call.
    """

    def fetch(self, symbols: Sequence[str]) -> Mapping[str, SymbolRules]:
        """Return current venue rules for every requested symbol.

        Args:
            symbols: Canonical symbols to describe.

        Returns:
            The rules, keyed by canonical symbol, each stamped with the time it was read.

        Raises:
            DataProviderError: If the venue could not be reached or its answer was
                unreadable.
            DataIntegrityError: If the answer was readable but did not describe usable
                rules. Defaulting a missing limit would size orders against a number the
                venue never published, so an implementation must raise instead.
        """
        ...


@runtime_checkable
class SymbolRulesMaintainer(Protocol):
    """Whatever keeps the venue's trading rules current during a long run.

    The seam between a loop that pumps bars and a policy that decides when rules have aged
    enough to re-fetch. The runner calls this and forwards the reading; it never learns what
    a refresh interval is, and the trading components below it never learn that refreshing
    happens at all — they read the shared store and see whatever is currently in it.

    One call does both jobs deliberately. Refreshing and reading as separate steps could
    interleave with a replacement and report an age that belongs to neither the old rules
    nor the new ones.
    """

    def maintain(self) -> SymbolRulesTelemetry:
        """Refresh the rules if they are due, and report the state of the mechanism.

        Must not raise on a failed refresh. A venue that is briefly unreachable is an
        ordinary event; the previous rules stay in force, the failure is counted, and the
        risk engine's freshness check remains the thing that decides whether trading may
        continue.

        Returns:
            The current reading, whether or not a refresh happened on this call.
        """
        ...


@runtime_checkable
class StreamingMarketDataProvider(Protocol):
    """A live, read-only source of closed candles.

    The streaming counterpart to :class:`MarketDataProvider`, which fetches history on
    request. The two are separate ports because they are separate problems: one answers
    "what happened between these dates", the other "what just finished printing", and an
    adapter is normally good at one of them rather than both.

    **Read-only by construction.** There is no order method, no balance method, no account
    or user-data stream, and no authenticated endpoint. An implementation of this protocol
    cannot place a trade, because nothing in the contract can express one.

    Shaped to satisfy :class:`PaperMarketDataFeed` as well: ``symbols``, ``closed_bars`` and
    ``close`` carry the same meaning in both, so a real provider drops into a paper session
    wherever a replay double sat, with no adapter in between and no change to the session.
    """

    @property
    def name(self) -> str:
        """Return the stable identifier of this data source, recorded on every bar."""
        ...

    @property
    def symbols(self) -> Sequence[str]:
        """Return the canonical platform symbols currently subscribed."""
        ...

    @property
    def state(self) -> MarketDataFeedState:
        """Return the feed's connection and synchronisation state."""
        ...

    def connect(self) -> None:
        """Open the upstream connection.

        Raises:
            MarketDataConnectionError: If the connection cannot be established.
        """
        ...

    def disconnect(self) -> None:
        """Close the upstream connection, leaving the feed reusable."""
        ...

    def subscribe(self, symbols: Sequence[str]) -> None:
        """Add symbols to the live subscription.

        Args:
            symbols: Canonical platform symbols.

        Raises:
            MarketDataSubscriptionError: If a symbol cannot be mapped or is refused.
        """
        ...

    def unsubscribe(self, symbols: Sequence[str]) -> None:
        """Remove symbols from the live subscription."""
        ...

    def closed_bars(self) -> Iterator[MarketBar]:
        """Yield closed bars as the venue publishes them.

        Blocking between candles is expected. Implementations must never yield a forming
        bar, must never yield the same bar twice, and must stop yielding rather than skip
        a missing one.

        Returns:
            An iterator over closed bars in strictly increasing open-time order.
        """
        ...

    def close(self) -> None:
        """Release every resource the feed holds; safe to call more than once."""
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
class FeaturePipeline(Protocol):
    """Turns a window of closed bars into the feature values a strategy reads.

    A pure function of the bars it is given, for the same reason strategies are: a backtest
    that cannot reproduce its own features cannot reproduce its own signals. Implementations
    read no clock, perform no input or output, and must return the same mapping for the same
    window every time.

    The window ends at the bar being decided on and never extends past it, which is what
    keeps a feature from seeing a price that had not yet printed.
    """

    @property
    def feature_names(self) -> Sequence[str]:
        """Return the names this pipeline can produce, for contract checking before a run."""
        ...

    @property
    def required_history(self) -> int:
        """Return the smallest number of bars that yields a complete feature set."""
        ...

    def compute(self, bars: Sequence[MarketBar]) -> Mapping[str, Decimal]:
        """Compute features for the window ending at the most recent bar.

        Args:
            bars: Closed bars in ascending open-time order; the last is the bar being decided.

        Returns:
            Feature values keyed by name. A feature the window is too short to compute is
            omitted rather than guessed at, so a strategy declaring it as required fails its
            context check instead of trading on a fabricated number.
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
class SettlementLedger(Protocol):
    """Everything an execution adapter may do to an account, and nothing more.

    This is the **only** port between execution and portfolio accounting. An adapter can
    hand over a fill and move funds between the available and reserved halves of a balance;
    it cannot read positions, cost basis, realised PnL or snapshots, so it structurally
    cannot begin keeping books of its own. Adapters must not declare a private protocol of
    their own alongside this one — two descriptions of the same boundary drift apart.

    Reserving is not an accounting event: it changes availability, never ownership, so
    ``Balance.total`` and every position, cost-basis and PnL figure are unaffected. Only a
    fill changes what the account owns, and only through :meth:`apply_fill`.

    Implementations are expected to be the same component that implements
    :class:`PortfolioEngine`, since balances have exactly one owner.
    """

    def apply_fill(self, fill: Fill) -> None:
        """Incorporate a fill into balances, positions and realised PnL.

        Implementations must be idempotent with respect to the fill's identifier.
        """
        ...

    def reserve(self, *, asset: str, amount: Decimal, at: datetime) -> None:
        """Move ``amount`` of ``asset`` from available to reserved.

        Args:
            asset: Asset whose balance is being reserved against.
            amount: Non-negative amount to reserve.
            at: Instant recorded on the updated balance, from the event being processed.

        Raises:
            InsufficientBalanceError: If the available amount does not cover ``amount``.
        """
        ...

    def release(self, *, asset: str, amount: Decimal, at: datetime) -> None:
        """Move ``amount`` of ``asset`` from reserved back to available.

        Args:
            asset: Asset whose reservation is being released.
            amount: Non-negative amount to release.
            at: Instant recorded on the updated balance, from the event being processed.

        Raises:
            InsufficientBalanceError: If the reserved amount does not cover ``amount``.
        """
        ...

    def reserved(self, asset: str) -> Decimal:
        """Return the currently reserved amount of ``asset``, zero when it is unknown."""
        ...

    def balance(self, asset: str) -> Balance | None:
        """Return the current balance for ``asset``, or ``None`` when it is unknown.

        Exposed so an adapter can capture a balance before mutating it and restore it
        exactly if the mutation has to be undone. It carries no accounting information
        beyond the asset's available and reserved amounts.
        """
        ...


@runtime_checkable
class PaperMarketDataFeed(Protocol):
    """Delivers closed bars from a live market as they finish.

    The only difference between a paper session and a backtest is where the bars come from,
    so this port is deliberately shaped like an iterator over the same
    :class:`~quantplatform.core.models.market.MarketBar` a backtest consumes. Everything
    downstream — features, strategy, risk, execution, accounting — cannot tell the two apart,
    which is the entire point of running paper mode before risking money.

    **Only closed bars.** An implementation must not yield a forming candle. A strategy that
    sees a partial bar is reading a price that has not settled, and every decision it makes
    from one is unreproducible. Waiting for the close is the adapter's job; the session
    re-checks it regardless, because a feed bug must not become an accounting bug.
    """

    @property
    def symbols(self) -> Sequence[str]:
        """Return the symbols this feed delivers bars for."""
        ...

    def closed_bars(self) -> Iterator[MarketBar]:
        """Yield closed bars in non-decreasing close-time order.

        Blocking until the next candle closes is expected and correct. The iterator ends when
        the feed is stopped or the upstream source is exhausted.

        Returns:
            An iterator over closed bars.
        """
        ...

    def close(self) -> None:
        """Release the feed's resources; safe to call more than once."""
        ...


@runtime_checkable
class PaperStateRepository(Protocol):
    """Persists just enough of a paper session to resume it after a restart.

    A paper session is long-lived — days or weeks — so a process restart must not silently
    reset the account it was tracking. What is persisted is a snapshot of the session, not a
    replay log: reconstructing a portfolio by re-running weeks of decisions would depend on
    the strategy and the venue behaving identically the second time, which is exactly what a
    paper run exists to find out.

    **This is a port only.** No SQL, key-value or file implementation lives in the platform
    yet; a composition root supplies one. That keeps the session testable with an in-memory
    double and keeps a storage choice out of the trading logic.
    """

    def load(self, session_id: str) -> PaperSessionState | None:
        """Return the stored state for a session, or ``None`` when it has never been saved."""
        ...

    def save(self, state: PaperSessionState) -> None:
        """Store a session's state, replacing any previous snapshot for the same id.

        Implementations should write atomically: a partially written snapshot is worse than
        no snapshot, because it resumes into an account that never existed.
        """
        ...

    def delete(self, session_id: str) -> None:
        """Remove a session's stored state; a no-op when nothing was stored."""
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
