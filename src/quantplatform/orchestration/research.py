"""Wiring an engine for an experiment definition.

The harness declares what it needs and this satisfies it. The split is not stylistic: building
an engine means importing strategies, risk and execution in one place, which is what makes a
package an orchestrator, and the architecture permits exactly one answer to "what happens in
what order". So the research package describes and measures, and composition happens here —
where it already happens for paper trading.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.enums import ExecutionMode
from quantplatform.core.interfaces import FeaturePipeline
from quantplatform.core.models.portfolio import Balance
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.execution.config import ExecutionConfig
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.risk.engine import StandardRiskEngine
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.registry import StrategyRegistry

__all__ = ["ExperimentEngineFactory", "code_revision", "load_definition"]


class ExperimentEngineFactory:
    """Builds a fresh, isolated engine for each experiment.

    Every run gets its own portfolio and broker. Two experiments sharing either would be two
    runs over one account, and the second would inherit whatever the first left behind —
    which is exactly the contamination the identifier is supposed to rule out.
    """

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        features_for: Callable[[BaseStrategy], FeaturePipeline],
        quote_asset: str,
    ) -> None:
        """Wire the registry an experiment's ``strategy_id`` is resolved against.

        Args:
            registry: Where a definition's ``strategy_id`` becomes code. A definition
                names a strategy and never imports one, and the registry is what already
                knows how to validate its parameters against the declared schema — building
                the instance here instead would be a second, weaker copy of that check.
            features_for: Builds the pipeline a resolved strategy declares it needs.
            quote_asset: Asset the account is denominated in.
        """
        self._registry = registry
        self._features_for = features_for
        self._quote_asset = quote_asset

    def __call__(self, definition: ExperimentDefinition) -> BacktestEngine:
        """Return an engine configured exactly as the definition describes.

        The venue's trading rules come from the definition, never from a live lookup. A
        backtest over last year that asked the exchange for today's filters would be a
        different run every time it was repeated, and the ledger would report the difference
        as the engine's fault.

        Raises:
            StrategyNotFoundError: If the definition names a strategy the registry does not
                hold.
            StrategyParameterError: If its parameters fail the strategy's declared schema.
                Both are raised rather than defaulted: an experiment that silently ran
                something other than what it named would poison every comparison it entered.
        """
        strategy = self._registry.create(
            definition.strategy.strategy_id, dict(definition.strategy.params)
        )
        symbols = {definition.dataset.symbol: definition.dataset.symbol_rules}
        capital = definition.backtest.initial_capital
        portfolio = SpotPortfolioEngine(
            quote_asset=self._quote_asset,
            symbols=symbols,
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
            symbols=symbols,
            portfolio=portfolio,
            execution_mode=ExecutionMode.BACKTEST,
            started_at=definition.dataset.start,
            config=ExecutionConfig(policy=definition.risk.execution_policy),
            source=definition.experiment_id,
        )
        return BacktestEngine(
            config=definition.backtest,
            strategy=strategy,
            features=self._features_for(strategy),
            risk_engine=StandardRiskEngine(config=definition.risk),
            broker=broker,
            portfolio=portfolio,
            symbols=symbols,
        )


_GIT_TIMEOUT_SECONDS = 10


def code_revision(repo_root: Path) -> str | None:
    """Return the revision of the code being run, or ``None`` when it cannot be established.

    Lives here because the research package performs no I/O of its own and does not know that
    git exists; it receives a string. This is the only place in the platform that shells out
    to a version-control system, and it is deliberately the narrowest possible use of one.

    A dirty working tree is reported as such, and that suffix is the point. A result produced
    from a tree nobody can reconstruct is not reproducible, and without saying so the checker
    would report reproducibility failures whose real cause was uncommitted code.

    Args:
        repo_root: Directory to interrogate.

    Returns:
        The commit hash, suffixed ``-dirty`` when the tree carries uncommitted changes, or
        ``None`` when there is no repository or git cannot be run. Never raises and never
        guesses: an unknown revision is recorded as unknown, and a fabricated one would make
        every later comparison worthless.
    """

    def _git(*args: str) -> str | None:
        try:
            completed = subprocess.run(  # noqa: S603
                ["git", *args],  # noqa: S607
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip()

    head = _git("rev-parse", "HEAD")
    if head is None:
        return None
    status = _git("status", "--porcelain")
    if status is None:
        return None
    return f"{head}-dirty" if status else head


def load_definition(path: Path) -> ExperimentDefinition:
    """Read an experiment definition from disk.

    Raises:
        ValueError: If the file is not a complete definition. Refused rather than patched
            with defaults: an experiment that silently ran something other than what its
            file described would poison every comparison it entered.
    """
    return ExperimentDefinition.model_validate_json(path.read_text(encoding="utf-8"))
