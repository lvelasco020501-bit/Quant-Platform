"""What an experiment is, and what it is called.

An experiment is its definition. A result whose inputs cannot be reconstructed is an
anecdote, and a comparison between two anecdotes is an opinion — so the identifier is derived
from the complete definition rather than assigned, which makes "the same experiment" a
computable claim and makes changing anything about a run appear as a different name.

There is deliberately **no seed**. Nothing in the pipeline reads a clock or draws a random
number: identifiers are derived, iteration order is fixed, and two runs over the same bars
produce the same output. A seed field would suggest randomness is being managed, which is the
kind of unread vocabulary this codebase has had to remove four times. If randomness ever
arrives the field arrives with it, every existing identifier changes, and that change is the
visible event.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self

from pydantic import model_validator

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.core.models.base import (
    DomainModel,
    SemanticVersion,
    StrategyId,
    Symbol,
    Text,
    UtcDatetime,
)
from quantplatform.risk.config import RiskConfiguration

__all__ = [
    "BENCHMARK_STRATEGY_ID",
    "DatasetSpec",
    "ExperimentDefinition",
    "ExperimentRole",
    "StrategySpec",
    "canonical_json",
    "experiment_id",
]

_IDENTIFIER_LENGTH = 32

BENCHMARK_STRATEGY_ID = "ema_trend"
"""The frozen yardstick, named here so the rule about it can be enforced rather than
remembered.

EMA20/50 was watched for the whole of week 5. It is what later work is measured against and
it is not clean out-of-sample evidence for anything, so declaring it as such is refused
below. A convention nobody enforces is a convention that gets broken at two in the morning.
"""


class ExperimentRole(StrEnum):
    """What a run claims to be.

    The rule this exists for is the one no code can check: a period used to choose something
    has stopped being out-of-sample for that choice, and nothing can know what a person looked
    at before deciding. What *can* be arranged is that the claim is made up front and forms
    part of the experiment's name — so the same configuration claimed as in-sample and as
    out-of-sample are two experiments with two names, rather than one told twice.
    """

    BENCHMARK = "benchmark"
    IN_SAMPLE = "in_sample"
    OUT_OF_SAMPLE = "out_of_sample"
    WALK_FORWARD_TRAIN = "walk_forward_train"
    WALK_FORWARD_TEST = "walk_forward_test"


class DatasetSpec(DomainModel):
    """Which bars an experiment ran over."""

    symbol: Symbol
    timeframe: Text
    start: UtcDatetime
    end: UtcDatetime
    source: Text
    """Where the bars came from, so two runs over different vintages of the same range are
    two experiments rather than one that disagrees with itself."""

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        """Check the range runs forwards.

        Raises:
            ValueError: If the range ends before it starts.
        """
        if self.end <= self.start:
            msg = "a dataset's end must follow its start"
            raise ValueError(msg)
        return self


class StrategySpec(DomainModel):
    """Which strategy an experiment ran, named rather than imported.

    The definition never holds a class. A harness that imported strategy code to describe an
    experiment would be a second place where strategies are wired, and the composition root
    is already the first.
    """

    strategy_id: StrategyId
    strategy_version: SemanticVersion
    params: tuple[tuple[Text, Text], ...] = ()
    """Parameters as an ordered tuple of pairs, not a mapping.

    A mapping serialises in insertion order, so two definitions built from the same values in
    a different order would hash differently for a reason invisible in the file. Ordering is
    therefore part of the identity, and stated as such rather than left to chance.
    """


class ExperimentDefinition(DomainModel):
    """Everything needed to run one experiment and to recognise it again."""

    name: Text
    dataset: DatasetSpec
    strategy: StrategySpec
    risk: RiskConfiguration
    """Risk and position management alike. Trailing, break-even, take-profit and holding
    limits live here already; a second block for them would be a second source of truth."""

    backtest: BacktestConfig
    role: ExperimentRole = ExperimentRole.IN_SAMPLE
    """What this run claims to be. Part of the identifier, so the claim cannot be restated
    after the fact without becoming a different experiment."""

    regime_label: Text | None = None

    @model_validator(mode="after")
    def _validate_role(self) -> Self:
        """Check the claimed role is one this configuration is entitled to make.

        Raises:
            ValueError: If the frozen benchmark claims to be out-of-sample.
        """
        if (
            self.strategy.strategy_id == BENCHMARK_STRATEGY_ID
            and self.role is ExperimentRole.OUT_OF_SAMPLE
        ):
            msg = (
                f"{BENCHMARK_STRATEGY_ID} is the frozen benchmark and was observed "
                "throughout week 5: it cannot be declared clean out-of-sample evidence"
            )
            raise ValueError(msg)
        return self

    @property
    def experiment_id(self) -> str:
        """Return this definition's derived name."""
        return experiment_id(self)


def canonical_json(definition: ExperimentDefinition) -> str:
    """Return the definition's canonical form, byte-stable across processes.

    Pydantic serialises in field-declaration order, and every collection in a definition is a
    tuple rather than a set — so nothing here depends on Python's per-process hash
    randomisation. That is a property of the models rather than of this function, which is
    why a test proves it in a fresh interpreter under several hash seeds instead of trusting
    the observation.
    """
    return definition.model_dump_json()


def experiment_id(definition: ExperimentDefinition) -> str:
    """Return the identifier derived from the whole definition."""
    digest = hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()
    return digest[:_IDENTIFIER_LENGTH]
