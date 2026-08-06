"""Immutable views of a paper session.

Three questions, three objects: what is the account doing right now
(:class:`SessionSnapshot`), how is the process itself holding up
(:class:`RuntimeMetrics`), and what has the whole session amounted to
(:class:`SessionResult`).

All three are frozen. A caller reading a snapshot while the session keeps running is looking
at a fixed instant, not at state that shifts underneath it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from quantplatform.backtesting.metrics import PerformanceSummary
from quantplatform.backtesting.results import BacktestResult
from quantplatform.core.enums import ExecutionMode
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.orders import Fill, Order
from quantplatform.core.models.portfolio import Balance, PortfolioSnapshot, Position

__all__ = ["RuntimeMetrics", "SessionResult", "SessionSnapshot", "SessionStatus"]


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """Where a session is in its lifecycle."""

    running: bool
    started_at: datetime | None
    stopped_at: datetime | None
    restarts: int

    @property
    def has_started(self) -> bool:
        """Return whether the session has ever begun."""
        return self.started_at is not None


@dataclass(frozen=True, slots=True)
class RuntimeMetrics:
    """How the session process itself is behaving, as distinct from how it is trading.

    Kept separate from :class:`~quantplatform.backtesting.metrics.PerformanceSummary` on
    purpose: a session can be flat and profitable while quietly dropping every third candle,
    and a single blended report would hide that.
    """

    uptime_seconds: float = 0.0
    bars_received: int = 0
    bars_processed: int = 0
    bars_rejected: int = 0
    """Bars refused before reaching the pipeline: still forming, out of order, or unknown."""

    signals_generated: int = 0
    intents_created: int = 0
    decisions_made: int = 0
    orders_submitted: int = 0
    fills_received: int = 0
    state_saves: int = 0
    restarts: int = 0
    last_bar_close_time: datetime | None = None
    last_processed_at: datetime | None = None

    @property
    def acceptance_rate(self) -> Decimal | None:
        """Return the share of received bars that reached the pipeline.

        ``None`` when nothing has arrived — a rate over zero observations is not zero, it is
        undefined, and reporting it as zero would read like a total feed failure.
        """
        if self.bars_received == 0:
            return None
        return Decimal(self.bars_processed) / Decimal(self.bars_received)


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """The account and the session at one instant."""

    taken_at: datetime
    status: SessionStatus
    equity: Decimal
    cash: Decimal
    balances: tuple[Balance, ...] = ()
    positions: tuple[Position, ...] = ()
    open_orders: tuple[Order, ...] = ()
    last_bar: MarketBar | None = None
    runtime: RuntimeMetrics = field(default_factory=RuntimeMetrics)
    portfolio: PortfolioSnapshot | None = None

    @property
    def open_position_count(self) -> int:
        """Return how many instruments currently carry exposure."""
        return sum(1 for position in self.positions if position.is_open)


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Everything a paper session produced, from its start to the moment it was read."""

    session_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    status: SessionStatus
    runtime: RuntimeMetrics
    snapshot: SessionSnapshot
    performance: PerformanceSummary | None = None
    """How the session has traded so far.

    A session that has not yet processed a bar reports an untouched account rather than
    ``None``, matching an empty backtest, so a caller never has to special-case a session
    that has only just started.
    """

    fills: tuple[Fill, ...] = ()
    orders: tuple[Order, ...] = ()
    detail: BacktestResult | None = None
    """The full pipeline record — signals, intents, decisions, events, equity curve.

    A paper session runs the identical chain a backtest does, so it produces the identical
    record. Exposing it directly means the same reporting and analysis works on both, rather
    than a parallel set of paper-only accessors that would drift.
    """
