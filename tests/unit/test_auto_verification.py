"""Noticing a contradiction at the moment it is recorded.

Verification used to be a function nobody called. Recording an experiment now compares it
with what was recorded before, because a reproducibility failure discovered months later by
someone who thought to check is a failure that shaped months of decisions first.

The evidence is written before it is judged. A verdict that could prevent a result from being
stored would be a checker with the power to erase what it disagrees with.
"""

from __future__ import annotations

from pathlib import Path

from quantplatform.research.ledger import ExperimentLedger, ReproducibilityVerdict
from quantplatform.research.result import ExperimentStatus
from tests.factories import ANCHOR, make_experiment_result


def _ledger(tmp_path: Path) -> ExperimentLedger:
    return ExperimentLedger(tmp_path / "ledger.jsonl")


def test_the_first_run_of_an_experiment_has_nothing_to_compare_with(tmp_path: Path) -> None:
    entry = _ledger(tmp_path).record(make_experiment_result())

    assert entry.verdict is None


def test_running_the_same_thing_twice_is_recorded_as_reproducible(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record(make_experiment_result())

    entry = ledger.record(make_experiment_result(finished_at=ANCHOR.replace(year=2030)))

    assert entry.verdict is ReproducibilityVerdict.REPRODUCIBLE
    assert len(ledger.entries()) == 2


def test_a_run_over_different_data_says_so_without_raising_an_alarm(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record(make_experiment_result())

    entry = ledger.record(make_experiment_result(bars_digest="d2", total_return="0.9"))

    assert entry.verdict is ReproducibilityVerdict.DATASET_CHANGED
    assert len(ledger.entries()) == 2


def test_a_run_under_different_code_says_so_without_raising_an_alarm(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record(make_experiment_result())

    entry = ledger.record(make_experiment_result(code_revision="def5678", total_return="0.9"))

    assert entry.verdict is ReproducibilityVerdict.CODE_CHANGED
    assert len(ledger.entries()) == 2


def test_a_contradiction_writes_an_extra_line_naming_what_it_contradicts(
    tmp_path: Path,
) -> None:
    # The alarm. Same question, same data, same code, different answer — the one combination
    # that accuses the engine rather than its inputs, and the only one that earns a line of
    # its own.
    ledger = _ledger(tmp_path)
    first = ledger.record(make_experiment_result())

    ledger.record(make_experiment_result(total_return="0.9"))

    entries = ledger.entries()
    assert len(entries) == 3
    alarm = entries[-1]
    assert alarm.status is ExperimentStatus.REPRODUCIBILITY_FAILURE
    assert alarm.compared_with == first.entry_id


def test_the_contradicting_result_is_still_recorded_in_full(tmp_path: Path) -> None:
    # A checker that could suppress the evidence it disagrees with would be deciding what is
    # true rather than reporting what was found.
    ledger = _ledger(tmp_path)
    ledger.record(make_experiment_result())

    ledger.record(make_experiment_result(total_return="0.9"))

    contradicting = ledger.entries()[1]
    assert contradicting.status is ExperimentStatus.SUCCEEDED
    assert contradicting.verdict is ReproducibilityVerdict.REPRODUCIBILITY_FAILURE


def test_a_later_agreement_never_erases_an_earlier_contradiction(tmp_path: Path) -> None:
    # Append-only means the record of having disagreed survives agreeing again. A ledger that
    # could be cleaned by re-running until it matched would be a ledger worth nothing.
    ledger = _ledger(tmp_path)
    ledger.record(make_experiment_result())
    ledger.record(make_experiment_result(total_return="0.9"))

    ledger.record(make_experiment_result(finished_at=ANCHOR.replace(year=2031)))

    statuses = [entry.status for entry in ledger.entries()]
    assert ExperimentStatus.REPRODUCIBILITY_FAILURE in statuses
