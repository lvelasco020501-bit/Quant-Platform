"""A paper trading session: the Phase 5 pipeline, driven by a live feed.

The chain is unchanged — bars, features, strategy, risk, broker, portfolio — and that is the
entire design. The session does not reimplement any of it; it holds a
:class:`~quantplatform.backtesting.engine.BacktestEngine` and feeds it one bar at a time
instead of a whole history at once. A second implementation of the trading logic for the
streaming case would be two things that must stay identical and will not.

What the session adds is everything a long-lived process needs and a backtest does not:

* a lifecycle — :meth:`start`, :meth:`stop`, :meth:`resume`;
* a guard that a bar has genuinely closed before it is acted on;
* runtime metrics distinct from trading performance;
* a snapshot that survives a restart.

**No real orders, ever.** The session talks to the simulated broker and a virtual portfolio.
The only thing that is real is the market data. There is no code path from here to a venue,
which is what makes running this against live prices safe.

**Extension points for later phases.** The session takes the strategy and the feature
pipeline as ports, so a strategy ensemble, a regime detector, an adaptive selector or a
model-driven signal source plugs in as another implementation of the *existing* contracts —
:class:`~quantplatform.core.interfaces.FeaturePipeline` for learned features,
:class:`~quantplatform.strategies.base.BaseStrategy` for a composite or model-backed
strategy. None of them require a change here, and none are implemented in this phase.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.backtesting.engine import BacktestEngine, RunState
from quantplatform.backtesting.results import BacktestResult, BarOutcome
from quantplatform.core.clock import Clock
from quantplatform.core.constants import ZERO
from quantplatform.core.errors import DataIntegrityError, PaperSessionStateError
from quantplatform.core.interfaces import PaperStateRepository
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.portfolio import PortfolioSnapshot
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.paper.clock import SessionClock
from quantplatform.paper.results import (
    RuntimeMetrics,
    SessionResult,
    SessionSnapshot,
    SessionStatus,
)
from quantplatform.portfolio.engine import SpotPortfolioEngine

__all__ = ["DayRolloverObserver", "PaperTradingSession"]


@runtime_checkable
class DayRolloverObserver(Protocol):
    """Notified once when a session crosses from one reporting day into the next.

    Strictly observational: it is handed a finished day and its return value is discarded,
    so nothing it does can reach a strategy, a risk decision, a broker or the account. That
    is the whole contract — a reporting layer that could influence trading would make every
    report a description of itself.

    **Implementations must not raise.** The session contains a failure rather than letting a
    reporter take down a run that has been going for a week, and counts it in
    :attr:`~quantplatform.paper.results.RuntimeMetrics.report_failures` so containment is
    not the same as silence.

    The observer owns the definition of a "day" through :meth:`day_of`. Time-zone policy is
    a reporting concern, and a session that decided it would be making a reporting decision.
    """

    def day_of(self, moment: datetime) -> date:
        """Return the reporting day a UTC instant belongs to."""
        ...

    def on_day_rollover(self, *, completed_day: date, result: SessionResult) -> None:
        """Handle a day that has just finished.

        Args:
            completed_day: The day that ended, as returned by :meth:`day_of`.
            result: Everything the session has produced up to the rollover.
        """
        ...


class PaperTradingSession:
    """Runs one strategy against a live feed with virtual money."""

    def __init__(  # noqa: PLR0913 - a session is defined by exactly these collaborators
        self,
        *,
        session_id: str,
        engine: BacktestEngine,
        broker: SimulatedBroker,
        portfolio: SpotPortfolioEngine,
        config: BacktestConfig,
        clock: Clock,
        state_repository: PaperStateRepository | None = None,
        close_grace_seconds: int = 0,
        save_every_bar: bool = True,
        day_rollover_observer: DayRolloverObserver | None = None,
    ) -> None:
        """Wire a session.

        Args:
            session_id: Identity this session's persisted state is stored under.
            engine: The Phase 5 pipeline; the session drives it incrementally.
            broker: The simulated broker the engine executes through.
            portfolio: The virtual accounting engine the broker settles into.
            config: Run configuration, shared with the engine.
            clock: Injected time source; no wall clock is read anywhere below.
            state_repository: Where to persist snapshots, or ``None`` to run without.
            close_grace_seconds: Time beyond a bar's close before it counts as final.
            save_every_bar: Persist after each processed bar. Off means a crash loses
                everything since the last explicit :meth:`save`.
            day_rollover_observer: Notified when a bar begins a new reporting day. Purely
                observational and cannot influence the pipeline.
        """
        self._session_id = session_id
        self._engine = engine
        self._broker = broker
        self._portfolio = portfolio
        self._config = config
        self._clock = SessionClock(clock, close_grace_seconds=close_grace_seconds)
        self._repository = state_repository
        self._save_every_bar = save_every_bar
        self._rollover_observer = day_rollover_observer

        self._state: RunState | None = None
        self._running = False
        self._stopped_at: datetime | None = None
        self._last_bar: MarketBar | None = None
        self._restarts = 0
        self._metrics = _MutableMetrics()

    # --- Lifecycle --------------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """Return the session's identity."""
        return self._session_id

    @property
    def is_running(self) -> bool:
        """Return whether the session is accepting bars."""
        return self._running

    def start(self) -> SessionStatus:
        """Begin a fresh session.

        Returns:
            The session's status once started.

        Raises:
            PaperSessionStateError: If the session is already running.
            ConfigurationError: If the pipeline's own contract checks fail.
        """
        if self._running:
            raise PaperSessionStateError("session is already running", session_id=self._session_id)
        if self._state is None:
            self._state = self._engine.begin()
        self._clock.mark_start()
        self._running = True
        self._stopped_at = None
        return self.status()

    def stop(self) -> SessionStatus:
        """Stop accepting bars, persisting a final snapshot.

        Idempotent: stopping a stopped session is not an error, because a shutdown path that
        cannot be run twice is a shutdown path that fails during shutdown.

        Returns:
            The session's status once stopped.
        """
        if not self._running:
            return self.status()
        self._running = False
        self._stopped_at = self._clock.now()
        self.save()
        return self.status()

    def resume(self) -> SessionStatus:
        """Restore a persisted session and begin accepting bars again.

        The account is restored from the stored snapshot rather than by replaying history:
        re-running weeks of past decisions would assume the strategy, the feed and the venue
        all behave identically the second time, which is the very thing a paper run exists to
        find out.

        Returns:
            The session's status once resumed.

        Raises:
            PaperSessionStateError: If the session is running, no repository was supplied,
                nothing was stored, or the stored state cannot be safely restored.
        """
        if self._running:
            raise PaperSessionStateError(
                "cannot resume a running session", session_id=self._session_id
            )
        if self._repository is None:
            raise PaperSessionStateError(
                "cannot resume without a state repository", session_id=self._session_id
            )
        stored = self._repository.load(self._session_id)
        if stored is None:
            raise PaperSessionStateError(
                "no stored state for this session", session_id=self._session_id
            )
        self._apply(stored)
        self._restarts = stored.restarts + 1
        self._metrics.restarts = self._restarts
        self._running = True
        self._stopped_at = None
        return self.status()

    def _apply(self, stored: PaperSessionState) -> None:
        """Restore session bookkeeping from a stored snapshot.

        Raises:
            PaperSessionStateError: If the snapshot holds an open position, which the
                flat-start portfolio engine cannot be seeded with.
        """
        open_positions = [position for position in stored.positions if position.is_open]
        if open_positions:
            raise PaperSessionStateError(
                "cannot resume a session holding an open position: the portfolio engine is "
                "flat-start, and seeding one would bypass its reconciliation invariant",
                session_id=self._session_id,
                symbols=[position.symbol for position in open_positions],
            )
        self._clock.adopt_start(stored.started_at)
        self._last_bar = stored.last_bar
        self._metrics.bars_processed = stored.bars_processed
        self._metrics.last_bar_close_time = (
            stored.last_bar.close_time if stored.last_bar is not None else None
        )
        if self._state is None:
            self._state = self._engine.begin()

    # --- Bar processing ---------------------------------------------------------------------

    def submit_bar(self, bar: MarketBar) -> BarOutcome | None:
        """Offer one bar to the session.

        Returns ``None`` when the bar is refused, which is an ordinary outcome rather than a
        failure: a feed that re-sends a candle, or sends one still forming, must not stop a
        session that has been running for a week.

        A bar is refused when it is still open, has not yet passed its close plus the grace
        period on this session's clock, or does not follow the last bar already processed.
        Acting on a forming candle means deciding from a price that has not settled; acting on
        a bar the account already lived through means trading the same minute twice.

        Args:
            bar: A candle from the feed.

        Returns:
            What the pipeline produced, or ``None`` if the bar was refused.

        Raises:
            PaperSessionStateError: If the session is not running.
            DataIntegrityError: If the bar's symbol is unknown to the pipeline — a
                configuration error rather than a feed hiccup, so it is not swallowed.
        """
        if not self._running or self._state is None:
            raise PaperSessionStateError("session is not running", session_id=self._session_id)
        self._metrics.bars_received += 1

        if not self._is_actionable(bar):
            self._metrics.bars_rejected += 1
            return None

        self._notify_rollover(bar)

        try:
            outcome = self._engine.advance(bar, self._state)
        except DataIntegrityError:
            # Ordering and closure are already screened above, so what remains is an unknown
            # symbol: a wiring mistake, not a transient feed problem, and it must surface.
            self._metrics.bars_rejected += 1
            raise

        self._last_bar = bar
        self._record(outcome)
        if self._save_every_bar:
            self.save()
        return outcome

    def _notify_rollover(self, bar: MarketBar) -> None:
        """Tell the observer a day finished, if this bar starts a new one.

        Called after the bar has been accepted but *before* it is processed, so the day it
        closes off is reported exactly as it ended. Doing it afterwards would fold the first
        bar of the new day into the previous day's figures.

        A failure here is contained rather than propagated: the observer is an onlooker, and
        a broken reporter must not stop a session from trading. The failure is counted, so
        containment does not become silence.
        """
        observer = self._rollover_observer
        if observer is None or self._last_bar is None:
            return
        try:
            previous_day = observer.day_of(self._last_bar.close_time)
            if observer.day_of(bar.close_time) == previous_day:
                return
            observer.on_day_rollover(completed_day=previous_day, result=self.result())
        except Exception:
            # Deliberately broad: an onlooker may fail in any way it likes, and none of them
            # are the session's problem.
            self._metrics.report_failures += 1

    def _is_actionable(self, bar: MarketBar) -> bool:
        """Return whether a bar may be acted on now."""
        if not bar.is_closed:
            return False
        if not self._clock.is_bar_final(bar):
            return False
        return not (self._last_bar is not None and bar.close_time <= self._last_bar.close_time)

    def _record(self, outcome: BarOutcome) -> None:
        """Fold one bar's outcome into the runtime metrics."""
        metrics = self._metrics
        metrics.bars_processed += 1
        metrics.signals_generated += len(outcome.signals)
        metrics.intents_created += len(outcome.intents)
        metrics.decisions_made += len(outcome.decisions)
        metrics.orders_submitted += len(outcome.submitted)
        metrics.fills_received += len(outcome.fills)
        metrics.last_bar_close_time = outcome.bar.close_time
        metrics.last_processed_at = self._clock.now()

    # --- Persistence ------------------------------------------------------------------------

    def save(self) -> PaperSessionState | None:
        """Persist a snapshot of the session, if a repository was supplied.

        Returns:
            The snapshot stored, or ``None`` when running without persistence.
        """
        if self._repository is None:
            return None
        state = self.capture()
        self._repository.save(state)
        self._metrics.state_saves += 1
        return state

    def capture(self) -> PaperSessionState:
        """Return the session's persistable state without storing it."""
        started = self._clock.started_at or self._clock.mark_start()
        snapshot = self._portfolio_snapshot()
        return PaperSessionState(
            session_id=self._session_id,
            strategy_id=self._engine.strategy_id,
            execution_mode=self._config.execution_mode,
            quote_asset=self._config.quote_asset,
            started_at=started,
            saved_at=self._clock.now(),
            balances=tuple(self._portfolio.balances()),
            positions=tuple(self._portfolio.positions()),
            last_bar=self._last_bar,
            bars_processed=self._metrics.bars_processed,
            realized_pnl=snapshot.realized_pnl if snapshot is not None else ZERO,
            total_fees=snapshot.total_fees if snapshot is not None else ZERO,
            restarts=self._restarts,
        )

    # --- Reporting --------------------------------------------------------------------------

    def status(self) -> SessionStatus:
        """Return where the session is in its lifecycle."""
        return SessionStatus(
            running=self._running,
            started_at=self._clock.started_at,
            stopped_at=self._stopped_at,
            restarts=self._restarts,
        )

    def runtime_metrics(self) -> RuntimeMetrics:
        """Return an immutable view of how the process itself is behaving."""
        return self._metrics.freeze(uptime_seconds=self._clock.uptime_seconds())

    def snapshot(self) -> SessionSnapshot:
        """Return the account and the session at this instant."""
        portfolio = self._portfolio_snapshot()
        return SessionSnapshot(
            taken_at=self._clock.now(),
            status=self.status(),
            equity=portfolio.equity if portfolio is not None else self._config.initial_capital,
            cash=portfolio.cash if portfolio is not None else self._config.initial_capital,
            balances=tuple(self._portfolio.balances()),
            positions=tuple(self._portfolio.positions()),
            open_orders=tuple(self._broker.open_orders()),
            last_bar=self._last_bar,
            runtime=self.runtime_metrics(),
            portfolio=portfolio,
        )

    def result(self) -> SessionResult:
        """Return everything the session has produced so far.

        Safe to call while the session is running: the pipeline record is frozen at the point
        of the call, so a caller reading it is not racing the next bar.
        """
        detail: BacktestResult | None = None
        if self._state is not None:
            detail = self._engine.summarise(self._state)
        return SessionResult(
            session_id=self._session_id,
            strategy_id=self._engine.strategy_id,
            execution_mode=self._config.execution_mode,
            status=self.status(),
            runtime=self.runtime_metrics(),
            snapshot=self.snapshot(),
            performance=detail.performance if detail is not None else None,
            fills=detail.fills if detail is not None else (),
            orders=detail.orders if detail is not None else (),
            detail=detail,
        )

    def _portfolio_snapshot(self) -> PortfolioSnapshot | None:
        """Mark the virtual account at the last bar seen, or ``None`` before any."""
        if self._last_bar is None:
            return None
        marks: Mapping[str, Decimal] = {self._last_bar.symbol: self._last_bar.close}
        return self._portfolio.snapshot(as_of=self._last_bar.close_time, mark_prices=dict(marks))


class _MutableMetrics:
    """Running counters, frozen into :class:`RuntimeMetrics` whenever they are read."""

    def __init__(self) -> None:
        self.bars_received = 0
        self.bars_processed = 0
        self.bars_rejected = 0
        self.signals_generated = 0
        self.intents_created = 0
        self.decisions_made = 0
        self.orders_submitted = 0
        self.fills_received = 0
        self.state_saves = 0
        self.restarts = 0
        self.report_failures = 0
        self.last_bar_close_time: datetime | None = None
        self.last_processed_at: datetime | None = None

    def freeze(self, *, uptime_seconds: float) -> RuntimeMetrics:
        """Return an immutable copy of the current counters."""
        return RuntimeMetrics(
            uptime_seconds=uptime_seconds,
            bars_received=self.bars_received,
            bars_processed=self.bars_processed,
            bars_rejected=self.bars_rejected,
            signals_generated=self.signals_generated,
            intents_created=self.intents_created,
            decisions_made=self.decisions_made,
            orders_submitted=self.orders_submitted,
            fills_received=self.fills_received,
            state_saves=self.state_saves,
            restarts=self.restarts,
            report_failures=self.report_failures,
            last_bar_close_time=self.last_bar_close_time,
            last_processed_at=self.last_processed_at,
        )
