"""Putting results beside each other without deciding between them.

A comparison that sorts by profit has already answered the question it was asked to
illustrate. So the table preserves the order it was handed, offers no way to reorder itself,
and keeps the runs that failed as rows — a comparison that quietly omits what did not work is
a comparison that has started choosing.
"""

from __future__ import annotations

from decimal import Decimal

from quantplatform.backtesting.metrics import PerformanceSummary, TradeStatistics
from quantplatform.research.compare import COMPARISON_METRICS, compare
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
)
from quantplatform.research.result import ExperimentResult, ExperimentStatus
from tests.factories import (
    ANCHOR,
    make_backtest_config,
    make_dataset_spec,
    make_risk_config,
)


def _definition(name: str, role: ExperimentRole = ExperimentRole.IN_SAMPLE) -> ExperimentDefinition:
    return ExperimentDefinition(
        name=name,
        role=role,
        dataset=make_dataset_spec(),
        strategy=StrategySpec(strategy_id="probe", strategy_version="1.0.0", params=()),
        risk=make_risk_config(),
        backtest=make_backtest_config(),
    )


def _performance(total_return: str) -> PerformanceSummary:
    return PerformanceSummary(
        initial_equity=Decimal(100_000),
        final_equity=Decimal(100_000),
        total_return=Decimal(total_return),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        max_drawdown=Decimal("0.1"),
        commission_paid=Decimal(5),
        slippage_paid=Decimal(3),
        trades=TradeStatistics(
            count=2, wins=1, losses=1, gross_profit=Decimal(10), gross_loss=Decimal(4)
        ),
        bars_processed=10,
        duration_seconds=3600,
    )


def _result(name: str, total_return: str = "0.1") -> ExperimentResult:
    return ExperimentResult(
        definition=_definition(name),
        status=ExperimentStatus.SUCCEEDED,
        performance=_performance(total_return),
        started_at=ANCHOR,
        finished_at=ANCHOR,
    )


def _failed(name: str) -> ExperimentResult:
    return ExperimentResult(
        definition=_definition(name),
        status=ExperimentStatus.FAILED,
        error="the strategy raised",
        started_at=ANCHOR,
        finished_at=ANCHOR,
    )


def test_rows_keep_the_order_they_were_given() -> None:
    # Not sorted by anything, ever. The reader brought an order; the table returns it.
    table = compare([_result("c", "0.3"), _result("a", "0.1"), _result("b", "0.2")])

    assert [row.name for row in table.rows] == ["c", "a", "b"]


def test_every_row_reports_the_same_columns() -> None:
    table = compare([_result("a"), _failed("b")])

    for row in table.rows:
        assert set(row.metrics) == set(COMPARISON_METRICS)


def test_seventeen_metrics_are_compared() -> None:
    assert len(COMPARISON_METRICS) == 17


def test_a_metric_a_run_could_not_produce_is_absent_rather_than_zero() -> None:
    # Zero is a measurement. A run that could not compute Sharpe did not measure zero
    # Sharpe, and filling the gap would put a number nobody calculated into a comparison.
    (row,) = compare([_result("a")]).rows

    assert row.metrics["sharpe_ratio"] is None
    assert row.metrics["total_return"] == Decimal("0.1")


def test_a_failed_run_is_a_row_and_not_an_omission() -> None:
    table = compare([_result("a"), _failed("b")])

    failed = table.rows[1]
    assert failed.status is ExperimentStatus.FAILED
    assert all(value is None for value in failed.metrics.values())


def test_comparing_nothing_is_an_empty_table_rather_than_an_error() -> None:
    assert compare([]).rows == ()


def test_the_table_offers_no_way_to_pick_a_winner() -> None:
    # Deliberately absent, and asserted so that adding one is a test failure rather than a
    # convenience. The distance between "this can be ordered by profit" and "this says which
    # is good" is a single call site.
    table = compare([_result("a"), _result("b")])

    for forbidden in ("best", "rank", "top", "sort_by", "order_by", "winner"):
        assert not hasattr(table, forbidden), forbidden
