"""Summarising a set of runs without ever picking one of them.

Every property here exists to keep this module from becoming a ranking function with a
distributional disguise: failures count, extremes are reported without saying which run
produced them, and a summary refuses to mix runs from two different plans.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.robustness import (
    DistributionSummary,
    VariationRun,
    summarise_variations,
)
from tests.factories import make_experiment_result


def _run(tmp_path: Path, *, plan_id: str = "plan-a", **overrides: object) -> VariationRun:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    result = make_experiment_result(**overrides)
    entry = ledger.record(
        result,
        derived_from="base",
        variation_kind=VariationKind.SENSITIVITY,
        variation_plan_id=plan_id,
    )
    return VariationRun(entry=entry, result=result)


def test_count_total_is_completed_plus_failed(tmp_path: Path) -> None:
    runs = [
        _run(tmp_path, code_revision="a1"),
        _run(tmp_path, code_revision="a2", status=ExperimentStatus.FAILED, error="boom"),
    ]

    summary = summarise_variations(runs, expected_plan_id="plan-a")

    assert summary.count_total == 2
    assert summary.count_completed == 1
    assert summary.count_failed == 1


def test_a_failed_run_is_not_silently_excluded_from_the_total(tmp_path: Path) -> None:
    runs = [_run(tmp_path, status=ExperimentStatus.FAILED, error="boom")]

    summary = summarise_variations(runs, expected_plan_id="plan-a")

    assert summary.count_total == 1
    assert summary.count_completed == 0
    assert summary.count_failed == 1
    assert summary.median_return is None


def test_runs_from_a_different_plan_are_refused(tmp_path: Path) -> None:
    runs = [_run(tmp_path, plan_id="plan-a"), _run(tmp_path, plan_id="plan-b", code_revision="a2")]

    with pytest.raises(ValueError, match="plan"):
        summarise_variations(runs, expected_plan_id="plan-a")


def test_summarising_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarise_variations([], expected_plan_id="plan-a")


def test_dispersion_needs_at_least_two_completed_runs(tmp_path: Path) -> None:
    runs = [_run(tmp_path)]

    summary = summarise_variations(runs, expected_plan_id="plan-a")

    assert summary.expectancy_dispersion is None
    assert summary.r_stability is None


def test_min_and_max_return_are_real_extremes_not_the_median(tmp_path: Path) -> None:
    runs = [
        _run(tmp_path, code_revision="a1", total_return="-0.5"),
        _run(tmp_path, code_revision="a2", total_return="0.1"),
        _run(tmp_path, code_revision="a3", total_return="0.9"),
    ]

    summary = summarise_variations(runs, expected_plan_id="plan-a")

    assert summary.min_return == Decimal("-0.5")
    assert summary.max_return == Decimal("0.9")
    assert summary.median_return == Decimal("0.1")


def test_distribution_summary_carries_no_selection_vocabulary() -> None:
    # No field named best/worst/rank/top/winner/score, and no method that could return one
    # member of the set that produced this summary.
    names = set(DistributionSummary.model_fields)
    banned = {"best", "worst", "rank", "top", "winner", "score"}
    assert not any(word in name for name in names for word in banned)
    assert not hasattr(DistributionSummary, "best")
    assert not hasattr(DistributionSummary, "winner")
