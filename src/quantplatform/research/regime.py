"""Labelling bars without ever seeing the future, and measuring by regime without picking one.

A regime label assigned to bar T using anything at or after T is not a label — it is a
retrospective story a classifier gets to tell because it already knows how the story ends.
Terciles cut over a whole sample, a volatility window that reaches forward, a "high vol"
threshold set by looking at the run's own worst days: all of these are the same mistake
wearing a different name.

The defence here is structural rather than a rule to remember. There is exactly one place a
:class:`RegimeLabeller` is ever called — :func:`label_series` — and the history it hands over
on each call is a slice of the array *up to the loop's current position*. The bars after that
position are not merely withheld; at the moment of the call, this function has not read them
yet. A labeller cannot be handed what the driver has not looked at.

No concrete label lives in this module, and no concrete labeller lives in this package. The
only implementation this milestone ships is a test fixture that returns one constant label —
useful for proving the plumbing works, useless for inventing an edge.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

from pydantic import model_validator

from quantplatform.core.errors import StrategyNotFoundError
from quantplatform.core.models.base import DomainModel, RegimeLabel, Text
from quantplatform.research.canonical import canonical_json
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.folds import WindowSpec
from quantplatform.research.ledger import VariationKind
from quantplatform.research.plan_runner import FATAL_ERRORS
from quantplatform.research.robustness import (
    DistributionSummary,
    VariationRun,
    summarise_variations,
)
from quantplatform.research.runner import BacktestFactory, ExperimentRunner

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar
    from quantplatform.research.ledger import ExperimentLedger
    from quantplatform.research.store import ResultStore

__all__ = [
    "RegimeEpisode",
    "RegimeLabeller",
    "RegimeLabellerRegistry",
    "RegimeOutcome",
    "RegimePlan",
    "RegimeRunner",
    "derive_episodes",
    "label_series",
    "summarise_regime",
]

_PLAN_ID_LENGTH = 32


@runtime_checkable
class RegimeLabeller(Protocol):
    """Assigns a regime label to one bar, from what was known up to it.

    Never call this directly with a hand-picked ``history`` — call :func:`label_series`
    instead, which is the only place in the platform that constructs these calls and the only
    place that can guarantee ``history`` never reaches forward.
    """

    def label(self, *, bar: MarketBar, history: Sequence[MarketBar]) -> RegimeLabel:
        """Return the label for ``bar``.

        Args:
            bar: The bar being labelled.
            history: Every closed bar strictly before ``bar``, oldest first. Never contains
                ``bar`` itself or anything after it.
        """
        ...


def label_series(labeller: RegimeLabeller, bars: Sequence[MarketBar]) -> tuple[RegimeLabel, ...]:
    """Label every bar in ``bars``, in order, never showing a labeller the future.

    The only place a :class:`RegimeLabeller` is invoked. ``history`` is ``bars[:index]`` at
    the exact loop position labelling ``bars[index]`` — not a copy filtered by a timestamp
    comparison that could carry an off-by-one, but a slice whose end *is* the position in the
    loop. The bars after it have not been reached yet, so they cannot leak by accident.

    Args:
        labeller: Assigns one label per bar.
        bars: The series to label, oldest first.

    Returns:
        One label per bar, in the same order.
    """
    return tuple(labeller.label(bar=bar, history=bars[:index]) for index, bar in enumerate(bars))


class RegimeEpisode(DomainModel):
    """One contiguous run of bars sharing a single label."""

    label: RegimeLabel
    window: WindowSpec


def derive_episodes(
    bars: Sequence[MarketBar], labels: Sequence[RegimeLabel]
) -> tuple[RegimeEpisode, ...]:
    """Group a labelled bar series into contiguous same-label episodes.

    Args:
        bars: The series that was labelled, oldest first.
        labels: One label per bar, from :func:`label_series` over the same ``bars``.

    Returns:
        One episode per contiguous run of equal labels. Each episode's window starts at its
        first bar's open time and ends at the open time of the bar immediately after it — or,
        for the final episode, at its last bar's close time, since there is no next bar to
        take a boundary from.

    Raises:
        ValueError: If ``bars`` is empty, or ``labels`` is not exactly as long as ``bars``.
    """
    if not bars:
        msg = "cannot derive episodes from an empty series"
        raise ValueError(msg)
    if len(labels) != len(bars):
        msg = "labels must be exactly as long as the bars they label"
        raise ValueError(msg, {"bars": len(bars), "labels": len(labels)})

    episodes: list[RegimeEpisode] = []
    start_index = 0
    for index in range(1, len(bars) + 1):
        at_boundary = index == len(bars) or labels[index] != labels[start_index]
        if at_boundary:
            end = bars[index].open_time if index < len(bars) else bars[index - 1].close_time
            episodes.append(
                RegimeEpisode(
                    label=labels[start_index],
                    window=WindowSpec(start=bars[start_index].open_time, end=end),
                )
            )
            start_index = index
    return tuple(episodes)


class RegimePlan(DomainModel):
    """Every episode to run, declared before anything runs.

    Built by calling :func:`label_series` and :func:`derive_episodes` once, offline, and
    freezing the result — the labeller is never invoked again once a plan exists, which is
    what stops execution-time re-slicing from becoming a second, less scrutinised place
    leakage could enter.
    """

    base_experiment_id: Text
    labeller_id: Text
    """Which registered labeller produced this plan — recorded for audit, never re-invoked
    from here."""

    episodes: tuple[RegimeEpisode, ...]

    @property
    def plan_id(self) -> str:
        """Return the identifier derived from the base experiment and every episode."""
        return hashlib.sha256(canonical_json(self).encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        """Check the plan holds at least one episode and none of them overlap.

        Raises:
            ValueError: If the plan is empty, or two episode windows overlap — two episodes
                covering the same bars would count part of the series twice.
        """
        if not self.episodes:
            msg = "a regime plan needs at least one episode"
            raise ValueError(msg)
        for earlier, later in zip(self.episodes, self.episodes[1:], strict=False):
            if later.window.start < earlier.window.end:
                msg = "episode windows must not overlap"
                raise ValueError(msg)
        return self


class RegimeLabellerRegistry:
    """Maps a labeller id to an implementation — the same shape as the strategy registry.

    Empty by default. No concrete labeller ships in this package; registering one is a
    decision an operator makes with evidence, the same way registering a strategy is.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._labellers: dict[str, RegimeLabeller] = {}

    def register(self, labeller_id: str, labeller: RegimeLabeller) -> None:
        """Add a labeller under an id, refusing to shadow one already registered.

        Raises:
            ValueError: If ``labeller_id`` is already registered.
        """
        if labeller_id in self._labellers:
            msg = f"a labeller is already registered under {labeller_id!r}"
            raise ValueError(msg)
        self._labellers[labeller_id] = labeller

    def get(self, labeller_id: str) -> RegimeLabeller:
        """Return the registered labeller.

        Raises:
            StrategyNotFoundError: If no labeller is registered under this id. Reused rather
                than a bespoke error: an unregistered labeller and an unregistered strategy
                are the same shape of mistake — a name that does not resolve to code.
        """
        try:
            return self._labellers[labeller_id]
        except KeyError as exc:
            msg = f"no regime labeller is registered under {labeller_id!r}"
            raise StrategyNotFoundError(msg, labeller_id=labeller_id) from exc


class RegimeOutcome(DomainModel):
    """Everything a regime plan produced, and whether it finished."""

    plan_id: Text
    episodes: tuple[VariationRun, ...] = ()
    aborted: bool = False
    abort_reason: Text | None = None

    def summarise(self) -> dict[RegimeLabel, DistributionSummary]:
        """Summarise every regime separately — never pooled, never one number.

        Raises:
            ValueError: If the plan was aborted before every episode ran.
        """
        if self.aborted:
            msg = (
                "this plan was aborted before it finished and cannot be summarised: "
                f"{self.abort_reason}"
            )
            raise ValueError(msg)
        return summarise_regime(self.episodes, expected_plan_id=self.plan_id)


def summarise_regime(
    runs: Sequence[VariationRun], *, expected_plan_id: str
) -> dict[RegimeLabel, DistributionSummary]:
    """Group runs by the regime label their own definition carries, and summarise each group.

    Args:
        runs: Every episode's evidence, from one plan.
        expected_plan_id: The plan every run must belong to.

    Returns:
        One :class:`~quantplatform.research.robustness.DistributionSummary` per label. Labels
        are never combined: a trending episode and a ranging episode are different questions,
        and pooling them would answer neither honestly.
    """
    by_label: dict[RegimeLabel, list[VariationRun]] = {}
    for run in runs:
        label = run.result.definition.regime_label
        if label is None:
            msg = "every regime run must carry the label its episode ran under"
            raise ValueError(msg)
        by_label.setdefault(label, []).append(run)
    return {
        label: summarise_variations(group, expected_plan_id=expected_plan_id)
        for label, group in by_label.items()
    }


def _for_episode(base: ExperimentDefinition, episode: RegimeEpisode) -> ExperimentDefinition:
    """Return the base definition narrowed to one episode's window, labelled with its regime."""
    dataset = base.dataset.model_copy(
        update={"start": episode.window.start, "end": episode.window.end}
    )
    return base.model_copy(update={"dataset": dataset, "regime_label": episode.label})


class RegimeRunner:
    """Runs every episode of a regime plan, recording all of them."""

    def __init__(self, *, runner: ExperimentRunner | None = None) -> None:
        """Wire a regime runner over the single-experiment runner it delegates to."""
        self._runner = runner if runner is not None else ExperimentRunner()

    def run(
        self,
        base: ExperimentDefinition,
        plan: RegimePlan,
        *,
        bars: Sequence[MarketBar],
        factory: BacktestFactory,
        store: ResultStore,
        ledger: ExperimentLedger,
        code_revision: str | None = None,
    ) -> RegimeOutcome:
        """Run every episode, over the same series the plan's episodes were derived from.

        Args:
            base: The configuration every episode narrows.
            plan: The episodes to run, already validated when the plan was built.
            bars: The full series the plan's episodes were derived from — each episode's own
                bars are sliced from this by its window.
            factory: Builds the engine for one definition.
            store: Where each attempt's evidence is written.
            ledger: Where each attempt is recorded, with its lineage.
            code_revision: Revision of the code being run, or ``None`` when unknown.

        Returns:
            What every episode produced, and whether an integrity failure ended the plan
            early.
        """
        plan_id = plan.plan_id
        runs: list[VariationRun] = []
        for episode in plan.episodes:
            definition = _for_episode(base, episode)
            episode_bars = tuple(bar for bar in bars if episode.window.contains(bar.open_time))
            result = self._runner.run(
                definition, bars=episode_bars, factory=factory, code_revision=code_revision
            )
            entry = ledger.record(
                result,
                store=store,
                derived_from=base.experiment_id,
                variation_kind=VariationKind.REGIME,
                variation_plan_id=plan_id,
            )
            runs.append(VariationRun(entry=entry, result=result))
            if result.error_type in FATAL_ERRORS:
                return RegimeOutcome(
                    plan_id=plan_id,
                    episodes=tuple(runs),
                    aborted=True,
                    abort_reason=result.error or result.error_type,
                )
        return RegimeOutcome(plan_id=plan_id, episodes=tuple(runs))
