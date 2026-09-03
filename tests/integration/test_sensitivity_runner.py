"""Running a base configuration's neighbourhood, and keeping every variant.

The baseline is not a contestant among its variants — it is the point they are measured
around — so it always runs, always gets recorded, and never enters the distribution its own
variants are summarised into.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.errors import DataIntegrityError, StrategyError
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import BacktestFactory
from quantplatform.research.sensitivity import (
    SensitivityOutcome,
    SensitivityPlan,
    SensitivityRunner,
    SensitivityVariation,
)
from quantplatform.research.store import ResultStore
from tests.factories import make_bars, make_experiment_definition, make_research_factory


def _plan(base_id: str) -> SensitivityPlan:
    return SensitivityPlan(
        base_experiment_id=base_id,
        variations=(
            SensitivityVariation(params=(("x", "1"),)),
            SensitivityVariation(params=(("x", "2"),)),
            SensitivityVariation(params=(("x", "3"),)),
        ),
    )


def _run(
    tmp_path: Path, *, factory: BacktestFactory | None = None, plan: SensitivityPlan | None = None
) -> SensitivityOutcome:
    base = make_experiment_definition(strategy_id="buy_then_sell")
    bars = make_bars([Decimal(50_000)] * 32)
    return SensitivityRunner().run(
        base,
        plan if plan is not None else _plan(base.experiment_id),
        bars=bars,
        factory=factory if factory is not None else make_research_factory(),
        store=ResultStore(tmp_path / "results"),
        ledger=ExperimentLedger(tmp_path / "ledger.jsonl"),
        code_revision="abc1234",
    )


def test_the_baseline_and_every_variation_are_run(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    assert outcome.baseline.result.status is ExperimentStatus.SUCCEEDED
    assert len(outcome.variations) == 3


def test_entries_are_recorded_in_the_declared_order(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    declared = [dict(variation.params)["x"] for variation in _plan("x").variations]
    recorded = [dict(run.result.definition.strategy.params)["x"] for run in outcome.variations]
    assert recorded == declared


def test_lineage_is_recorded_on_every_line(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    assert outcome.baseline.entry.derived_from is None
    assert outcome.baseline.entry.variation_kind is VariationKind.SENSITIVITY
    assert outcome.baseline.entry.variation_plan_id == outcome.plan_id
    for run in outcome.variations:
        assert run.entry.derived_from == outcome.baseline.entry.experiment_id
        assert run.entry.variation_kind is VariationKind.SENSITIVITY
        assert run.entry.variation_plan_id == outcome.plan_id


def test_a_local_failure_does_not_stop_the_plan(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(definition: ExperimentDefinition) -> BacktestEngine:
        calls["n"] += 1
        if calls["n"] == 3:  # the base runs first (call 1); this hits the second variation
            raise StrategyError("this variation could not be built")
        return make_research_factory()(definition)

    outcome = _run(tmp_path, factory=flaky)

    statuses = [run.result.status for run in outcome.variations]
    assert statuses.count(ExperimentStatus.FAILED) == 1
    assert len(outcome.variations) == 3


def test_a_failure_of_integrity_aborts_the_plan_after_recording_the_failure(
    tmp_path: Path,
) -> None:
    def corrupt(definition: ExperimentDefinition) -> BacktestEngine:
        del definition
        raise DataIntegrityError("the position could not be reconciled")

    outcome = _run(tmp_path, factory=corrupt)

    assert outcome.aborted is True
    # The baseline itself hits the corrupt factory first and is still recorded.
    assert outcome.baseline.result.status is ExperimentStatus.FAILED
    assert len(outcome.variations) == 0


def test_an_aborted_plan_refuses_to_summarise_itself(tmp_path: Path) -> None:
    def corrupt(definition: ExperimentDefinition) -> BacktestEngine:
        del definition
        raise DataIntegrityError("the position could not be reconciled")

    outcome = _run(tmp_path, factory=corrupt)

    with pytest.raises(ValueError, match="aborted"):
        outcome.summarise()


def test_the_summary_never_includes_the_baseline(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    summary = outcome.summarise()

    assert summary.count_total == 3  # the three variations, not four


def test_a_plan_rejects_the_same_variation_declared_twice(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="twice"):
        SensitivityPlan(
            base_experiment_id="x",
            variations=(
                SensitivityVariation(params=(("x", "1"),)),
                SensitivityVariation(params=(("x", "1"),)),
            ),
        )


def test_a_plan_needs_at_least_one_variation() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SensitivityPlan(base_experiment_id="x", variations=())


def test_two_variations_with_different_params_get_different_experiment_ids(
    tmp_path: Path,
) -> None:
    outcome = _run(tmp_path)

    ids = {run.result.experiment_id for run in outcome.variations}
    assert len(ids) == 3


def test_a_variation_identical_to_the_base_shares_its_experiment_id(tmp_path: Path) -> None:
    base = make_experiment_definition(strategy_id="buy_then_sell")
    plan = SensitivityPlan(
        base_experiment_id=base.experiment_id,
        variations=(SensitivityVariation(params=base.strategy.params),),
    )
    bars = make_bars([Decimal(50_000)] * 32)

    outcome = SensitivityRunner().run(
        base,
        plan,
        bars=bars,
        factory=make_research_factory(),
        store=ResultStore(tmp_path / "results"),
        ledger=ExperimentLedger(tmp_path / "ledger.jsonl"),
        code_revision="abc1234",
    )

    assert outcome.variations[0].result.experiment_id == outcome.baseline.result.experiment_id
