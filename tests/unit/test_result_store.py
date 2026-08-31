"""Keeping the evidence, not just the note that says it existed.

Until now a ledger line recorded that an experiment ran and carried a hash of its result.
The result itself was never written anywhere, so nothing could be compared, re-read or
audited — the record pointed at evidence that had already been discarded.

An attempt is its own thing. One experiment can be run many times: `experiment_id` names the
question, `result_hash` names the answer, and neither identifies *this run* — two runs that
agree share both. The attempt identifier is what lets the record hold them separately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantplatform.research.result import ExperimentStatus, result_hash
from quantplatform.research.store import ResultStore, attempt_id
from tests.factories import ANCHOR, make_experiment_result


def test_an_attempt_is_named_apart_from_the_question_and_the_answer() -> None:
    # Two runs that agreed completely still happened twice, and a record that could not tell
    # them apart could not count how many times we looked.
    first = make_experiment_result()
    second = make_experiment_result(finished_at=ANCHOR.replace(year=2030))

    assert first.experiment_id == second.experiment_id
    assert result_hash(first) == result_hash(second)
    assert attempt_id(first) != attempt_id(second)


def test_a_saved_result_reads_back_exactly(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    result = make_experiment_result()

    identifier = store.save(result)

    assert store.load(identifier) == result


def test_saving_over_an_existing_attempt_is_refused(tmp_path: Path) -> None:
    # Evidence is never rewritten. If an attempt identifier ever collided, silently replacing
    # the earlier file would destroy the record of what was believed at the time.
    store = ResultStore(tmp_path)
    result = make_experiment_result()
    store.save(result)

    with pytest.raises(FileExistsError):
        store.save(result)


def test_a_failed_experiment_is_kept_like_any_other(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    failed = make_experiment_result(status=ExperimentStatus.FAILED, error="the fold raised")

    identifier = store.save(failed)

    assert store.load(identifier).status is ExperimentStatus.FAILED


def test_an_attempt_that_was_never_stored_is_reported_rather_than_invented(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        ResultStore(tmp_path).load("0" * 32)
