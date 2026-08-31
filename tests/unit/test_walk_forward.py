"""Windows declared in advance, and what a run over them does and does not show.

Walk-forward here is a **measurement instrument, not a fitting one**. Nothing is selected, so
train and test run the same configuration and leakage is impossible by construction rather
than by vigilance. That also bounds what a good result means: it demonstrates stability across
windows, not clean out-of-sample edge, because the configuration was chosen by a person who
had already seen every window.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from quantplatform.backtesting.metrics import PerformanceSummary, TradeStatistics
from quantplatform.research.aggregate import aggregate_walk_forward
from quantplatform.research.definition import (
    DatasetSpec,
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
)
from quantplatform.research.folds import (
    Fold,
    WalkForwardPlan,
    WindowSpec,
    derive_fold_definitions,
)
from quantplatform.research.result import ExperimentResult, ExperimentStatus
from tests.factories import ANCHOR, SYMBOL, make_backtest_config, make_risk_config

_DAY = timedelta(days=1)


def _base() -> ExperimentDefinition:
    return ExperimentDefinition(
        name="probe",
        role=ExperimentRole.IN_SAMPLE,
        dataset=DatasetSpec(
            symbol=SYMBOL,
            timeframe="1h",
            start=ANCHOR,
            end=ANCHOR + 100 * _DAY,
            source="fixture",
        ),
        strategy=StrategySpec(strategy_id="probe", strategy_version="1.0.0", params=()),
        risk=make_risk_config(),
        backtest=make_backtest_config(),
    )


def _window(start_day: int, end_day: int) -> WindowSpec:
    return WindowSpec(start=ANCHOR + start_day * _DAY, end=ANCHOR + end_day * _DAY)


def _plan(
    *, folds: tuple[tuple[int, int, int, int], ...] = ((0, 10, 10, 20), (10, 20, 20, 30))
) -> WalkForwardPlan:
    return WalkForwardPlan(
        base_experiment_id=_base().experiment_id,
        folds=tuple(
            Fold(index=index, train=_window(a, b), test=_window(c, d))
            for index, (a, b, c, d) in enumerate(folds)
        ),
    )


# --- The plan and its temporal invariants --------------------------------------------------------


def test_a_plan_names_itself_from_its_windows() -> None:
    assert _plan().plan_id == _plan().plan_id


def test_moving_a_single_window_names_a_different_plan() -> None:
    other = _plan(folds=((0, 10, 10, 20), (10, 20, 20, 31)))

    assert _plan().plan_id != other.plan_id


def test_a_test_window_may_not_precede_its_own_training_window() -> None:
    with pytest.raises(ValueError, match="train"):
        WalkForwardPlan(
            base_experiment_id=_base().experiment_id,
            folds=(Fold(index=0, train=_window(10, 20), test=_window(0, 10)),),
        )


def test_overlapping_test_windows_are_refused() -> None:
    # Two folds testing the same days would count that period twice, and the second would
    # inherit whatever the first concluded about it.
    with pytest.raises(ValueError, match="overlap"):
        WalkForwardPlan(
            base_experiment_id=_base().experiment_id,
            folds=(
                Fold(index=0, train=_window(0, 10), test=_window(10, 25)),
                Fold(index=1, train=_window(10, 20), test=_window(20, 30)),
            ),
        )


def test_fold_indices_must_be_contiguous_from_zero() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        WalkForwardPlan(
            base_experiment_id=_base().experiment_id,
            folds=(Fold(index=1, train=_window(0, 10), test=_window(10, 20)),),
        )


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError, match="end"):
        WindowSpec(start=ANCHOR + 10 * _DAY, end=ANCHOR)


def test_a_plan_with_no_folds_is_refused() -> None:
    with pytest.raises(ValueError, match="fold"):
        WalkForwardPlan(base_experiment_id=_base().experiment_id, folds=())


def test_a_boundary_bar_belongs_to_exactly_one_window() -> None:
    # Half-open windows. With a closed upper bound the bar at the boundary would sit in the
    # training window and in the test window at once, which is leakage of exactly one bar and
    # invisible in every summary.
    train, test = _window(0, 10), _window(10, 20)
    boundary = ANCHOR + 10 * _DAY

    assert not train.contains(boundary)
    assert test.contains(boundary)


# --- Deriving the definitions each fold runs -----------------------------------------------------


def test_each_fold_produces_a_training_and_a_test_definition() -> None:
    derived = derive_fold_definitions(_base(), _plan())

    assert len(derived) == 2
    for train, test in derived:
        assert train.role is ExperimentRole.WALK_FORWARD_TRAIN
        assert test.role is ExperimentRole.WALK_FORWARD_TEST


def test_a_derived_definition_runs_only_its_own_window() -> None:
    (train, test), _ = derive_fold_definitions(_base(), _plan())

    assert (train.dataset.start, train.dataset.end) == (ANCHOR, ANCHOR + 10 * _DAY)
    assert (test.dataset.start, test.dataset.end) == (ANCHOR + 10 * _DAY, ANCHOR + 20 * _DAY)


def test_every_derived_definition_has_its_own_name() -> None:
    derived = derive_fold_definitions(_base(), _plan())
    identifiers = {definition.experiment_id for pair in derived for definition in pair}

    assert len(identifiers) == 4
    assert _base().experiment_id not in identifiers


def test_a_derived_definition_carries_no_lineage() -> None:
    # Lineage belongs in the ledger. Putting the plan into the definition would mean the same
    # window run alone and run inside a plan were different experiments, when what differs is
    # why they were run rather than what was run.
    (train, _), _ = derive_fold_definitions(_base(), _plan())

    assert "plan_id" not in ExperimentDefinition.model_fields
    assert "fold_index" not in ExperimentDefinition.model_fields
    assert train.dataset.symbol == SYMBOL


# --- Consolidating folds without losing the bad ones ---------------------------------------------


def _performance(total_return: str, drawdown: str, expectancy: str) -> PerformanceSummary:
    return PerformanceSummary(
        initial_equity=Decimal(100_000),
        final_equity=Decimal(100_000),
        total_return=Decimal(total_return),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        max_drawdown=Decimal(drawdown),
        commission_paid=Decimal(0),
        slippage_paid=Decimal(0),
        trades=TradeStatistics(
            count=2,
            wins=1,
            losses=1,
            gross_profit=Decimal(10),
            gross_loss=Decimal(4),
            expectancy=Decimal(expectancy),
            average_r=Decimal(expectancy),
        ),
        bars_processed=10,
        duration_seconds=3600,
    )


def _fold_result(
    *,
    role: ExperimentRole = ExperimentRole.WALK_FORWARD_TEST,
    total_return: str = "0.1",
    drawdown: str = "0.05",
    expectancy: str = "1",
    failed: bool = False,
) -> ExperimentResult:
    definition = _base().model_copy(update={"role": role})
    if failed:
        return ExperimentResult(
            definition=definition,
            status=ExperimentStatus.FAILED,
            error="the fold raised",
            started_at=ANCHOR,
            finished_at=ANCHOR,
        )
    return ExperimentResult(
        definition=definition,
        status=ExperimentStatus.SUCCEEDED,
        performance=_performance(total_return, drawdown, expectancy),
        started_at=ANCHOR,
        finished_at=ANCHOR,
    )


def test_only_the_test_folds_are_summarised() -> None:
    # A training window is an observation window here, and counting it would report the same
    # calendar twice under two names.
    summary = aggregate_walk_forward(
        [
            _fold_result(role=ExperimentRole.WALK_FORWARD_TRAIN, total_return="9"),
            _fold_result(total_return="0.1"),
        ]
    )

    assert summary.folds_total == 1
    assert summary.median_return == Decimal("0.1")


def test_the_worst_fold_is_reported_and_not_averaged_away() -> None:
    summary = aggregate_walk_forward(
        [
            _fold_result(total_return="0.5", drawdown="0.02"),
            _fold_result(total_return="-0.4", drawdown="0.30"),
            _fold_result(total_return="0.2", drawdown="0.05"),
        ]
    )

    assert summary.worst_return == Decimal("-0.4")
    assert summary.worst_max_drawdown == Decimal("0.30")
    assert summary.median_return == Decimal("0.2")


def test_a_failed_fold_is_counted_and_never_becomes_a_return_of_zero() -> None:
    # Zero is a result. A fold that blew up produced no result at all, and pretending it
    # broke even would drag every median toward a number nothing measured.
    summary = aggregate_walk_forward([_fold_result(total_return="0.4"), _fold_result(failed=True)])

    assert (summary.folds_total, summary.folds_completed, summary.folds_failed) == (2, 1, 1)
    assert summary.median_return == Decimal("0.4")


def test_the_positive_share_says_which_denominator_it_used() -> None:
    # Eight failures and two winners must not read as "100% positive". The field carries its
    # own denominator in its name, and the counts sit beside it so the other fraction is one
    # subtraction away.
    summary = aggregate_walk_forward(
        [
            _fold_result(total_return="0.4"),
            _fold_result(total_return="0.2"),
            *[_fold_result(failed=True) for _ in range(8)],
        ]
    )

    assert summary.positive_share_of_completed == Decimal(1)
    assert summary.folds_total == 10
    assert summary.folds_failed == 8


def test_dispersion_is_undefined_rather_than_zero_for_a_single_fold() -> None:
    summary = aggregate_walk_forward([_fold_result()])

    assert summary.expectancy_dispersion is None
    assert summary.r_stability is None
