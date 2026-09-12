"""Indicator features for the M13 research families.

Every value here is checked against arithmetic done by hand on a handful of closes, because
the only thing worse than a strategy with no edge is a strategy whose edge came from an
indicator computed wrongly. The windows are bounded and end at the bar being decided on, and
the one feature that deliberately looks at the *previous* bar proves the current bar cannot
move it.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from quantplatform.core.errors import ConfigurationError
from quantplatform.features import ExponentialMovingAverageFeatures, IndicatorFeatures
from tests.factories import make_bars


def _closes(*values: int | str) -> tuple[Decimal, ...]:
    return tuple(Decimal(str(value)) for value in values)


def _compute(names: tuple[str, ...], *closes: int | str) -> dict[str, Decimal]:
    return dict(IndicatorFeatures(names).compute(make_bars(_closes(*closes))))


# --- Grammar ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "sma_20",
        "roc_72",
        "stdev_48",
        "zscore_48",
        "zscore_prev_20",
        "rvol_72",
        "rsi_14",
        "er_72",
        "emab_50",
        "emaslope_50_10",
        "volratio_24_168",
    ],
)
def test_the_pipeline_recognises_every_research_indicator(name: str) -> None:
    assert IndicatorFeatures.handles(name)


@pytest.mark.parametrize("name", ["ema_20", "donchian_high_20", "close", "sma_", "roc_x", "rsi_0"])
def test_names_owned_by_other_pipelines_or_malformed_are_not_claimed(name: str) -> None:
    # ema_ and donchian_ belong to the existing pipelines; claiming them here would put two
    # implementations behind one name and let the composite silently pick one.
    assert not IndicatorFeatures.handles(name)


def test_an_unrecognised_name_is_refused_at_construction() -> None:
    with pytest.raises(ConfigurationError):
        IndicatorFeatures(("ema_20",))


def test_the_pipeline_never_emits_close() -> None:
    # The existing EMA pipeline already emits `close`; emitting it here as well would make a
    # composite of the two refuse to build.
    assert "close" not in IndicatorFeatures(("sma_3",)).feature_names


# --- Values, by hand ---------------------------------------------------------------------------


def test_sma_includes_the_current_bar() -> None:
    assert _compute(("sma_3",), 1, 2, 3, 4)["sma_3"] == Decimal(3)


def test_roc_is_the_return_over_n_bars() -> None:
    assert _compute(("roc_2",), 1, 2, 3, 4)["roc_2"] == Decimal(1)  # 4 / 2 - 1


def test_zscore_is_distance_from_the_mean_in_standard_deviations() -> None:
    # window [2, 3, 4]: mean 3, population variance 2/3
    value = _compute(("zscore_3",), 1, 2, 3, 4)["zscore_3"]
    assert abs(value - Decimal(1) / (Decimal(2) / Decimal(3)).sqrt()) < Decimal("1e-20")


def test_the_previous_zscore_cannot_be_moved_by_the_current_bar() -> None:
    base = make_bars(_closes(10, 11, 12, 13))
    moved = (*base[:-1], base[-1].model_copy(update={"close": Decimal(999)}))
    pipeline = IndicatorFeatures(("zscore_prev_3",))

    assert pipeline.compute(base)["zscore_prev_3"] == pipeline.compute(moved)["zscore_prev_3"]


def test_a_flat_window_has_no_zscore_rather_than_a_division_by_zero() -> None:
    assert "zscore_3" not in _compute(("zscore_3",), 5, 5, 5, 5)


def test_rsi_with_no_losses_is_one_hundred() -> None:
    assert _compute(("rsi_3",), 1, 2, 3, 4)["rsi_3"] == Decimal(100)


def test_rsi_is_the_simple_average_form() -> None:
    # changes +2, -1: average gain 1, average loss 0.5, RS 2, RSI 100 - 100/3
    value = _compute(("rsi_2",), 10, 12, 11)["rsi_2"]
    assert abs(value - (Decimal(100) - Decimal(100) / Decimal(3))) < Decimal("1e-20")


def test_efficiency_ratio_is_one_for_a_straight_line_and_zero_for_a_round_trip() -> None:
    assert _compute(("er_3",), 1, 2, 3, 4)["er_3"] == Decimal(1)
    assert _compute(("er_4",), 1, 2, 1, 2, 1)["er_4"] == Decimal(0)


def test_realised_volatility_of_constant_returns_is_zero() -> None:
    assert _compute(("rvol_2",), 100, 110, 121)["rvol_2"] == Decimal(0)


def test_a_volatility_ratio_over_a_silent_long_window_is_omitted() -> None:
    assert "volratio_2_3" not in _compute(("volratio_2_3",), 100, 110, 121, "133.1")


def test_a_window_too_short_omits_the_feature_rather_than_approximating_it() -> None:
    assert _compute(("sma_5", "roc_5"), 1, 2, 3) == {}


def test_required_history_is_the_deepest_window_any_name_needs() -> None:
    pipeline = IndicatorFeatures(("sma_20", "roc_72", "emaslope_50_10", "volratio_24_168"))
    assert pipeline.required_history == 260  # 5 * 50 + 10


# --- The bounded EMA -----------------------------------------------------------------------------


def test_the_bounded_ema_agrees_with_the_full_history_one() -> None:
    # Seeded over a tail of 5 * period bars rather than the whole history, so a year-long run
    # does not recompute from the first bar on every call. The seed's weight after that many
    # steps is below 1e-4; this asserts the approximation is invisible at the price level.
    closes = tuple(Decimal(str(round(100 + 10 * math.sin(i / 7) + i * 0.1, 6))) for i in range(400))
    bars = make_bars(closes)
    full = ExponentialMovingAverageFeatures([10]).compute(bars)["ema_10"]
    bounded = IndicatorFeatures(("emab_10",)).compute(bars)["emab_10"]

    assert abs(bounded - full) / full < Decimal("1e-3")


def test_ema_slope_is_positive_on_a_rising_series() -> None:
    bars = make_bars(tuple(Decimal(100 + i) for i in range(80)))
    assert IndicatorFeatures(("emaslope_10_3",)).compute(bars)["emaslope_10_3"] > 0


def test_features_are_deterministic() -> None:
    bars = make_bars(tuple(Decimal(100 + (i % 7)) for i in range(120)))
    pipeline = IndicatorFeatures(("zscore_20", "rsi_14", "er_30", "rvol_24"))
    assert pipeline.compute(bars) == pipeline.compute(bars)
