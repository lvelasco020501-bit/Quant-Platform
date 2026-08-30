"""An experiment is its definition, and its name is that definition's hash.

The point is not tidiness. A result whose inputs cannot be reconstructed is an anecdote, and
a comparison between two anecdotes is an opinion. Deriving the identifier from the complete
definition makes "same experiment" a computable claim rather than a remembered one — and
makes changing anything about a run visible as a different name.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

import pytest

from quantplatform.research.definition import (
    DatasetSpec,
    ExperimentDefinition,
    StrategySpec,
    experiment_id,
)
from tests.factories import ANCHOR, SYMBOL, make_backtest_config, make_risk_config


def _definition(**overrides: object) -> ExperimentDefinition:
    defaults: dict[str, object] = {
        "name": "ema-benchmark",
        "dataset": DatasetSpec(
            symbol=SYMBOL,
            timeframe="1h",
            start=ANCHOR,
            end=ANCHOR.replace(year=2027),
            source="fixture",
        ),
        "strategy": StrategySpec(
            strategy_id="ema_trend",
            strategy_version="1.0.0",
            params=(("fast", "20"), ("slow", "50")),
        ),
        "risk": make_risk_config(),
        "backtest": make_backtest_config(),
    }
    return ExperimentDefinition(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_the_same_definition_always_names_the_same_experiment() -> None:
    assert experiment_id(_definition()) == experiment_id(_definition())


def test_changing_one_field_changes_the_name() -> None:
    other = _definition(
        strategy=StrategySpec(
            strategy_id="ema_trend",
            strategy_version="1.0.0",
            params=(("fast", "21"), ("slow", "50")),
        )
    )

    assert experiment_id(_definition()) != experiment_id(other)


def test_parameters_given_in_a_different_order_are_a_different_experiment() -> None:
    # Deliberate, and the reason params are an ordered tuple rather than a mapping: the
    # alternative is a hash that depends on dictionary insertion order, which would make two
    # runs of the same thing look different for a reason nobody could see in the file.
    swapped = _definition(
        strategy=StrategySpec(
            strategy_id="ema_trend",
            strategy_version="1.0.0",
            params=(("slow", "50"), ("fast", "20")),
        )
    )

    assert experiment_id(_definition()) != experiment_id(swapped)


def test_the_name_is_the_same_in_a_different_process_and_under_a_different_hash_seed() -> None:
    # Reproducibility has to survive leaving the machine, and Python randomises the hash of
    # built-in collections per process. Nothing in a definition may depend on that, and only
    # a fresh interpreter can prove it.
    expected = experiment_id(_definition())
    script = (
        "import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, '.')\n"
        "from tests.unit.test_experiment_definition import _definition\n"
        "from quantplatform.research.definition import experiment_id\n"
        "print(experiment_id(_definition()))"
    )

    seen = set()
    for seed in ("0", "1", "12345"):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        seen.add(completed.stdout.strip())

    assert seen == {expected}


def test_a_definition_round_trips_through_json_unchanged() -> None:
    definition = _definition()

    restored = ExperimentDefinition.model_validate_json(definition.model_dump_json())

    assert restored == definition
    assert experiment_id(restored) == experiment_id(definition)


def test_a_dataset_that_ends_before_it_starts_does_not_construct() -> None:
    with pytest.raises(ValueError, match="end"):
        DatasetSpec(
            symbol=SYMBOL,
            timeframe="1h",
            start=ANCHOR.replace(year=2027),
            end=ANCHOR,
            source="fixture",
        )


def test_a_definition_carries_no_seed_because_nothing_is_random() -> None:
    # An unread field that looks like a control is worse than an absent one: it invites a
    # reader to believe randomness is being managed. If randomness ever arrives, the field
    # arrives with it and every existing experiment id changes, which is the visible event.
    assert "seed" not in ExperimentDefinition.model_fields
    assert Decimal is not None
