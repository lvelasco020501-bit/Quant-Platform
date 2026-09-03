"""Running the same strategy over the same data under a different cost or risk assumption.

A stress scenario is not a different question — it is the same question asked again with
fees, slippage or a risk threshold moved. Nothing here touches the strategy or the dataset:
:func:`derive_stress_definition` only ever copies ``risk`` and/or ``backtest`` onto the base,
which is what makes it structurally impossible for a scenario to also change what strategy ran
or what bars it saw, whatever a scenario file happens to contain.

Fees and slippage are varied together, through ``risk.execution_policy``, because that is the
one object the risk engine and the broker already share — the composition root builds the
broker's execution config directly from it. Varying either independently would mean building a
second copy of that policy, which is exactly the drift
:mod:`quantplatform.core.models.execution_policy` exists to rule out.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Self

from pydantic import model_validator

from quantplatform.backtesting.config import BacktestConfig
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
from quantplatform.risk.config import RiskConfiguration

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar
    from quantplatform.research.ledger import ExperimentLedger
    from quantplatform.research.store import ResultStore

__all__ = [
    "StressOutcome",
    "StressPlan",
    "StressRunner",
    "StressScenario",
    "derive_stress_definition",
]

_PLAN_ID_LENGTH = 32


class StressScenario(DomainModel):
    """A complete risk and/or backtest configuration to run instead of the base's own.

    Whole objects, never a delta onto a sub-field: ``RiskConfiguration`` and
    ``BacktestConfig`` each carry their own coherence validators, and a scenario that replaced
    the whole object gets those validators applied automatically. A partial merge would have
    to reimplement that checking or risk producing a configuration that is only coherent
    because the merge happened to bypass it.
    """

    risk: RiskConfiguration | None = None
    backtest: BacktestConfig | None = None

    @model_validator(mode="after")
    def _validate_scenario(self) -> Self:
        """Check the scenario actually varies something.

        Raises:
            ValueError: If both ``risk`` and ``backtest`` are unset — a scenario that
                changes nothing is not a stress test of anything.
        """
        if self.risk is None and self.backtest is None:
            msg = "a stress scenario must vary risk, backtest, or both"
            raise ValueError(msg)
        return self


def derive_stress_definition(
    base: ExperimentDefinition, scenario: StressScenario
) -> ExperimentDefinition:
    """Return the base definition with only ``risk`` and/or ``backtest`` replaced.

    ``strategy`` and ``dataset`` are never in the update this builds — not merely undocumented
    as changeable, but absent from the only dict :meth:`~pydantic.BaseModel.model_copy` reads,
    so there is no key through which either could be touched.
    """
    update: dict[str, object] = {}
    if scenario.risk is not None:
        update["risk"] = scenario.risk
    if scenario.backtest is not None:
        update["backtest"] = scenario.backtest
    return base.model_copy(update=update)


class StressPlan(DomainModel):
    """Every scenario to run, declared before anything runs."""

    base_experiment_id: Text
    scenarios: tuple[StressScenario, ...]

    @property
    def plan_id(self) -> str:
        """Return the identifier derived from the base experiment and every scenario."""
        return hashlib.sha256(canonical_json(self).encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        """Check the plan holds at least one scenario.

        Raises:
            ValueError: If the plan is empty.
        """
        if not self.scenarios:
            msg = "a stress plan needs at least one scenario"
            raise ValueError(msg)
        return self


class StressOutcome(DomainModel):
    """Everything a stress plan produced, and whether it finished."""

    plan_id: Text
    baseline: VariationRun
    scenarios: tuple[VariationRun, ...] = ()
    aborted: bool = False
    abort_reason: Text | None = None

    def summarise(self) -> DistributionSummary:
        """Summarise the scenarios, never the baseline.

        See :meth:`~quantplatform.research.sensitivity.SensitivityOutcome.summarise` for why
        the baseline is excluded — the reasoning is identical.

        Raises:
            ValueError: If the plan was aborted before every scenario ran.
        """
        if self.aborted:
            msg = (
                "this plan was aborted before it finished and cannot be summarised: "
                f"{self.abort_reason}"
            )
            raise ValueError(msg)
        return summarise_variations(self.scenarios, expected_plan_id=self.plan_id)


class StressRunner:
    """Runs a plan's baseline and every scenario, recording all of them."""

    def __init__(self, *, runner: ExperimentRunner | None = None) -> None:
        """Wire a stress runner over the single-experiment runner it delegates to."""
        self._runner = runner if runner is not None else ExperimentRunner()

    def run(
        self,
        base: ExperimentDefinition,
        plan: StressPlan,
        *,
        bars: Sequence[MarketBar],
        factory: BacktestFactory,
        store: ResultStore,
        ledger: ExperimentLedger,
        code_revision: str | None = None,
    ) -> StressOutcome:
        """Run the base configuration, then every scenario, over the same bars.

        Args:
            base: The configuration every scenario varies the cost or risk assumptions of.
            plan: The scenarios to run, already validated when the plan was built.
            bars: The closed bars every run consumes — the base's own dataset, unchanged.
            factory: Builds the engine for one definition.
            store: Where each attempt's evidence is written.
            ledger: Where each attempt is recorded, with its lineage.
            code_revision: Revision of the code being run, or ``None`` when unknown.

        Returns:
            What the baseline and every scenario produced, and whether an integrity failure
            ended the plan early.
        """
        plan_id = plan.plan_id
        baseline_result = self._runner.run(
            base, bars=bars, factory=factory, code_revision=code_revision
        )
        baseline_entry = ledger.record(
            baseline_result,
            store=store,
            variation_kind=VariationKind.STRESS,
            variation_plan_id=plan_id,
        )
        baseline_run = VariationRun(entry=baseline_entry, result=baseline_result)

        if baseline_result.error_type in FATAL_ERRORS:
            return StressOutcome(
                plan_id=plan_id,
                baseline=baseline_run,
                aborted=True,
                abort_reason=baseline_result.error or baseline_result.error_type,
            )

        runs: list[VariationRun] = []
        for scenario in plan.scenarios:
            definition = derive_stress_definition(base, scenario)
            result = self._runner.run(
                definition, bars=bars, factory=factory, code_revision=code_revision
            )
            entry = ledger.record(
                result,
                store=store,
                derived_from=base.experiment_id,
                variation_kind=VariationKind.STRESS,
                variation_plan_id=plan_id,
            )
            runs.append(VariationRun(entry=entry, result=result))
            if result.error_type in FATAL_ERRORS:
                return StressOutcome(
                    plan_id=plan_id,
                    baseline=baseline_run,
                    scenarios=tuple(runs),
                    aborted=True,
                    abort_reason=result.error or result.error_type,
                )

        return StressOutcome(plan_id=plan_id, baseline=baseline_run, scenarios=tuple(runs))
