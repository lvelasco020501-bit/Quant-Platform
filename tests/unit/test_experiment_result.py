"""A result carries its own inputs, its own failures, and a hash that ignores the clock.

Embedding the definition rather than pointing at it is what makes a result standalone
evidence. Recording failures is what stops the record from being a highlight reel. And
excluding timestamps from the hash is what makes reproducibility checkable at all: two
identical runs necessarily happen at different moments, so a hash over the clock could never
match and the property would be untestable by construction.
"""

from __future__ import annotations

from decimal import Decimal

from quantplatform.research.definition import DatasetSpec, ExperimentDefinition, StrategySpec
from quantplatform.research.result import ExperimentResult, ExperimentStatus, result_hash
from tests.factories import ANCHOR, SYMBOL, make_backtest_config, make_risk_config


def _definition() -> ExperimentDefinition:
    return ExperimentDefinition(
        name="ema-benchmark",
        dataset=DatasetSpec(
            symbol=SYMBOL,
            timeframe="1h",
            start=ANCHOR,
            end=ANCHOR.replace(year=2027),
            source="fixture",
        ),
        strategy=StrategySpec(
            strategy_id="ema_trend", strategy_version="1.0.0", params=(("fast", "20"),)
        ),
        risk=make_risk_config(),
        backtest=make_backtest_config(),
    )


def _result(**overrides: object) -> ExperimentResult:
    defaults: dict[str, object] = {
        "definition": _definition(),
        "code_revision": "abc1234",
        "status": ExperimentStatus.SUCCEEDED,
        "started_at": ANCHOR,
        "finished_at": ANCHOR,
    }
    return ExperimentResult(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_a_result_carries_the_definition_that_produced_it() -> None:
    result = _result()

    assert result.definition == _definition()
    assert result.experiment_id == _definition().experiment_id


def test_a_result_may_record_that_the_code_revision_was_unknown() -> None:
    # Reading git is I/O, and research does none. The composition root supplies the revision
    # or it does not; "unknown" is an honest answer and a fabricated one would not be.
    assert _result(code_revision=None).code_revision is None


def test_a_failed_experiment_is_a_result_and_keeps_its_error() -> None:
    # The record is not a highlight reel. An experiment that blew up is evidence about the
    # configuration that blew it up, and deleting it is how a ledger acquires survivorship
    # bias on its very first bad day.
    failed = _result(status=ExperimentStatus.FAILED, error="the strategy raised on bar 3")

    assert failed.status is ExperimentStatus.FAILED
    assert failed.error == "the strategy raised on bar 3"
    assert failed.performance is None


def test_the_reproducible_hash_ignores_when_the_run_happened() -> None:
    later = _result(started_at=ANCHOR.replace(year=2030), finished_at=ANCHOR.replace(year=2030))

    assert result_hash(_result()) == result_hash(later)


def test_the_reproducible_hash_notices_a_different_outcome() -> None:
    assert result_hash(_result()) != result_hash(
        _result(status=ExperimentStatus.FAILED, error="boom")
    )


def test_a_result_round_trips_through_json_unchanged() -> None:
    result = _result()

    restored = ExperimentResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert Decimal is not None
