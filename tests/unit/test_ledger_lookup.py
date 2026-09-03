"""Finding one attempt again, without scanning every line by hand.

`compare` needed to inspect a *specific* run, not only "the most recent one", and the ledger
had no way to answer that beyond iterating `entries()` and re-deriving the filter every
caller already had to write. And `attempts()` counted alarm lines as though they were runs:
two agreeing attempts followed by a contradiction reported three attempts, when only two
runs ever happened — the third line is the ledger raising its voice about the first two, not
a third look at the question.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.research.ledger import ExperimentLedger
from tests.factories import ANCHOR, make_experiment_result


def test_attempts_for_lists_the_real_runs_oldest_first(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    result = make_experiment_result()
    ledger.record(result)
    ledger.record(result.model_copy(update={"finished_at": ANCHOR.replace(year=2027)}))

    attempts = ledger.attempts_for(result.experiment_id)

    assert [entry.recorded_at for entry in attempts] == [ANCHOR, ANCHOR.replace(year=2027)]


def test_an_alarm_line_is_not_a_second_attempt(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    honest = make_experiment_result()
    ledger.record(honest)
    diverged = honest.model_copy(
        update={
            "performance": honest.performance.model_copy(  # type: ignore[union-attr]
                update={"final_equity": Decimal(1)}
            )
        }
    )
    ledger.record(diverged)

    # Two runs happened, and a third line was appended to say so. attempts_for and the
    # attempts() count must both describe the two runs, not the three lines on disk.
    assert len(ledger.entries()) == 3
    assert len(ledger.attempts_for(honest.experiment_id)) == 2
    assert ledger.attempts(honest.experiment_id) == 2


def test_entry_for_attempt_finds_the_line_that_named_it(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    result = make_experiment_result()
    recorded = ledger.record(result)

    found = ledger.entry_for_attempt(recorded.attempt_id)

    assert found == recorded


def test_entry_for_attempt_fails_loudly_on_an_unknown_id(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")

    with pytest.raises(LookupError, match="unknown-attempt"):
        ledger.entry_for_attempt("unknown-attempt")


def test_latest_attempt_fails_loudly_when_the_experiment_never_ran(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")

    with pytest.raises(LookupError, match="never"):
        ledger.latest_attempt("an-experiment-that-never-ran")
