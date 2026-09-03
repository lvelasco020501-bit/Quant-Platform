"""Running every episode of a regime plan, and keeping each regime's results separate.

Two labels never share one summary — pooling a trending episode with a ranging one would
answer a question about neither.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.errors import StrategyError
from quantplatform.core.models.market import MarketBar
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.folds import WindowSpec
from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.regime import RegimeEpisode, RegimeOutcome, RegimePlan, RegimeRunner
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import BacktestFactory
from quantplatform.research.store import ResultStore
from tests.factories import make_bar, make_experiment_definition, make_research_factory


def _bars(count: int) -> tuple[MarketBar, ...]:
    return tuple(make_bar(index=i, close=Decimal(50_000)) for i in range(count))


def _plan(base_id: str, bars: tuple[MarketBar, ...]) -> RegimePlan:
    return RegimePlan(
        base_experiment_id=base_id,
        labeller_id="parity",
        episodes=(
            RegimeEpisode(
                label="trend",
                window=WindowSpec(start=bars[0].open_time, end=bars[4].open_time),
            ),
            RegimeEpisode(
                label="range",
                window=WindowSpec(start=bars[4].open_time, end=bars[8].close_time),
            ),
        ),
    )


def _run(
    tmp_path: Path,
    *,
    factory: BacktestFactory | None = None,
    plan: RegimePlan | None = None,
    bars: tuple[MarketBar, ...] | None = None,
) -> RegimeOutcome:
    base = make_experiment_definition(strategy_id="buy_then_sell")
    actual_bars = bars if bars is not None else _bars(9)
    return RegimeRunner().run(
        base,
        plan if plan is not None else _plan(base.experiment_id, actual_bars),
        bars=actual_bars,
        factory=factory if factory is not None else make_research_factory(),
        store=ResultStore(tmp_path / "results"),
        ledger=ExperimentLedger(tmp_path / "ledger.jsonl"),
        code_revision="abc1234",
    )


def test_every_episode_is_run(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    assert len(outcome.episodes) == 2
    assert all(run.result.status is ExperimentStatus.SUCCEEDED for run in outcome.episodes)


def test_each_episode_carries_its_own_regime_label(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    labels = {run.result.definition.regime_label for run in outcome.episodes}
    assert labels == {"trend", "range"}


def test_lineage_is_recorded_on_every_episode(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    for run in outcome.episodes:
        assert run.entry.variation_kind is VariationKind.REGIME
        assert run.entry.variation_plan_id == outcome.plan_id
        assert run.entry.derived_from is not None


def test_a_local_failure_does_not_stop_the_plan(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(definition: ExperimentDefinition) -> BacktestEngine:
        calls["n"] += 1
        if calls["n"] == 1:
            raise StrategyError("this episode could not be built")
        return make_research_factory()(definition)

    outcome = _run(tmp_path, factory=flaky)

    statuses = [run.result.status for run in outcome.episodes]
    assert statuses.count(ExperimentStatus.FAILED) == 1
    assert len(outcome.episodes) == 2


def test_a_dataset_mismatch_aborts_the_plan_after_recording_the_failure(tmp_path: Path) -> None:
    # Only the first episode's bars are supplied to the runner, so the second episode's
    # window has nothing to validate against — an ordinary DatasetMismatchError, and fatal
    # because the loader is shared across the plan.
    bars = _bars(9)
    only_first_episode = bars[:4]
    plan = _plan(make_experiment_definition(strategy_id="buy_then_sell").experiment_id, bars)

    outcome = _run(tmp_path, plan=plan, bars=only_first_episode)

    assert outcome.aborted is True
    assert len(outcome.episodes) == 2
    assert outcome.episodes[0].result.status is ExperimentStatus.SUCCEEDED
    assert outcome.episodes[1].result.status is ExperimentStatus.FAILED
    assert outcome.episodes[1].result.error_type == "DatasetMismatchError"


def test_an_aborted_plan_refuses_to_summarise_itself(tmp_path: Path) -> None:
    bars = _bars(9)
    plan = _plan(make_experiment_definition(strategy_id="buy_then_sell").experiment_id, bars)

    outcome = _run(tmp_path, plan=plan, bars=bars[:4])

    with pytest.raises(ValueError, match="aborted"):
        outcome.summarise()


def test_summarise_never_pools_two_labels(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    summary_by_label = outcome.summarise()

    assert set(summary_by_label) == {"trend", "range"}
    for summary in summary_by_label.values():
        assert summary.count_total == 1


def test_a_regime_run_verifies_as_reproducible(tmp_path: Path) -> None:
    bars = _bars(9)
    base = make_experiment_definition(strategy_id="buy_then_sell")
    plan = _plan(base.experiment_id, bars)
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    store = ResultStore(tmp_path / "results")

    RegimeRunner().run(
        base,
        plan,
        bars=bars,
        factory=make_research_factory(),
        store=store,
        ledger=ledger,
        code_revision="abc1234",
    )
    RegimeRunner().run(
        base,
        plan,
        bars=bars,
        factory=make_research_factory(),
        store=store,
        ledger=ledger,
        code_revision="abc1234",
    )

    verdicts = {entry.verdict for entry in ledger.entries() if entry.verdict is not None}
    assert "reproducible" in {v.value for v in verdicts}
