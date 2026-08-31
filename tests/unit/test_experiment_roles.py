"""What an experiment claims to be, recorded where it cannot be quietly restated.

The rule this protects is the one nothing else can: a period used to choose something has
stopped being out-of-sample for that choice. Code cannot know what a person looked at before
deciding, so it enforces the part it can — that a run declares its role up front, that the
declaration is part of the experiment's name, and that the same configuration claimed as
in-sample and as out-of-sample are two different experiments rather than one told twice.
"""

from __future__ import annotations

import pytest

from quantplatform.research.definition import (
    BENCHMARK_STRATEGY_ID,
    DatasetSpec,
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    experiment_id,
)
from tests.factories import ANCHOR, SYMBOL, make_backtest_config, make_risk_config


def _definition(**overrides: object) -> ExperimentDefinition:
    defaults: dict[str, object] = {
        "name": "candidate",
        "dataset": DatasetSpec(
            symbol=SYMBOL,
            timeframe="1h",
            start=ANCHOR,
            end=ANCHOR.replace(year=2027),
            source="fixture",
        ),
        "strategy": StrategySpec(strategy_id="momentum_probe", strategy_version="1.0.0", params=()),
        "risk": make_risk_config(),
        "backtest": make_backtest_config(),
    }
    return ExperimentDefinition(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_a_definition_declares_a_role() -> None:
    assert _definition().role is ExperimentRole.IN_SAMPLE


def test_the_role_changes_the_name_of_the_experiment() -> None:
    # The whole mechanism. If both claims hashed the same, a result computed in-sample could
    # be filed as out-of-sample and nothing would contradict it.
    in_sample = _definition(role=ExperimentRole.IN_SAMPLE)
    out_of_sample = _definition(role=ExperimentRole.OUT_OF_SAMPLE)

    assert experiment_id(in_sample) != experiment_id(out_of_sample)


def test_the_same_role_still_names_the_same_experiment() -> None:
    assert experiment_id(_definition()) == experiment_id(_definition())


def test_the_frozen_benchmark_cannot_claim_to_be_out_of_sample() -> None:
    # EMA20/50 was watched for the whole of week 5. It is the yardstick and it is not clean
    # out-of-sample evidence for anything, so the claim is refused at construction rather
    # than left to a convention someone breaks at two in the morning.
    benchmark = StrategySpec(strategy_id=BENCHMARK_STRATEGY_ID, strategy_version="1.0.0", params=())

    with pytest.raises(ValueError, match="benchmark"):
        _definition(strategy=benchmark, role=ExperimentRole.OUT_OF_SAMPLE)


def test_the_frozen_benchmark_is_perfectly_allowed_to_be_the_benchmark() -> None:
    benchmark = StrategySpec(strategy_id=BENCHMARK_STRATEGY_ID, strategy_version="1.0.0", params=())

    definition = _definition(strategy=benchmark, role=ExperimentRole.BENCHMARK)

    assert definition.role is ExperimentRole.BENCHMARK


def test_the_role_survives_a_round_trip() -> None:
    definition = _definition(role=ExperimentRole.WALK_FORWARD_TEST)

    restored = ExperimentDefinition.model_validate_json(definition.model_dump_json())

    assert restored.role is ExperimentRole.WALK_FORWARD_TEST
    assert experiment_id(restored) == experiment_id(definition)
