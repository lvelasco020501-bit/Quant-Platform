"""Windows declared in advance, and the definitions that run over them.

**Walk-forward here is a measurement instrument, not a fitting one.** Nothing is selected, so
a fold's training window and its test window run the identical configuration and leakage is
impossible by construction rather than by vigilance.

That also bounds what a good result means, and the bound is easy to forget: a plan that comes
out well demonstrates **stability across windows**, not clean out-of-sample edge. The
configuration was chosen by a person who had already seen every window, and no partition of
data undoes that. Reading a healthy walk-forward as out-of-sample evidence is the most
expensive misunderstanding available at this stage.

The word "train" is protocol vocabulary. Today it fits nothing.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Self

from pydantic import Field, model_validator

from quantplatform.core.models.base import DomainModel, Text, UtcDatetime
from quantplatform.research.definition import ExperimentDefinition, ExperimentRole

if TYPE_CHECKING:
    from datetime import datetime

__all__ = ["Fold", "WalkForwardPlan", "WindowSpec", "derive_fold_definitions"]

_PLAN_ID_LENGTH = 32


class WindowSpec(DomainModel):
    """A half-open span of time, ``[start, end)``.

    Half-open on purpose. With a closed upper bound the bar sitting exactly on a boundary
    would belong to the training window and to the test window at once — leakage of precisely
    one bar, invisible in every summary it feeds.
    """

    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _validate_span(self) -> Self:
        """Check the window runs forwards and holds something.

        Raises:
            ValueError: If the window ends at or before it starts.
        """
        if self.end <= self.start:
            msg = "a window's end must follow its start"
            raise ValueError(msg)
        return self

    def contains(self, moment: datetime) -> bool:
        """Return whether an instant falls inside this window."""
        return self.start <= moment < self.end


class Fold(DomainModel):
    """One training window and the window that follows it."""

    index: int = Field(ge=0)
    train: WindowSpec
    test: WindowSpec

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        """Check the test window does not precede the window it is paired with.

        Raises:
            ValueError: If the test window starts before the training window ends.
        """
        if self.test.start < self.train.end:
            msg = "a fold's test window may not begin before its train window ends"
            raise ValueError(msg)
        return self


class WalkForwardPlan(DomainModel):
    """Every fold, declared before anything is run."""

    base_experiment_id: Text
    folds: tuple[Fold, ...]

    @property
    def plan_id(self) -> str:
        """Return the identifier derived from the base experiment and every window."""
        payload = self.model_dump_json()
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        """Check the plan is complete, ordered, and free of overlapping test windows.

        Every check happens here rather than during a run: a plan that cannot be honoured is
        a mistake in the protocol, and discovering it after several folds have executed would
        mean deciding what to do with results that should never have been produced.

        Raises:
            ValueError: If the plan holds no folds, if its indices are not contiguous from
                zero, or if two test windows cover any of the same time.
        """
        if not self.folds:
            msg = "a walk-forward plan needs at least one fold"
            raise ValueError(msg)
        if [fold.index for fold in self.folds] != list(range(len(self.folds))):
            msg = "fold indices must be contiguous from zero and in order"
            raise ValueError(msg)
        for earlier, later in zip(self.folds, self.folds[1:], strict=False):
            if later.test.start < earlier.test.end:
                msg = (
                    "test windows must not overlap: two folds covering the same days would "
                    "count that period twice"
                )
                raise ValueError(msg)
        return self


def derive_fold_definitions(
    base: ExperimentDefinition, plan: WalkForwardPlan
) -> tuple[tuple[ExperimentDefinition, ExperimentDefinition], ...]:
    """Return the pair of definitions each fold runs.

    The same configuration twice, over two windows, with two roles. Nothing passes from the
    first to the second because nothing is fitted — which is what makes leakage structurally
    impossible in this phase rather than a thing to be watched for.

    Lineage is deliberately absent from what comes back. Which plan a definition belongs to is
    recorded in the ledger, not in the definition: putting it here would make the same window
    run alone and run inside a plan two different experiments, when what differs is *why* they
    were run rather than *what* was run.

    Args:
        base: The configuration every fold shares.
        plan: The windows, already validated.

    Returns:
        One ``(train, test)`` pair per fold, in fold order.
    """
    return tuple(
        (
            _for_window(base, fold.train, ExperimentRole.WALK_FORWARD_TRAIN),
            _for_window(base, fold.test, ExperimentRole.WALK_FORWARD_TEST),
        )
        for fold in plan.folds
    )


def _for_window(
    base: ExperimentDefinition, window: WindowSpec, role: ExperimentRole
) -> ExperimentDefinition:
    """Return the base definition narrowed to one window and claiming one role."""
    dataset = base.dataset.model_copy(update={"start": window.start, "end": window.end})
    return base.model_copy(update={"dataset": dataset, "role": role})
