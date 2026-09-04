"""What a paper session looks like from outside, gathered from what it already wrote.

Read-only by construction, and structurally so: this domain may import ``core``, ``config``,
``storage``, ``reporting`` and ``strategies``, and nothing else. It cannot reach ``execution``,
``risk``, ``portfolio``, ``paper`` or ``orchestration``, so there is no path from a status
command into a trading decision — the boundary test enforces that rather than a promise in a
docstring.

**Nothing here is computed that the platform does not already compute.** Cash comes from the
balances the session persisted, unrealised profit from :meth:`Position.unrealized_pnl`, the
day's activity from the daily report the session itself wrote. Where a number exists nowhere —
strategy signal counts, live feed state — the answer is ``None`` and the renderer says N/A,
because a status display that invents a figure is worse than one that admits a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantplatform.config.settings import Settings
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.portfolio import Position
from quantplatform.core.models.risk import CircuitBreakerState, PositionRiskState
from quantplatform.reporting.config import ReportingConfiguration
from quantplatform.reporting.models import DailyReport
from quantplatform.reporting.writer import DailyReportWriter
from quantplatform.storage.paper_state import FilePaperStateRepository
from quantplatform.storage.session_lock import SessionLockRecord, read_session_lock
from quantplatform.strategies.registry import StrategyRegistry

__all__ = [
    "Health",
    "SessionStatus",
    "gather_status",
]


class Health:
    """The four words a status line is allowed to lead with."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class SessionStatus:
    """One session, as far as its own persisted record can describe it.

    Every field that could be unknown is optional, and unknown is spelled ``None`` rather
    than zero. The distinction matters: no reconnects and no way to tell are different
    answers, and a dashboard that renders both as ``0`` is lying about the second.
    """

    # --- identity ---------------------------------------------------------------------
    health: str
    running: bool
    session_id: str | None
    strategy_id: str | None
    strategy_parameters: dict[str, object]
    execution_mode: str | None
    started_at: datetime | None
    saved_at: datetime | None
    restarts: int | None

    # --- market ------------------------------------------------------------------------
    symbols: tuple[str, ...]
    timeframe: str
    last_bar: MarketBar | None
    bars_processed: int | None
    required_history: int | None

    # --- portfolio ----------------------------------------------------------------------
    quote_asset: str
    starting_capital: Decimal | None
    cash: Decimal | None
    equity: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    total_fees: Decimal | None

    # --- position ------------------------------------------------------------------------
    open_positions: tuple[Position, ...]
    position_risk: tuple[PositionRiskState, ...]

    # --- risk ---------------------------------------------------------------------------
    risk_v2_active: bool
    stop_required: bool
    risk_per_trade_pct: Decimal | None
    breakers: tuple[CircuitBreakerState, ...]

    # --- activity (from the day's own report) ----------------------------------------------
    report: DailyReport | None

    # --- diagnosis ---------------------------------------------------------------------------
    notes: tuple[str, ...]
    """Anything an operator needs told in words: no snapshot yet, a dead lock holder, a
    state file that would not parse. Never silently swallowed."""

    state_present: bool
    lock: SessionLockRecord | None

    @property
    def warmup_complete(self) -> bool | None:
        """Whether enough history has been seen for the strategy to be allowed an opinion."""
        if self.bars_processed is None or self.required_history is None:
            return None
        return self.bars_processed >= self.required_history

    @property
    def elapsed(self) -> timedelta | None:
        """How long the session has been alive, by its own start time."""
        if self.started_at is None:
            return None
        return _now() - self.started_at

    @property
    def marked_at(self) -> Decimal | None:
        """The price open positions are marked at: the last closed bar, or nothing."""
        return None if self.last_bar is None else self.last_bar.close


def _now() -> datetime:
    """Return the current instant in UTC.

    A free function rather than an injected clock: this domain observes and never decides,
    so there is no trading behaviour for a wall-clock read to make irreproducible, and a
    status display asked "how long has this been running" must answer about now.
    """
    return datetime.now(UTC)


def gather_status(
    settings: Settings,
    *,
    registry: StrategyRegistry,
    session_id: str | None = None,
) -> SessionStatus:
    """Read everything a running session has written about itself.

    Opens files for reading and nothing else. No lock is claimed, no state is written, no
    connection is made — the command that calls this cannot start, stop or alter a session
    even by accident, because it holds nothing capable of it.

    Args:
        settings: Effective configuration, for the directories and the declared strategy.
        registry: Registry the configured strategy identifier is described from. Metadata
            only; no strategy is constructed, so a status read works even for a deployment
            whose parameters are currently unconfigurable.
        session_id: Which session to describe. Defaults to the configured one, unless a
            different session holds the lock — a running session is what an operator asking
            "how is it going" means, even if configuration has since moved on.

    Returns:
        The gathered status.
    """
    paper = settings.paper
    state_directory = paper.state_directory
    notes: list[str] = []
    problems: list[str] = []

    lock = _read_lock(state_directory, notes, problems)
    resolved_id = _resolve_session_id(session_id, lock, paper.session_id, notes)
    running = lock is not None and lock.is_alive and lock.session_id == resolved_id

    state = _load_state(state_directory, resolved_id, notes, problems)
    report = _load_report(settings, notes, problems)

    strategy_id = state.strategy_id if state is not None else paper.strategy_id
    required_history = _required_history(registry, strategy_id, notes)

    starting_capital = settings.backtest.initial_capital
    cash = _cash(state)
    positions = tuple(position for position in state.positions if position.is_open) if state else ()
    mark = state.last_bar.close if state is not None and state.last_bar is not None else None
    unrealized = _unrealized(positions, mark) if state is not None else None
    equity = _equity(cash, positions, mark)

    if state is not None and positions and mark is None:
        notes.append(
            "an open position cannot be marked: no closed bar has been recorded yet, so "
            "equity and unrealised profit are unknown rather than assumed flat"
        )

    return SessionStatus(
        health=_health(running=running, state=state, problems=problems),
        running=running,
        session_id=resolved_id,
        strategy_id=strategy_id,
        strategy_parameters=dict(paper.strategy_params),
        execution_mode=state.execution_mode.value if state else settings.execution_mode.value,
        started_at=state.started_at if state else (lock.started_at if lock else None),
        saved_at=state.saved_at if state else None,
        restarts=state.restarts if state else None,
        symbols=tuple(paper.symbols),
        timeframe=paper.timeframe.value,
        last_bar=state.last_bar if state else None,
        bars_processed=state.bars_processed if state else None,
        required_history=required_history,
        quote_asset=state.quote_asset if state else settings.market.quote_asset,
        starting_capital=starting_capital,
        cash=cash,
        equity=equity,
        realized_pnl=state.realized_pnl if state else None,
        unrealized_pnl=unrealized,
        total_fees=state.total_fees if state else None,
        open_positions=positions,
        position_risk=state.position_risk if state else (),
        risk_v2_active=settings.risk.risk_per_trade_pct is not None,
        stop_required=settings.risk.risk_per_trade_pct is not None,
        risk_per_trade_pct=settings.risk.risk_per_trade_pct,
        breakers=state.breakers if state else (),
        report=report,
        notes=tuple(notes),
        state_present=state is not None,
        lock=lock,
    )


def _read_lock(directory: Path, notes: list[str], problems: list[str]) -> SessionLockRecord | None:
    """Return the lock holder, treating an unreadable lock as absent but said aloud."""
    try:
        record = read_session_lock(directory)
    except Exception as exc:
        message = (
            f"the session lock could not be read ({type(exc).__name__}); treating it as "
            "absent, which may understate what is running"
        )
        notes.append(message)
        problems.append(message)
        return None
    if record is not None and not record.is_alive:
        _both(
            notes,
            problems,
            f"a lock file names process {record.pid}, which is no longer running: the "
            "session died without releasing it, or the pid has since been reused",
        )
    return record


def _both(notes: list[str], problems: list[str], message: str) -> None:
    """Record something that is both worth saying and worth colouring the banner for."""
    notes.append(message)
    problems.append(message)


def _resolve_session_id(
    requested: str | None,
    lock: SessionLockRecord | None,
    configured: str,
    notes: list[str],
) -> str:
    """Decide which session is being asked about.

    An explicit request wins. Otherwise a live lock wins over configuration, because an
    operator asking about "the session" means the one that is running, and reporting the
    configured name while a differently-named session holds the lock is how a status display
    comes to describe something that is not there.
    """
    if requested is not None:
        return requested
    if lock is not None and lock.session_id != configured:
        notes.append(
            f"the running session is {lock.session_id!r}, which is not the configured "
            f"{configured!r}; reporting on the running one"
        )
        return lock.session_id
    return configured


def _load_state(
    directory: Path, session_id: str, notes: list[str], problems: list[str]
) -> PaperSessionState | None:
    """Load the persisted snapshot, reporting a refusal rather than raising through it."""
    try:
        state = FilePaperStateRepository(directory).load(session_id)
    except Exception as exc:
        _both(
            notes,
            problems,
            f"the persisted state for {session_id!r} could not be read "
            f"({type(exc).__name__}: {exc}); every figure it would have supplied is unknown",
        )
        return None
    if state is None:
        notes.append(
            f"no snapshot has been written for {session_id!r} yet: a session persists one "
            "after it finishes a bar, so this is expected before the first candle closes"
        )
    return state


def _load_report(settings: Settings, notes: list[str], problems: list[str]) -> DailyReport | None:
    """Return today's report, or the most recent one, or nothing."""
    try:
        writer = DailyReportWriter(
            config=ReportingConfiguration(output_directory=settings.paper.reports_directory)
        )
        today = _now().date()
        report = writer.read(today)
        if report is None:
            report = writer.read_previous(today)
    except Exception as exc:
        _both(
            notes,
            problems,
            f"daily reports could not be read ({type(exc).__name__}); activity counts are unknown",
        )
        return None
    return report


def _required_history(
    registry: StrategyRegistry, strategy_id: str | None, notes: list[str]
) -> int | None:
    """Return how many bars the strategy needs before it may have an opinion."""
    if strategy_id is None:
        return None
    try:
        return registry.metadata_for(strategy_id).required_history
    except Exception:
        notes.append(
            f"strategy {strategy_id!r} is not registered here, so its warm-up "
            "requirement is unknown"
        )
        return None


def _cash(state: PaperSessionState | None) -> Decimal | None:
    """Return spendable and reserved quote currency, as the session recorded it."""
    if state is None:
        return None
    return sum(
        (balance.total for balance in state.balances if balance.asset == state.quote_asset),
        Decimal(0),
    )


def _unrealized(positions: tuple[Position, ...], mark: Decimal | None) -> Decimal | None:
    """Return open profit at the last closed bar, using the position's own calculation."""
    if not positions:
        return Decimal(0)
    if mark is None:
        return None
    return sum((position.unrealized_pnl(mark) for position in positions), Decimal(0))


def _equity(
    cash: Decimal | None, positions: tuple[Position, ...], mark: Decimal | None
) -> Decimal | None:
    """Return cash plus what open positions are worth at the last closed bar."""
    if cash is None:
        return None
    if not positions:
        return cash
    if mark is None:
        return None
    return cash + sum((position.market_value(mark) for position in positions), Decimal(0))


def _health(*, running: bool, state: PaperSessionState | None, problems: list[str]) -> str:
    """Summarise the session in one word, erring towards saying something is wrong.

    A latched breaker is FAILED even while the process lives, because the session has
    stopped being allowed to trade and a green banner over that would be the single most
    misleading thing this command could print.

    Judged on problems, never on notes. "No snapshot yet" is a note — it is the correct and
    expected state of a session that started four minutes ago — and letting it colour the
    banner amber would train an operator to ignore the banner.

    A problem outranks a clean stop, which is why DEGRADED is checked before STOPPED. A lock
    left behind by a process that died is not a session that stopped; calling it STOPPED
    would file a crash under the same word as a deliberate shutdown.
    """
    if state is not None and state.breakers:
        return Health.FAILED
    if problems:
        return Health.DEGRADED
    if not running:
        return Health.STOPPED
    return Health.HEALTHY
