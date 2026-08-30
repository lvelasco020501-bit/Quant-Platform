"""Wiring an engine for an experiment definition.

The harness declares what it needs and this satisfies it. The split is not stylistic: building
an engine means importing strategies, risk and execution in one place, which is what makes a
package an orchestrator, and the architecture permits exactly one answer to "what happens in
what order". So the research package describes and measures, and composition happens here —
where it already happens for paper trading.
"""

from __future__ import annotations

from collections.abc import Mapping

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.enums import ExecutionMode
from quantplatform.core.interfaces import FeaturePipeline
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.portfolio import Balance
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.execution.config import ExecutionConfig
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.risk.engine import StandardRiskEngine
from quantplatform.strategies.base import BaseStrategy

__all__ = ["ExperimentEngineFactory"]


class ExperimentEngineFactory:
    """Builds a fresh, isolated engine for each experiment.

    Every run gets its own portfolio and broker. Two experiments sharing either would be two
    runs over one account, and the second would inherit whatever the first left behind —
    which is exactly the contamination the identifier is supposed to rule out.
    """

    def __init__(
        self,
        *,
        strategies: Mapping[str, type[BaseStrategy]],
        features: Mapping[str, FeaturePipeline],
        symbols: Mapping[str, SymbolRules],
        quote_asset: str,
    ) -> None:
        """Wire the registry an experiment's ``strategy_id`` is resolved against.

        Args:
            strategies: Strategy classes by registry identifier. A definition names a
                strategy; it never imports one, so this is where the name becomes code.
            features: Feature pipeline per strategy identifier.
            symbols: Venue rules for every symbol an experiment may reference.
            quote_asset: Asset the account is denominated in.
        """
        self._strategies = dict(strategies)
        self._features = dict(features)
        self._symbols = dict(symbols)
        self._quote_asset = quote_asset

    def __call__(self, definition: ExperimentDefinition) -> BacktestEngine:
        """Return an engine configured exactly as the definition describes.

        Raises:
            KeyError: If the definition names a strategy or a symbol this factory was not
                given. Raised rather than defaulted: an experiment that silently ran
                something other than what it named would poison every comparison it entered.
        """
        strategy_id = definition.strategy.strategy_id
        strategy = self._strategies[strategy_id](
            dict(definition.strategy.params)  # type: ignore[arg-type]
        )
        capital = definition.backtest.initial_capital
        portfolio = SpotPortfolioEngine(
            quote_asset=self._quote_asset,
            symbols=self._symbols,
            execution_mode=ExecutionMode.BACKTEST,
            initial_balances=(
                Balance(
                    asset=self._quote_asset,
                    free=capital,
                    locked=definition.backtest.initial_capital * 0,
                    updated_at=definition.dataset.start,
                ),
            ),
            source=definition.experiment_id,
        )
        broker = SimulatedBroker(
            symbols=self._symbols,
            portfolio=portfolio,
            execution_mode=ExecutionMode.BACKTEST,
            started_at=definition.dataset.start,
            config=ExecutionConfig(policy=definition.risk.execution_policy),
            source=definition.experiment_id,
        )
        return BacktestEngine(
            config=definition.backtest,
            strategy=strategy,
            features=self._features[strategy_id],
            risk_engine=StandardRiskEngine(config=definition.risk),
            broker=broker,
            portfolio=portfolio,
            symbols=self._symbols,
        )
