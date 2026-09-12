"""The M13 research strategies: their rules, and the contract fix that makes them testable.

Every existing strategy carries its feature requirements on the *class*, so constructing one
with any parameter other than its default is refused. That is why M10c ran no sensitivity
sweep at all — there was no way to build a neighbour. These strategies derive their contract
from their parameters instead, and the engine already reads the contract per instance, so a
neighbour is just a different set of numbers.

Nothing here asserts a rule is profitable. Each test states a market condition and the signal
the rule is defined to give for it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import PositionState, SignalAction
from quantplatform.core.models.market import MarketBar
from quantplatform.features import IndicatorFeatures
from quantplatform.orchestration.features import features_for
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.registry import BUILTIN_STRATEGIES, build_default_registry
from quantplatform.strategies.research import RESEARCH_STRATEGIES, build_research_registry
from tests.factories import make_bar, make_bars, make_context

FLAT = PositionState.FLAT
LONG = PositionState.LONG


def _create(strategy_id: str, **params: object) -> BaseStrategy:
    return build_research_registry().create(strategy_id, {k: str(v) for k, v in params.items()})


def _act(
    strategy: BaseStrategy,
    features: dict[str, str],
    position: PositionState,
    *,
    bar: MarketBar | None = None,
) -> SignalAction | None:
    bars = (*make_bars((Decimal(100),)), bar) if bar is not None else None
    context = make_context(
        bars=bars,
        features={k: Decimal(v) for k, v in features.items()},
        position_state=position,
    )
    signals = strategy.generate(context)
    return signals[0].action if signals else None


# --- Isolation from paper trading ----------------------------------------------------------------


def test_no_research_strategy_is_reachable_from_paper_trading() -> None:
    # The paper runner resolves strategies through the default registry. A research rule that
    # leaked into it would be one mistyped flag away from a live session.
    paper = build_default_registry()
    for strategy_class in RESEARCH_STRATEGIES:
        assert strategy_class not in BUILTIN_STRATEGIES
        assert strategy_class.METADATA.strategy_id not in paper
    assert len(paper) == 2


def test_the_research_registry_still_carries_the_benchmarks() -> None:
    registry = build_research_registry()
    assert "ema_trend" in registry
    assert "breakout" in registry
    assert len(registry) == 2 + len(RESEARCH_STRATEGIES)


def test_every_research_strategy_is_long_only_spot() -> None:
    for strategy_class in RESEARCH_STRATEGIES:
        assert strategy_class.METADATA.allows_short is False


# --- The contract follows the parameters ----------------------------------------------------------


def test_a_neighbour_declares_the_features_its_own_parameters_need() -> None:
    canonical = _create("momentum_roc", lookback=72)
    neighbour = _create("momentum_roc", lookback=48)

    assert canonical.metadata.required_features == ("roc_72",)
    assert neighbour.metadata.required_features == ("roc_48",)
    assert neighbour.metadata.required_history == 49


def test_features_for_builds_exactly_what_a_research_strategy_declares() -> None:
    strategy = _create("breakout_trend", entry_lookback=20, exit_lookback=10, trend_period=200)
    pipeline = features_for(strategy)

    assert set(strategy.metadata.required_features) <= set(pipeline.feature_names)
    assert pipeline.required_history == strategy.metadata.required_history


def test_the_existing_pipelines_are_unchanged_for_the_benchmarks() -> None:
    ema = build_default_registry().create("ema_trend", {})
    assert set(features_for(ema).feature_names) == {"close", "ema_20", "ema_50"}


def test_strategy_side_history_matches_the_pipeline_for_every_indicator() -> None:
    # The strategy package may not import features, so it states warm-up itself. This keeps
    # the two statements from drifting apart.
    for strategy_id, params in _EVERY_CONFIGURATION:
        strategy = _create(strategy_id, **params)
        declared = strategy.metadata.required_features
        indicator = tuple(name for name in declared if IndicatorFeatures.handles(name))
        if indicator:
            needed = IndicatorFeatures(indicator).required_history
            assert strategy.metadata.required_history >= needed
        assert strategy.metadata.required_history == features_for(strategy).required_history


_EVERY_CONFIGURATION: tuple[tuple[str, dict[str, object]], ...] = (
    ("momentum_roc", {"lookback": 72}),
    ("ema_slope", {"period": 50, "slope_bars": 10}),
    ("breakout_trend", {"entry_lookback": 20, "exit_lookback": 10, "trend_period": 200}),
    ("vol_momentum", {"lookback": 72, "vol_window": 72, "threshold": "1.0"}),
    ("zscore_revert", {"window": 48, "entry_z": "-2", "exit_z": "0"}),
    ("bollinger_revert", {"window": 20, "band_z": "2"}),
    ("rsi_reversal", {"period": 14, "oversold": "30", "exit_level": "50"}),
    ("regime_trend", {"lookback": 72, "er_window": 72, "er_min": "0.30"}),
    (
        "regime_revert",
        {"window": 48, "entry_z": "-2", "exit_z": "0", "er_window": 72, "er_max": "0.20"},
    ),
    (
        "regime_switch",
        {
            "lookback": 72,
            "window": 48,
            "entry_z": "-2",
            "exit_z": "0",
            "er_window": 72,
            "er_trend": "0.30",
            "er_range": "0.20",
        },
    ),
    (
        "vol_filtered_momentum",
        {"lookback": 72, "short_vol": 24, "long_vol": 168, "max_ratio": "1.5"},
    ),
)


def test_every_research_strategy_is_exercised_by_this_file() -> None:
    covered = {strategy_id for strategy_id, _ in _EVERY_CONFIGURATION}
    assert covered == {cls.METADATA.strategy_id for cls in RESEARCH_STRATEGIES}


# --- Parameters are validated, and have no defaults -----------------------------------------------


def test_a_research_strategy_refuses_to_guess_its_parameters() -> None:
    with pytest.raises(Exception, match="failed validation"):
        build_research_registry().create("momentum_roc", {})


@pytest.mark.parametrize(
    ("strategy_id", "params"),
    [
        ("zscore_revert", {"window": 48, "entry_z": "0", "exit_z": "-2"}),
        ("rsi_reversal", {"period": 14, "oversold": "60", "exit_level": "50"}),
        (
            "vol_filtered_momentum",
            {"lookback": 72, "short_vol": 168, "long_vol": 24, "max_ratio": "1.5"},
        ),
    ],
)
def test_incoherent_parameters_are_refused(strategy_id: str, params: dict[str, object]) -> None:
    with pytest.raises(Exception, match="failed validation"):
        _create(strategy_id, **params)


# --- Rules --------------------------------------------------------------------------------------


def test_momentum_enters_on_positive_return_and_exits_on_negative() -> None:
    s = _create("momentum_roc", lookback=72)
    assert _act(s, {"roc_72": "0.01"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"roc_72": "-0.01"}, FLAT) is None
    assert _act(s, {"roc_72": "-0.01"}, LONG) is SignalAction.EXIT_LONG
    assert _act(s, {"roc_72": "0.01"}, LONG) is None


def test_ema_slope_needs_both_a_rising_average_and_price_above_it() -> None:
    s = _create("ema_slope", period=50, slope_bars=10)
    # make_context's last close is 51_000
    assert _act(s, {"emab_50": "50000", "emaslope_50_10": "0.001"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"emab_50": "52000", "emaslope_50_10": "0.001"}, FLAT) is None
    assert _act(s, {"emab_50": "50000", "emaslope_50_10": "-0.001"}, LONG) is SignalAction.EXIT_LONG


def test_the_trend_filter_blocks_a_breakout_below_the_long_average() -> None:
    s = _create("breakout_trend", entry_lookback=20, exit_lookback=10, trend_period=200)
    bar = make_bar(index=1, close=Decimal(105), high=Decimal(106), low=Decimal(99))
    levels = {"donchian_high_20": "105", "donchian_low_10": "95"}
    assert _act(s, {**levels, "sma_200": "100"}, FLAT, bar=bar) is SignalAction.ENTER_LONG
    assert _act(s, {**levels, "sma_200": "110"}, FLAT, bar=bar) is None


def test_the_breakout_exit_ignores_the_trend_filter() -> None:
    s = _create("breakout_trend", entry_lookback=20, exit_lookback=10, trend_period=200)
    bar = make_bar(index=1, close=Decimal(96), high=Decimal(99), low=Decimal(94))
    features = {"donchian_high_20": "105", "donchian_low_10": "95", "sma_200": "90"}
    assert _act(s, features, LONG, bar=bar) is SignalAction.EXIT_LONG


def test_volatility_scaled_momentum_needs_a_move_larger_than_its_own_noise() -> None:
    s = _create("vol_momentum", lookback=72, vol_window=72, threshold="1.0")
    # threshold * rvol * sqrt(72) = 0.005 * 8.485 = 0.0424
    assert _act(s, {"roc_72": "0.05", "rvol_72": "0.005"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"roc_72": "0.03", "rvol_72": "0.005"}, FLAT) is None
    assert _act(s, {"roc_72": "-0.001", "rvol_72": "0.005"}, LONG) is SignalAction.EXIT_LONG


def test_zscore_reversion_buys_the_stretch_and_sells_the_mean() -> None:
    s = _create("zscore_revert", window=48, entry_z="-2", exit_z="0")
    assert _act(s, {"zscore_48": "-2.1"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"zscore_48": "-1.9"}, FLAT) is None
    assert _act(s, {"zscore_48": "0"}, LONG) is SignalAction.EXIT_LONG
    assert _act(s, {"zscore_48": "-0.5"}, LONG) is None


def test_bollinger_waits_for_the_close_back_inside_the_band() -> None:
    s = _create("bollinger_revert", window=20, band_z="2")
    assert _act(s, {"zscore_prev_20": "-2.3", "zscore_20": "-1.8"}, FLAT) is SignalAction.ENTER_LONG
    # still outside the band: a falling knife, not a reversal
    assert _act(s, {"zscore_prev_20": "-2.3", "zscore_20": "-2.5"}, FLAT) is None
    assert _act(s, {"zscore_prev_20": "-1.0", "zscore_20": "0.1"}, LONG) is SignalAction.EXIT_LONG


def test_rsi_buys_oversold_and_exits_at_the_midline() -> None:
    s = _create("rsi_reversal", period=14, oversold="30", exit_level="50")
    assert _act(s, {"rsi_14": "25"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"rsi_14": "35"}, FLAT) is None
    assert _act(s, {"rsi_14": "55"}, LONG) is SignalAction.EXIT_LONG


def test_regime_trend_only_trades_momentum_in_an_efficient_market() -> None:
    s = _create("regime_trend", lookback=72, er_window=72, er_min="0.30")
    assert _act(s, {"roc_72": "0.02", "er_72": "0.40"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"roc_72": "0.02", "er_72": "0.10"}, FLAT) is None
    # the exit does not wait for the regime
    assert _act(s, {"roc_72": "-0.02", "er_72": "0.10"}, LONG) is SignalAction.EXIT_LONG


def test_regime_reversion_only_fades_in_a_ranging_market() -> None:
    params = {"window": 48, "entry_z": "-2", "exit_z": "0", "er_window": 72, "er_max": "0.20"}
    s = _create("regime_revert", **params)
    assert _act(s, {"zscore_48": "-2.5", "er_72": "0.10"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"zscore_48": "-2.5", "er_72": "0.50"}, FLAT) is None
    assert _act(s, {"zscore_48": "0.2", "er_72": "0.50"}, LONG) is SignalAction.EXIT_LONG


def test_regime_switch_uses_the_rule_that_fits_the_regime() -> None:
    s = _create("regime_switch", **dict(_EVERY_CONFIGURATION)["regime_switch"])
    trend = {"roc_72": "0.02", "zscore_48": "0.5", "er_72": "0.40"}
    ranging = {"roc_72": "-0.01", "zscore_48": "-2.4", "er_72": "0.10"}
    neither = {"roc_72": "0.02", "zscore_48": "-2.4", "er_72": "0.25"}
    assert _act(s, trend, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, ranging, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, neither, FLAT) is None


def test_the_volatility_filter_blocks_momentum_into_a_spike() -> None:
    params = {"lookback": 72, "short_vol": 24, "long_vol": 168, "max_ratio": "1.5"}
    s = _create("vol_filtered_momentum", **params)
    assert _act(s, {"roc_72": "0.02", "volratio_24_168": "1.2"}, FLAT) is SignalAction.ENTER_LONG
    assert _act(s, {"roc_72": "0.02", "volratio_24_168": "2.0"}, FLAT) is None
    assert _act(s, {"roc_72": "-0.02", "volratio_24_168": "2.0"}, LONG) is SignalAction.EXIT_LONG


@pytest.mark.parametrize(("strategy_id", "params"), _EVERY_CONFIGURATION)
def test_a_missing_feature_is_silence_not_an_error(
    strategy_id: str, params: dict[str, object]
) -> None:
    # Warm-up: the pipeline omits a feature its window cannot yet support.
    strategy = _create(strategy_id, **params)
    for position in (FLAT, LONG):
        assert _act(strategy, {}, position) is None
