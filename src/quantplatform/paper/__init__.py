"""Paper trading: the backtesting pipeline, driven by a live market feed.

Identical chain, identical components, virtual money. The only difference from a backtest is
where the bars come from — which is precisely what makes paper mode worth running: anything
that breaks here would have broken with real money, and nothing that works here depended on
knowing the future.

There is no code path from this package to a venue. Orders go to the simulated broker and
settle into a virtual portfolio; only the market data is real.
"""

from __future__ import annotations

from quantplatform.paper.clock import SessionClock
from quantplatform.paper.results import (
    RuntimeMetrics,
    SessionResult,
    SessionSnapshot,
    SessionStatus,
)
from quantplatform.paper.runner import PaperTradingRunner
from quantplatform.paper.session import DayRolloverObserver, PaperTradingSession
from quantplatform.paper.state import InMemoryPaperStateRepository, restore_balances

__all__ = [
    "DayRolloverObserver",
    "InMemoryPaperStateRepository",
    "PaperTradingRunner",
    "PaperTradingSession",
    "RuntimeMetrics",
    "SessionClock",
    "SessionResult",
    "SessionSnapshot",
    "SessionStatus",
    "restore_balances",
]
