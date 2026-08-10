"""Strategy contract and registry.

Strategies are pure decision functions over closed market data. They may not download
data, read balances, submit orders, access credentials, write to the database, modify the
portfolio or observe the execution mode. Those restrictions are enforced structurally: this
package is only permitted to import :mod:`quantplatform.core`, which the dependency
boundary tests verify on every run.
"""

from __future__ import annotations

from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.ema_trend import EmaTrendParameters, EmaTrendStrategy
from quantplatform.strategies.registry import (
    BUILTIN_STRATEGIES,
    StrategyRegistry,
    build_default_registry,
)

__all__ = [
    "BUILTIN_STRATEGIES",
    "BaseStrategy",
    "EmaTrendParameters",
    "EmaTrendStrategy",
    "StrategyRegistry",
    "build_default_registry",
]
