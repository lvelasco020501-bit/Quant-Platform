"""Telling a broken result apart from a changed input.

Two runs of one experiment disagreeing is only alarming if nothing else changed. If the data
was re-ingested, or the code moved, the disagreement is expected and saying otherwise would
cry wolf until nobody listened. So the verdict is a five-way question, and the first answer
it can give is "I cannot tell" — which is the honest response to a record that predates the
fingerprints, and the one a checker is most tempted to skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quantplatform.orchestration.research import code_revision
from quantplatform.research.definition import (
    ExperimentDefinition,
    StrategySpec,
)
from quantplatform.research.ledger import LedgerEntry, ReproducibilityVerdict, verify
from quantplatform.research.result import ExperimentResult, ExperimentStatus
from tests.factories import (
    ANCHOR,
    make_backtest_config,
    make_dataset_spec,
    make_risk_config,
)


def _definition() -> ExperimentDefinition:
    return ExperimentDefinition(
        name="benchmark",
        dataset=make_dataset_spec(),
        strategy=StrategySpec(strategy_id="ema_trend", strategy_version="1.0.0", params=()),
        risk=make_risk_config(),
        backtest=make_backtest_config(),
    )


def _entry(**overrides: object) -> LedgerEntry:
    defaults: dict[str, object] = {
        "entry_id": "e" * 32,
        "attempt_id": "a" * 32,
        "experiment_id": "e1",
        "name": "benchmark",
        "result_hash": "r1",
        "bars_digest": "d1",
        "code_revision": "abc1234",
        "status": ExperimentStatus.SUCCEEDED,
        "recorded_at": ANCHOR,
    }
    return LedgerEntry(**{**defaults, **overrides})  # type: ignore[arg-type]


# --- The five verdicts, in the order they are asked -----------------------------------------------


def test_two_identical_runs_are_reproducible() -> None:
    assert verify(_entry(), _entry()) is ReproducibilityVerdict.REPRODUCIBLE


def test_a_different_result_from_the_same_inputs_is_a_failure() -> None:
    assert (
        verify(_entry(), _entry(result_hash="r2")) is ReproducibilityVerdict.REPRODUCIBILITY_FAILURE
    )


def test_different_data_is_not_a_failure() -> None:
    # The run disagreed because it was given different numbers. Calling that irreproducible
    # would blame the engine for the dataset.
    assert (
        verify(_entry(), _entry(bars_digest="d2", result_hash="r2"))
        is ReproducibilityVerdict.DATASET_CHANGED
    )


def test_different_code_is_not_a_failure() -> None:
    assert (
        verify(_entry(), _entry(code_revision="def5678", result_hash="r2"))
        is ReproducibilityVerdict.CODE_CHANGED
    )


def test_a_missing_fingerprint_is_indeterminate_rather_than_anything_else() -> None:
    # Asked first, and deliberately: without both fingerprints nothing can be concluded, and
    # reporting "the dataset changed" with no digest to compare would be inventing a finding.
    # Ledger lines written before these fields existed land here.
    assert (
        verify(_entry(bars_digest=None), _entry(result_hash="r2"))
        is ReproducibilityVerdict.INDETERMINATE
    )
    assert (
        verify(_entry(code_revision=None), _entry(result_hash="r2"))
        is ReproducibilityVerdict.INDETERMINATE
    )


def test_data_takes_precedence_over_code_when_both_moved() -> None:
    assert (
        verify(_entry(), _entry(bars_digest="d2", code_revision="def5678"))
        is ReproducibilityVerdict.DATASET_CHANGED
    )


def test_two_different_experiments_are_not_compared_at_all() -> None:
    with pytest.raises(ValueError, match="same experiment"):
        verify(_entry(), _entry(experiment_id="e2"))


# --- A verdict is not something a run can produce ------------------------------------------------


def test_a_result_cannot_be_born_irreproducible() -> None:
    # The status exists for a ledger line, not for an outcome. A verdict comes from comparing
    # two results; a single run cannot have one, and letting it claim otherwise would put an
    # unearned conclusion into the evidence.
    with pytest.raises(ValueError, match="comparing"):
        ExperimentResult(
            definition=_definition(),
            status=ExperimentStatus.REPRODUCIBILITY_FAILURE,
            started_at=ANCHOR,
            finished_at=ANCHOR,
        )


# --- Reading the revision, outside research ------------------------------------------------------


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)  # noqa: S603, S607


def test_a_clean_checkout_reports_its_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")

    revision = code_revision(tmp_path)

    assert revision is not None
    assert not revision.endswith("-dirty")


def test_an_uncommitted_change_is_recorded_as_dirty(tmp_path: Path) -> None:
    # The important half. A result produced from a working tree nobody can reconstruct is not
    # reproducible, and without this suffix `verify` would report failures whose real cause
    # was uncommitted code.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    (tmp_path / "a.txt").write_text("two", encoding="utf-8")

    revision = code_revision(tmp_path)

    assert revision is not None
    assert revision.endswith("-dirty")


def test_no_repository_reports_nothing_rather_than_guessing(tmp_path: Path) -> None:
    assert code_revision(tmp_path / "not-a-repo") is None
