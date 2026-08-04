"""Deterministic, look-ahead-free backtesting engine.

Responsibilities: historical clock, event loop, next-bar execution, cost models, reports and
metrics.

:class:`~quantplatform.backtesting.engine.BacktestEngine` is the platform's only orchestrator.
It connects data, features, strategy, risk, execution and accounting in a fixed order, and no
component reaches around another: a strategy never sees the account, and only the risk engine
can authorise an order. Execution is next-bar — a decision taken from one bar's close is
matched against the following bar — so a run cannot fill at a price that printed before the
data it decided on.
"""

from __future__ import annotations

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.backtesting.metrics import (
    EquityPoint,
    PerformanceSummary,
    TradeStatistics,
)
from quantplatform.backtesting.results import BacktestResult, BarOutcome, ComponentCallCounts

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "BarOutcome",
    "ComponentCallCounts",
    "EquityPoint",
    "PerformanceSummary",
    "TradeStatistics",
]
