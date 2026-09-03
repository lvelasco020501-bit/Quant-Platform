"""Labelling never sees the future, and episodes are exactly what the labels say they are.

Nothing here tests a real classifier — there isn't one in this package on purpose. Every
labeller below is deliberately non-financial: parity of an index, or one constant string. What
is under test is the plumbing that would make leakage impossible even for a labeller that
tried to cheat.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from quantplatform.core.models.base import RegimeLabel
from quantplatform.core.models.market import MarketBar
from quantplatform.research.folds import WindowSpec
from quantplatform.research.regime import (
    RegimeEpisode,
    RegimeLabeller,
    RegimePlan,
    derive_episodes,
    label_series,
)
from tests.factories import make_bar


class _SpyLabeller:
    """Records exactly what it was handed, and returns one constant label."""

    def __init__(self) -> None:
        self.calls: list[tuple[MarketBar, tuple[MarketBar, ...]]] = []

    def label(self, *, bar: MarketBar, history: Sequence[MarketBar]) -> RegimeLabel:
        self.calls.append((bar, tuple(history)))
        return "constant"


class _ParityLabeller:
    """Labels by whether the number of bars seen so far (including this one) is even or odd.

    Purely structural — a stand-in for "some deterministic function of history", not a
    financial classifier of any kind.
    """

    def label(self, *, bar: MarketBar, history: Sequence[MarketBar]) -> RegimeLabel:
        del bar
        return "even" if len(history) % 2 == 0 else "odd"


def _bars(count: int) -> tuple[MarketBar, ...]:
    return tuple(make_bar(index=i, close=Decimal(50_000)) for i in range(count))


def test_label_series_returns_one_label_per_bar() -> None:
    labels = label_series(_SpyLabeller(), _bars(5))

    assert len(labels) == 5


def test_a_labeller_never_receives_a_bar_at_or_after_the_one_being_labelled() -> None:
    spy = _SpyLabeller()
    bars = _bars(5)

    label_series(spy, bars)

    for index, (bar, history) in enumerate(spy.calls):
        assert bar == bars[index]
        assert history == bars[:index]
        assert all(seen.open_time < bar.open_time for seen in history)


def test_the_first_bar_is_labelled_with_empty_history() -> None:
    spy = _SpyLabeller()

    label_series(spy, _bars(1))

    (call,) = spy.calls
    assert call[1] == ()


def test_a_deterministic_labeller_reproduces_the_same_labels_on_a_second_pass() -> None:
    bars = _bars(6)

    first = label_series(_ParityLabeller(), bars)
    second = label_series(_ParityLabeller(), bars)

    assert first == second


def test_derive_episodes_groups_contiguous_equal_labels() -> None:
    bars = _bars(4)
    labels: tuple[RegimeLabel, ...] = ("aa", "aa", "bb", "bb")

    episodes = derive_episodes(bars, labels)

    assert [episode.label for episode in episodes] == ["aa", "bb"]
    assert episodes[0].window.start == bars[0].open_time
    assert episodes[0].window.end == bars[2].open_time
    assert episodes[1].window.start == bars[2].open_time
    assert episodes[1].window.end == bars[3].close_time


def test_derive_episodes_handles_a_label_changing_every_bar() -> None:
    bars = _bars(3)
    labels: tuple[RegimeLabel, ...] = ("aa", "bb", "cc")

    episodes = derive_episodes(bars, labels)

    assert [episode.label for episode in episodes] == ["aa", "bb", "cc"]


def test_derive_episodes_rejects_a_length_mismatch() -> None:
    with pytest.raises(ValueError, match="long"):
        derive_episodes(_bars(3), ("aa", "bb"))


def test_derive_episodes_rejects_an_empty_series() -> None:
    with pytest.raises(ValueError, match="empty"):
        derive_episodes((), ())


def test_a_regime_plan_rejects_overlapping_episodes() -> None:
    bars = _bars(4)
    episode_a = RegimeEpisode(
        label="aa", window=WindowSpec(start=bars[0].open_time, end=bars[2].open_time)
    )
    episode_b = RegimeEpisode(
        label="bb", window=WindowSpec(start=bars[1].open_time, end=bars[3].close_time)
    )

    with pytest.raises(ValueError, match="overlap"):
        RegimePlan(base_experiment_id="x", labeller_id="parity", episodes=(episode_a, episode_b))


def test_a_regime_plan_needs_at_least_one_episode() -> None:
    with pytest.raises(ValueError, match="at least one"):
        RegimePlan(base_experiment_id="x", labeller_id="parity", episodes=())


def test_regime_labeller_is_a_runtime_checkable_protocol() -> None:
    assert isinstance(_ParityLabeller(), RegimeLabeller)
