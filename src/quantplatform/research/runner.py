"""Running an experiment without deciding how the platform is wired.

Building a backtest engine means importing strategies, risk and execution together, which is
the definition of an orchestrator — and the architecture allows exactly one. So the harness
does not build one. It declares the shape of the thing it needs and a composition root
supplies it, which is how every other boundary in this codebase already works.

A failure is a result. An experiment that raises says something about the configuration that
raised, and letting the exception escape would leave that fact recorded nowhere.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from quantplatform.core.errors import DatasetMismatchError, QuantPlatformError
from quantplatform.research.dataset import validate_dataset
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.digest import bars_digest
from quantplatform.research.result import ExperimentResult, ExperimentStatus

if TYPE_CHECKING:
    from quantplatform.backtesting.engine import BacktestEngine
    from quantplatform.core.models.market import MarketBar

__all__ = ["BacktestFactory", "ExperimentRunner"]


class BacktestFactory(Protocol):
    """Builds the engine one definition calls for.

    Implemented by a composition root, never here: satisfying it requires wiring the whole
    chain, and a second package doing that would mean two answers to "what happens in what
    order".
    """

    def __call__(self, definition: ExperimentDefinition) -> BacktestEngine:
        """Return an engine configured for this definition."""
        ...


class ExperimentRunner:
    """Turns a definition into a result, and never into an exception."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        """Wire a runner.

        Args:
            clock: Reads the wall time recorded on a result. Injected so a test can make the
                timestamps deterministic; they are excluded from the reproducible hash
                either way, so nothing about equality depends on this.
        """
        self._clock = clock

    def run(
        self,
        definition: ExperimentDefinition,
        *,
        bars: Sequence[MarketBar],
        factory: BacktestFactory,
        code_revision: str | None = None,
    ) -> ExperimentResult:
        """Run one experiment and describe what happened.

        Args:
            definition: What to run.
            bars: The closed bars to run it over.
            factory: Builds the engine; supplied by a composition root.
            code_revision: Revision of the code being run, or ``None`` when unknown.

        Returns:
            The result, with ``status`` reporting whether it completed. A failure carries its
            error rather than propagating: an experiment that blew up is evidence about the
            configuration that blew it up, and an exception would record that nowhere.
        """
        started_at = self._now()
        try:
            validated = validate_dataset(definition, bars)
        except DatasetMismatchError as exc:
            return ExperimentResult(
                definition=definition,
                code_revision=code_revision,
                status=ExperimentStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
                bars_digest=None,
                started_at=started_at,
                finished_at=self._now(),
            )
        digest = bars_digest(validated)
        try:
            engine = factory(definition)
            outcome = engine.run(validated)
        except (QuantPlatformError, ValueError, ArithmeticError) as exc:
            return ExperimentResult(
                definition=definition,
                code_revision=code_revision,
                status=ExperimentStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
                bars_digest=digest,
                started_at=started_at,
                finished_at=self._now(),
            )
        return ExperimentResult(
            definition=definition,
            code_revision=code_revision,
            status=ExperimentStatus.SUCCEEDED,
            bars_digest=digest,
            performance=outcome.performance,
            trades=tuple(outcome.trades),
            equity_curve=tuple(outcome.equity_curve),
            started_at=started_at,
            finished_at=self._now(),
        )

    def _now(self) -> datetime:
        """Return the instant to stamp, from the injected clock or the system one."""
        if self._clock is not None:
            return self._clock()
        return datetime.now(UTC)
