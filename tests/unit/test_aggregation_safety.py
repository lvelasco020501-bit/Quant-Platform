"""Proving a set of folds is one plan before summarising it.

The old signature took a bare list of results and trusted it. Two plans handed in together
would produce a summary of neither, and nothing would say so — the numbers would look exactly
as convincing as a correct one. Membership is now demonstrated by the ledger's own lineage
rather than assumed from the caller's good intentions.
"""

from __future__ import annotations

import pytest

from quantplatform.research.aggregate import FoldRun, aggregate_walk_forward
from quantplatform.research.definition import ExperimentRole
from tests.factories import make_fold_run


def test_a_complete_plan_summarises() -> None:
    summary = aggregate_walk_forward([make_fold_run(index=0), make_fold_run(index=1)])

    assert summary.folds_total == 2


def test_folds_from_two_plans_are_refused() -> None:
    with pytest.raises(ValueError, match="one plan"):
        aggregate_walk_forward(
            [make_fold_run(index=0), make_fold_run(index=1, plan_id="other-plan")]
        )


def test_a_repeated_fold_index_is_refused() -> None:
    # Two results claiming the same position means one of them is misfiled, and a median over
    # both would count a window twice without saying which.
    with pytest.raises(ValueError, match="index"):
        aggregate_walk_forward([make_fold_run(index=0), make_fold_run(index=0)])


def test_a_result_with_no_lineage_is_refused() -> None:
    with pytest.raises(ValueError, match="lineage"):
        aggregate_walk_forward([make_fold_run(index=0), make_fold_run(index=None)])


def test_a_training_fold_among_the_test_folds_is_refused() -> None:
    # Training windows are observation windows here. Slipping one into the summary would
    # report the same calendar twice under two names.
    with pytest.raises(ValueError, match="test"):
        aggregate_walk_forward(
            [
                make_fold_run(index=0),
                make_fold_run(index=1, role=ExperimentRole.WALK_FORWARD_TRAIN),
            ]
        )


def test_a_plan_with_a_hole_in_it_is_refused_by_default() -> None:
    with pytest.raises(ValueError, match="missing"):
        aggregate_walk_forward([make_fold_run(index=0), make_fold_run(index=2)])


def test_a_partial_summary_is_available_only_by_asking_for_one() -> None:
    # Seven folds of a ten-fold plan is a different claim from the plan's result, and saying
    # it out loud is the price of making it.
    summary = aggregate_walk_forward(
        [make_fold_run(index=0), make_fold_run(index=2)], require_complete=False
    )

    assert summary.folds_total == 2


def test_summarising_nothing_is_refused_rather_than_answered_with_zeroes() -> None:
    with pytest.raises(ValueError, match="fold"):
        aggregate_walk_forward([])


def test_the_contract_takes_evidence_rather_than_a_bare_list() -> None:
    assert set(FoldRun.model_fields) == {"entry", "result"}
