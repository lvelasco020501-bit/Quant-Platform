"""Running the same strategy over the same data under a different cost assumption.

`derive_stress_definition` cannot touch `strategy` or `dataset` — not by convention, by
construction — and every test here that could catch a slip checks that directly rather than
trusting the docstring.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.enums import CommissionModel
from quantplatform.core.errors import DataIntegrityError, StrategyError
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import BacktestFactory
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import (
    StressOutcome,
    StressPlan,
    StressRunner,
    StressScenario,
    derive_stress_definition,
)
from tests.factories import (
    make_bars,
    make_execution_policy,
    make_experiment_definition,
    make_research_factory,
)


def _fee_scenario(base: ExperimentDefinition, fee_bps: Decimal) -> StressScenario:
    stressed_policy = make_execution_policy(
        fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=fee_bps
    )
    return StressScenario(risk=base.risk.model_copy(update={"execution_policy": stressed_policy}))


def _plan(base: ExperimentDefinition) -> StressPlan:
    return StressPlan(
        base_experiment_id=base.experiment_id,
        scenarios=(_fee_scenario(base, Decimal(10)), _fee_scenario(base, Decimal(50))),
    )


def _run(
    tmp_path: Path, *, factory: BacktestFactory | None = None, plan: StressPlan | None = None
) -> StressOutcome:
    base = make_experiment_definition(strategy_id="buy_then_sell")
    bars = make_bars([Decimal(50_000)] * 32)
    return StressRunner().run(
        base,
        plan if plan is not None else _plan(base),
        bars=bars,
        factory=factory if factory is not None else make_research_factory(),
        store=ResultStore(tmp_path / "results"),
        ledger=ExperimentLedger(tmp_path / "ledger.jsonl"),
        code_revision="abc1234",
    )


def test_the_baseline_and_every_scenario_are_run(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    assert outcome.baseline.result.status is ExperimentStatus.SUCCEEDED
    assert len(outcome.scenarios) == 2


def test_lineage_is_recorded_on_every_line(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    assert outcome.baseline.entry.derived_from is None
    assert outcome.baseline.entry.variation_kind is VariationKind.STRESS
    for run in outcome.scenarios:
        assert run.entry.derived_from == outcome.baseline.entry.experiment_id
        assert run.entry.variation_kind is VariationKind.STRESS
        assert run.entry.variation_plan_id == outcome.plan_id


def test_a_scenario_leaves_strategy_and_dataset_untouched() -> None:
    base = make_experiment_definition(strategy_id="buy_then_sell")
    scenario = _fee_scenario(base, Decimal(25))

    derived = derive_stress_definition(base, scenario)

    assert derived.strategy == base.strategy
    assert derived.dataset == base.dataset
    assert derived.risk != base.risk


def test_a_scenario_that_varies_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="vary"):
        StressScenario(risk=None, backtest=None)


def test_a_plan_needs_at_least_one_scenario() -> None:
    with pytest.raises(ValueError, match="at least one"):
        StressPlan(base_experiment_id="x", scenarios=())


def test_a_local_failure_does_not_stop_the_plan(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(definition: ExperimentDefinition) -> BacktestEngine:
        calls["n"] += 1
        if calls["n"] == 2:  # base is call 1; this hits the first scenario
            raise StrategyError("this scenario could not be built")
        return make_research_factory()(definition)

    outcome = _run(tmp_path, factory=flaky)

    statuses = [run.result.status for run in outcome.scenarios]
    assert statuses.count(ExperimentStatus.FAILED) == 1
    assert len(outcome.scenarios) == 2


def test_a_failure_of_integrity_aborts_the_plan(tmp_path: Path) -> None:
    def corrupt(definition: ExperimentDefinition) -> BacktestEngine:
        del definition
        raise DataIntegrityError("the position could not be reconciled")

    outcome = _run(tmp_path, factory=corrupt)

    assert outcome.aborted is True
    assert outcome.baseline.result.status is ExperimentStatus.FAILED
    assert len(outcome.scenarios) == 0


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

    assert summary.count_total == 2


def test_two_scenarios_with_different_execution_policy_get_different_experiment_ids(
    tmp_path: Path,
) -> None:
    outcome = _run(tmp_path)

    ids = {run.result.experiment_id for run in outcome.scenarios}
    assert len(ids) == 2


def test_a_scenario_identical_to_the_base_shares_its_experiment_id(tmp_path: Path) -> None:
    base = make_experiment_definition(strategy_id="buy_then_sell")
    plan = StressPlan(
        base_experiment_id=base.experiment_id,
        scenarios=(StressScenario(risk=base.risk),),
    )
    bars = make_bars([Decimal(50_000)] * 32)

    outcome = StressRunner().run(
        base,
        plan,
        bars=bars,
        factory=make_research_factory(),
        store=ResultStore(tmp_path / "results"),
        ledger=ExperimentLedger(tmp_path / "ledger.jsonl"),
        code_revision="abc1234",
    )

    assert outcome.scenarios[0].result.experiment_id == outcome.baseline.result.experiment_id
