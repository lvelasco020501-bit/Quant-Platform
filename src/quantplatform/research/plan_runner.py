"""Walking a plan's folds, and keeping every one of them.

The models and the summary existed; nothing walked the folds, so lineage was a parameter
nobody passed and a plan was a contract with no execution behind it.

Two policies shape this and they pull against each other. A fold that fails is still a fold,
and abandoning the plan on the first one would turn ten observations into one — so the run
continues. But a failure of *integrity* says the platform can no longer describe its own
state, and carrying on would manufacture folds whose meaning nobody could defend while the
summary they fed looked exactly as trustworthy as a real one. The difference is typed rather
than inferred from a bare ``Exception``: deciding financial semantics by catching everything
is how the second case quietly becomes the first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from quantplatform.core.errors import (
    DataIntegrityError,
    DatasetMismatchError,
    PositionRiskAmbiguityError,
    PositionRiskUnavailableError,
)
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.aggregate import FoldRun, WalkForwardSummary, aggregate_walk_forward
from quantplatform.research.definition import ExperimentDefinition, ExperimentRole
from quantplatform.research.folds import WalkForwardPlan, WindowSpec, derive_fold_definitions
from quantplatform.research.runner import BacktestFactory, ExperimentRunner

if TYPE_CHECKING:
    from quantplatform.core.enums import MarketType, Timeframe
    from quantplatform.core.models.market import MarketBar
    from quantplatform.research.ledger import ExperimentLedger
    from quantplatform.research.store import ResultStore

__all__ = ["BarLoader", "WalkForwardOutcome", "WalkForwardRunner"]

FATAL_ERRORS: frozenset[str] = frozenset(
    error.__name__
    for error in (
        DataIntegrityError,
        DatasetMismatchError,
        PositionRiskAmbiguityError,
        PositionRiskUnavailableError,
    )
)
"""Failures that end a plan rather than a fold.

Each one says the platform cannot describe its own state — a position it cannot account for,
a record that contradicts itself, or a loader serving bars that are not the dataset a fold
asked for. That last one belongs here specifically because the loader is shared across every
fold of a plan: a mismatch in fold 3 means the same broken loader is about to be asked for
fold 4, and every later fold would run against that same unexplained state. The honest
outcome is a short plan with a reason rather than a full one nobody can defend. Listed
explicitly so that adding to it is a decision someone makes on purpose, and matched by name
rather than caught, so the fold that failed is *recorded* before the plan stops — an aborted
plan whose final fold left no evidence would be the least useful of all.
"""


class BarLoader(Protocol):
    """Supplies the bars a window covers.

    A port, because loading bars is I/O and this package performs none. The composition root
    reads them from the repository; a test reads them from a list.
    """

    def __call__(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        window: WindowSpec,
    ) -> Sequence[MarketBar]:
        """Return the bars whose open time falls inside the window."""
        ...


class WalkForwardOutcome(DomainModel):
    """Everything a plan produced, and whether it finished."""

    plan_id: Text
    folds: tuple[FoldRun, ...] = ()
    aborted: bool = False
    abort_reason: Text | None = None

    def summarise(self, *, require_complete: bool = True) -> WalkForwardSummary:
        """Summarise the plan's test folds.

        Raises:
            ValueError: If the plan was aborted. A summary of the folds that happened to run
                before the platform lost track of its own state would read exactly like a
                summary of a plan that completed, and nothing in the numbers would say
                otherwise.
        """
        if self.aborted:
            msg = (
                "this plan was aborted before it finished and cannot be summarised: "
                f"{self.abort_reason}"
            )
            raise ValueError(msg)
        tested = tuple(
            run
            for run in self.folds
            if run.result.definition.role is ExperimentRole.WALK_FORWARD_TEST
        )
        return aggregate_walk_forward(tested, require_complete=require_complete)


class WalkForwardRunner:
    """Runs every fold of a plan and records all of them."""

    def __init__(self, *, runner: ExperimentRunner | None = None) -> None:
        """Wire a plan runner over the single-experiment runner it delegates to."""
        self._runner = runner if runner is not None else ExperimentRunner()

    def run(
        self,
        base: ExperimentDefinition,
        plan: WalkForwardPlan,
        *,
        loader: BarLoader,
        factory: BacktestFactory,
        store: ResultStore,
        ledger: ExperimentLedger,
        code_revision: str | None = None,
    ) -> WalkForwardOutcome:
        """Run the plan fold by fold, recording each result as it completes.

        Training and test windows run the *same* configuration. Nothing is selected, nothing
        is fitted, and nothing passes from the first window to the second — which is what
        makes leakage structurally impossible here rather than a thing to be watched for.

        Aggregation happens only after the plan finishes, and only when asked: a running total
        would invite reading the plan while it was still deciding what it said.

        Args:
            base: The configuration every fold shares.
            plan: The windows, already validated when the plan was built.
            loader: Supplies each window's bars.
            factory: Builds the engine for one definition.
            store: Where each attempt's evidence is written.
            ledger: Where each attempt is recorded, with its lineage.
            code_revision: Revision of the code being run, or ``None`` when unknown.

        Returns:
            What every fold produced, and whether an integrity failure ended the plan early.
        """
        runs: list[FoldRun] = []
        for fold, (train, test) in zip(
            plan.folds, derive_fold_definitions(base, plan), strict=True
        ):
            for definition, window in ((train, fold.train), (test, fold.test)):
                bars = loader(
                    symbol=definition.dataset.symbol,
                    market_type=definition.dataset.market_type,
                    timeframe=definition.dataset.timeframe,
                    window=window,
                )
                result = self._runner.run(
                    definition, bars=bars, factory=factory, code_revision=code_revision
                )
                entry = ledger.record(
                    result, store=store, plan_id=plan.plan_id, fold_index=fold.index
                )
                runs.append(FoldRun(entry=entry, result=result))
                if result.error_type in FATAL_ERRORS:
                    return WalkForwardOutcome(
                        plan_id=plan.plan_id,
                        folds=tuple(runs),
                        aborted=True,
                        abort_reason=result.error or result.error_type,
                    )
        return WalkForwardOutcome(plan_id=plan.plan_id, folds=tuple(runs))
