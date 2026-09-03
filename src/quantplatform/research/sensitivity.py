"""Running a strategy's neighbourhood, not its optimum.

A sensitivity sweep answers one question: does this configuration's result depend on the
exact numbers chosen, or would something nearby have said roughly the same thing? That
question has no "best" in it. The base is not a contestant among its variants — it is the
point the variants are measured *around* — and every variant, whatever it produced, is kept.

Deliberately narrow. A variation is a complete parameter set, never a delta, so there is no
merge logic that could leave a stale value from the base sitting inside a definition that
claims to have changed it. And every variation runs over the base's own bars: this measures
sensitivity to configuration, not to a different slice of the market — a dataset override is
a different question, not asked here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Self

from pydantic import model_validator

from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.canonical import canonical_json
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.ledger import VariationKind
from quantplatform.research.plan_runner import FATAL_ERRORS
from quantplatform.research.robustness import (
    DistributionSummary,
    VariationRun,
    summarise_variations,
)
from quantplatform.research.runner import BacktestFactory, ExperimentRunner

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar
    from quantplatform.research.ledger import ExperimentLedger
    from quantplatform.research.store import ResultStore

__all__ = ["SensitivityOutcome", "SensitivityPlan", "SensitivityRunner", "SensitivityVariation"]

_PLAN_ID_LENGTH = 32


class SensitivityVariation(DomainModel):
    """A complete parameter set to run instead of the base strategy's own.

    Whole, not partial. A delta would need to say what happens to every parameter it does not
    mention, and "keeps the base's value" is a merge rule this model refuses to need: nothing
    here is ambiguous about what a variant actually ran with.
    """

    params: tuple[tuple[Text, Text], ...]


class SensitivityPlan(DomainModel):
    """Every variation to run, declared before anything runs."""

    base_experiment_id: Text
    variations: tuple[SensitivityVariation, ...]

    @property
    def plan_id(self) -> str:
        """Return the identifier derived from the base experiment and every variation."""
        return hashlib.sha256(canonical_json(self).encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        """Check the plan holds at least one variation, none of them a duplicate.

        Raises:
            ValueError: If the plan is empty, or the same parameter set is declared twice —
                almost certainly a mistake in the plan, not a deliberate second look: a
                deliberate re-ask is what re-running an already-recorded experiment is for,
                and that is a ledger concept, not a plan one.
        """
        if not self.variations:
            msg = "a sensitivity plan needs at least one variation"
            raise ValueError(msg)
        seen = {variation.params for variation in self.variations}
        if len(seen) != len(self.variations):
            msg = "a sensitivity plan must not declare the same variation twice"
            raise ValueError(msg)
        return self


class SensitivityOutcome(DomainModel):
    """Everything a sensitivity plan produced, and whether it finished."""

    plan_id: Text
    baseline: VariationRun
    variations: tuple[VariationRun, ...] = ()
    aborted: bool = False
    abort_reason: Text | None = None

    def summarise(self) -> DistributionSummary:
        """Summarise the variations, never the baseline.

        The baseline is the point everything else is measured around, not one more
        observation in the spread — folding it into the distribution would understate how
        much a variant actually moved the result.

        Raises:
            ValueError: If the plan was aborted before every variation ran.
        """
        if self.aborted:
            msg = (
                "this plan was aborted before it finished and cannot be summarised: "
                f"{self.abort_reason}"
            )
            raise ValueError(msg)
        return summarise_variations(self.variations, expected_plan_id=self.plan_id)


class SensitivityRunner:
    """Runs a plan's baseline and every variation, recording all of them."""

    def __init__(self, *, runner: ExperimentRunner | None = None) -> None:
        """Wire a sensitivity runner over the single-experiment runner it delegates to."""
        self._runner = runner if runner is not None else ExperimentRunner()

    def run(
        self,
        base: ExperimentDefinition,
        plan: SensitivityPlan,
        *,
        bars: Sequence[MarketBar],
        factory: BacktestFactory,
        store: ResultStore,
        ledger: ExperimentLedger,
        code_revision: str | None = None,
    ) -> SensitivityOutcome:
        """Run the base configuration, then every variation, over the same bars.

        The baseline runs first and unconditionally, and is recorded whatever it produces —
        this is what makes "baseline always present" true of every plan, not merely of the
        ones that happened not to fail.

        Args:
            base: The configuration every variation is a change from.
            plan: The variations to run, already validated when the plan was built.
            bars: The closed bars every run consumes — the base's own dataset, unchanged.
            factory: Builds the engine for one definition.
            store: Where each attempt's evidence is written.
            ledger: Where each attempt is recorded, with its lineage.
            code_revision: Revision of the code being run, or ``None`` when unknown.

        Returns:
            What the baseline and every variation produced, and whether an integrity failure
            ended the plan early.
        """
        plan_id = plan.plan_id
        baseline_result = self._runner.run(
            base, bars=bars, factory=factory, code_revision=code_revision
        )
        baseline_entry = ledger.record(
            baseline_result,
            store=store,
            variation_kind=VariationKind.SENSITIVITY,
            variation_plan_id=plan_id,
        )
        baseline_run = VariationRun(entry=baseline_entry, result=baseline_result)

        if baseline_result.error_type in FATAL_ERRORS:
            return SensitivityOutcome(
                plan_id=plan_id,
                baseline=baseline_run,
                aborted=True,
                abort_reason=baseline_result.error or baseline_result.error_type,
            )

        runs: list[VariationRun] = []
        for variation in plan.variations:
            definition = base.model_copy(
                update={"strategy": base.strategy.model_copy(update={"params": variation.params})}
            )
            result = self._runner.run(
                definition, bars=bars, factory=factory, code_revision=code_revision
            )
            entry = ledger.record(
                result,
                store=store,
                derived_from=base.experiment_id,
                variation_kind=VariationKind.SENSITIVITY,
                variation_plan_id=plan_id,
            )
            runs.append(VariationRun(entry=entry, result=result))
            if result.error_type in FATAL_ERRORS:
                return SensitivityOutcome(
                    plan_id=plan_id,
                    baseline=baseline_run,
                    variations=tuple(runs),
                    aborted=True,
                    abort_reason=result.error or result.error_type,
                )

        return SensitivityOutcome(plan_id=plan_id, baseline=baseline_run, variations=tuple(runs))
