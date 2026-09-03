"""The Donchian channel pipeline, and the one property it exists to guarantee.

Every other feature pipeline in this codebase (`MovingAverageFeatures`,
`ExponentialMovingAverageFeatures`) legitimately reads the bar being decided on as part of its
own window — an average that includes today's own close is not look-ahead, it is the strategy
asking "what happened including today". A breakout level is different: the whole point is that
it must exist *before* the bar that tests it, so this pipeline's window is `bars[:-1]`, never
`bars`. The central test below is the one that would catch a regression to `bars` directly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.errors import ConfigurationError
from quantplatform.core.models.market import MarketBar
from quantplatform.features import DonchianChannelFeatures
from tests.factories import make_bar


def _bars_with_highs_and_lows(
    values: list[tuple[Decimal, Decimal]],
) -> tuple[MarketBar, ...]:
    """Build bars whose (high, low) are exactly as given, closes held constant."""
    return tuple(
        make_bar(index=index, close=Decimal(100), high=high, low=low)
        for index, (high, low) in enumerate(values)
    )


def test_the_pipeline_emits_the_names_the_strategy_declares() -> None:
    pipeline = DonchianChannelFeatures([20])

    assert set(pipeline.feature_names) == {"donchian_high_20", "donchian_low_20"}
    assert pipeline.required_history == 21


def test_changing_the_current_bars_high_does_not_change_the_level() -> None:
    # The central guarantee. bars[-1] is the bar being decided on; its own high must never
    # participate in the level tested against it.
    pipeline = DonchianChannelFeatures([3])
    base = _bars_with_highs_and_lows(
        [
            (Decimal(101), Decimal(99)),
            (Decimal(102), Decimal(98)),
            (Decimal(103), Decimal(97)),
            (Decimal(105), Decimal(96)),  # the bar being decided on
        ]
    )
    changed = (*base[:-1], base[-1].model_copy(update={"high": Decimal(999), "low": Decimal(1)}))

    assert pipeline.compute(base)["donchian_high_3"] == pipeline.compute(changed)["donchian_high_3"]
    assert pipeline.compute(base)["donchian_low_3"] == pipeline.compute(changed)["donchian_low_3"]


def test_changing_the_last_prior_bars_high_does_change_the_level() -> None:
    # The complementary proof: the exclusion is exactly one bar, not the whole series.
    pipeline = DonchianChannelFeatures([3])
    base = _bars_with_highs_and_lows(
        [
            (Decimal(101), Decimal(99)),
            (Decimal(102), Decimal(98)),
            (Decimal(103), Decimal(97)),  # last bar strictly before the decided-on one
            (Decimal(105), Decimal(96)),
        ]
    )
    changed = (
        *base[:2],
        base[2].model_copy(update={"high": Decimal(500), "low": Decimal(1)}),
        base[3],
    )

    assert pipeline.compute(base)["donchian_high_3"] != pipeline.compute(changed)["donchian_high_3"]
    assert pipeline.compute(base)["donchian_low_3"] != pipeline.compute(changed)["donchian_low_3"]


def test_a_flat_series_produces_that_constant_level() -> None:
    pipeline = DonchianChannelFeatures([3])
    bars = _bars_with_highs_and_lows([(Decimal(100), Decimal(100))] * 5)

    features = pipeline.compute(bars)

    assert features["donchian_high_3"] == Decimal(100)
    assert features["donchian_low_3"] == Decimal(100)


def test_a_window_missing_one_prior_bar_is_omitted() -> None:
    # len(bars) == period exactly: only period - 1 bars are strictly prior, one short.
    pipeline = DonchianChannelFeatures([3])
    bars = _bars_with_highs_and_lows([(Decimal(100), Decimal(100))] * 3)

    assert pipeline.compute(bars) == {}


def test_the_pipeline_is_deterministic_and_stateless() -> None:
    pipeline = DonchianChannelFeatures([5])
    bars = _bars_with_highs_and_lows(
        [(Decimal(100 + index), Decimal(90 + index)) for index in range(10)]
    )

    assert pipeline.compute(bars) == pipeline.compute(bars)


def test_the_pipeline_refuses_unusable_periods() -> None:
    with pytest.raises(ConfigurationError, match="at least one period"):
        DonchianChannelFeatures([])
    with pytest.raises(ConfigurationError, match="strictly positive"):
        DonchianChannelFeatures([0])


def test_an_empty_or_single_bar_window_produces_nothing() -> None:
    pipeline = DonchianChannelFeatures([3])

    assert pipeline.compute(()) == {}
    assert pipeline.compute(_bars_with_highs_and_lows([(Decimal(100), Decimal(100))])) == {}


def test_a_worked_example_by_hand() -> None:
    pipeline = DonchianChannelFeatures([3])
    bars = _bars_with_highs_and_lows(
        [
            (Decimal(110), Decimal(90)),  # highest high, lowest low among the prior 3
            (Decimal(105), Decimal(95)),
            (Decimal(108), Decimal(92)),
            (Decimal(107), Decimal(93)),  # the bar being decided on; excluded from the level
        ]
    )

    features = pipeline.compute(bars)

    assert features["donchian_high_3"] == Decimal(110)
    assert features["donchian_low_3"] == Decimal(90)


def test_two_distinct_periods_both_compute_in_one_call() -> None:
    pipeline = DonchianChannelFeatures([3, 5])
    bars = _bars_with_highs_and_lows(
        [(Decimal(100 + index), Decimal(90 + index)) for index in range(6)]
    )

    features = pipeline.compute(bars)

    assert {"donchian_high_3", "donchian_low_3", "donchian_high_5", "donchian_low_5"} <= set(
        features
    )
