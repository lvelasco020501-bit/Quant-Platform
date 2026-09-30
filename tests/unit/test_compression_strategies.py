"""M33's three compression rules: what each one refuses to trade is the whole point.

A breakout strategy that ignored its compression filter would be ``breakout``, which this
project has already measured. So the tests that carry weight here are the negative ones — a
breakout with no squeeze behind it must produce silence, and a squeeze with no breakout must
produce silence too — because either failure would quietly turn M33 into a re-run of M13.

``test_the_paper_visible_registry_is_unchanged`` is the safety test. Two live paper sessions
load strategies from the default registry, and nothing added for research may appear in it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.errors import StrategyParameterError
from quantplatform.core.models.signals import StrategyContext
from quantplatform.strategies.registry import build_default_registry
from quantplatform.strategies.research import (
    BollingerSqueezeBreakoutStrategy,
    RangeCompressionBreakoutStrategy,
    VolatilityCompressionBreakoutStrategy,
    build_research_registry,
)
from tests.factories import SYMBOL, make_bar, make_bars, make_symbol_rules

SQUEEZED: dict[str, Decimal] = {
    # stdev_24/sma_24 over stdev_168/sma_168 is 0.1/100 over 0.5/100 = 0.2, and the random-walk
    # scale for 24 against 168 is 0.3780, so the statistic is 0.529: stiller than noise.
    "stdev_24": Decimal("0.1"),
    "sma_24": Decimal(100),
    "stdev_168": Decimal("0.5"),
    "sma_168": Decimal(100),
    "donchian_high_20": Decimal(100),
    "donchian_low_10": Decimal(90),
}
LOOSE: dict[str, Decimal] = {**SQUEEZED, "stdev_24": Decimal("0.4")}
"""The same market with four times the short-window dispersion: statistic 2.1, noisier than
noise, so no squeeze."""

BASE_PARAMS: dict[str, object] = {
    "short_window": 24,
    "long_window": 168,
    "entry_lookback": 20,
    "exit_lookback": 10,
    "max_compression": Decimal(1),
}


def context(
    features: dict[str, Decimal],
    *,
    high: Decimal = Decimal(101),
    low: Decimal = Decimal(95),
    state: PositionState = PositionState.FLAT,
) -> StrategyContext:
    """Return a context whose last bar has the given high and low, with the given features."""
    history = make_bars(tuple(Decimal(100) for _ in range(3)), timeframe=Timeframe.D1)
    # Close and open sit at the low so the bar validates: a MarketBar requires its high to be
    # the maximum of the four prices, which a close of 100 under a high of 99 would not be.
    latest = make_bar(
        index=len(history),
        close=low,
        open_price=low,
        high=high,
        low=low,
        timeframe=Timeframe.D1,
    )
    return StrategyContext(
        symbol=SYMBOL,
        market_type=MarketType.SPOT,
        timeframe=Timeframe.D1,
        as_of=latest.close_time,
        bars=(*history, latest),
        features=features,
        position_state=state,
        symbol_rules=make_symbol_rules(),
    )


def bb(**overrides: object) -> BollingerSqueezeBreakoutStrategy:
    return build_research_registry().create("bb_squeeze", {**BASE_PARAMS, **overrides})  # type: ignore[return-value]


def vol(**overrides: object) -> VolatilityCompressionBreakoutStrategy:
    params = {
        "short_vol": 24,
        "long_vol": 168,
        "entry_lookback": 20,
        "exit_lookback": 10,
        "max_compression": Decimal(1),
    }
    return build_research_registry().create("vol_compression", {**params, **overrides})  # type: ignore[return-value]


def rng(**overrides: object) -> RangeCompressionBreakoutStrategy:
    return build_research_registry().create("range_compression", {**BASE_PARAMS, **overrides})  # type: ignore[return-value]


# --- The safety boundary --------------------------------------------------------------------------


def test_the_paper_visible_registry_is_unchanged() -> None:
    # Two live paper sessions load from this registry. Nothing added for research may appear in
    # it, whatever else M33 does.
    assert sorted(build_default_registry()) == ["breakout", "breakout_trend", "ema_trend"]


def test_the_three_rules_exist_only_in_the_research_registry() -> None:
    research = build_research_registry()
    for strategy_id in ("bb_squeeze", "vol_compression", "range_compression"):
        assert strategy_id in research
        assert strategy_id not in build_default_registry()


# --- Compression without a breakout is not a trade ------------------------------------------------


def test_a_squeeze_alone_does_not_open_a_position() -> None:
    # The high sits below the channel, so there is nothing to break out of. A rule that entered
    # here would be trading stillness rather than the move out of it.
    assert bb().generate(context(SQUEEZED, high=Decimal(99))) == ()


def test_a_volatility_lull_alone_does_not_open_a_position() -> None:
    features = {"volratio_24_168": Decimal("0.4"), "donchian_high_20": Decimal(100)}
    assert vol().generate(context(features, high=Decimal(99))) == ()


def test_a_tight_range_alone_does_not_open_a_position() -> None:
    features = {
        "donchian_high_24": Decimal(101),
        "donchian_low_24": Decimal(100),
        "donchian_high_168": Decimal(150),
        "donchian_low_168": Decimal(50),
        "donchian_high_20": Decimal(100),
    }
    assert rng().generate(context(features, high=Decimal(99))) == ()


# --- A breakout without compression is not a trade either -----------------------------------------


def test_a_breakout_without_a_squeeze_is_refused() -> None:
    # This is the test that stops M33 being a re-run of M13's breakout. The price breaks the
    # channel exactly as it does in the passing case; only the dispersion differs.
    assert bb().generate(context(LOOSE, high=Decimal(101))) == ()


def test_a_breakout_without_a_volatility_lull_is_refused() -> None:
    features = {"volratio_24_168": Decimal("1.4"), "donchian_high_20": Decimal(100)}
    assert vol().generate(context(features, high=Decimal(101))) == ()


def test_a_breakout_without_a_tight_range_is_refused() -> None:
    # A 24-bar range of 60 against a 168-bar range of 100 is 0.6, well above the 0.378 a random
    # walk gives, so the statistic is 1.59 and the market is not compressed at all.
    features = {
        "donchian_high_24": Decimal(160),
        "donchian_low_24": Decimal(100),
        "donchian_high_168": Decimal(200),
        "donchian_low_168": Decimal(100),
        "donchian_high_20": Decimal(100),
    }
    assert rng().generate(context(features, high=Decimal(101))) == ()


# --- Both together is a trade ---------------------------------------------------------------------


def test_a_breakout_out_of_a_squeeze_enters_long() -> None:
    signals = bb().generate(context(SQUEEZED, high=Decimal(101)))

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG


def test_a_breakout_out_of_a_volatility_lull_enters_long() -> None:
    features = {"volratio_24_168": Decimal("0.4"), "donchian_high_20": Decimal(100)}

    signals = vol().generate(context(features, high=Decimal(101)))

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG


def test_a_breakout_out_of_a_tight_range_enters_long() -> None:
    features = {
        "donchian_high_24": Decimal(101),
        "donchian_low_24": Decimal(100),
        "donchian_high_168": Decimal(150),
        "donchian_low_168": Decimal(50),
        "donchian_high_20": Decimal(100),
    }

    signals = rng().generate(context(features, high=Decimal(101)))

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG


# --- The stricter threshold is stricter -----------------------------------------------------------


def test_the_tighter_threshold_refuses_what_the_looser_one_takes() -> None:
    # The only dimension that moves between a rule's two declared variants. SQUEEZED sits at a
    # statistic of 0.529, so it clears 1.0 and fails 0.6667 x 0.529... no: it clears both. A
    # statistic between the two thresholds separates them.
    between = {**SQUEEZED, "stdev_24": Decimal("0.16")}  # statistic ~0.847

    assert bb(max_compression=Decimal(1)).generate(context(between, high=Decimal(101)))
    assert bb(max_compression=Decimal("0.6667")).generate(context(between, high=Decimal(101))) == ()


# --- Exits ignore the filter ----------------------------------------------------------------------


def test_the_exit_fires_on_a_new_low_whatever_the_compression() -> None:
    # An exit that consulted the squeeze could leave a losing position open because the market
    # had become noisy, which is the one moment it must not.
    signals = bb().generate(context(LOOSE, low=Decimal(89), state=PositionState.LONG))

    assert len(signals) == 1
    assert signals[0].action is SignalAction.EXIT_LONG


def test_a_position_is_held_while_the_low_holds() -> None:
    assert bb().generate(context(SQUEEZED, low=Decimal(95), state=PositionState.LONG)) == ()


# --- Warm-up is silence, not a trade --------------------------------------------------------------


@pytest.mark.parametrize(
    "missing", ["stdev_24", "sma_24", "stdev_168", "sma_168", "donchian_high_20"]
)
def test_a_missing_feature_produces_silence(missing: str) -> None:
    features = {name: value for name, value in SQUEEZED.items() if name != missing}

    assert bb().generate(context(features, high=Decimal(101))) == ()


def test_a_zero_denominator_produces_silence_rather_than_an_error() -> None:
    # sma cannot be zero for a real market, and an unguarded division would end the run rather
    # than skip the bar.
    assert bb().generate(context({**SQUEEZED, "sma_168": Decimal(0)}, high=Decimal(101))) == ()
    flat_range = {
        "donchian_high_24": Decimal(100),
        "donchian_low_24": Decimal(100),
        "donchian_high_168": Decimal(100),
        "donchian_low_168": Decimal(100),
        "donchian_high_20": Decimal(100),
    }
    assert rng().generate(context(flat_range, high=Decimal(101))) == ()


# --- The contract is derived per instance ---------------------------------------------------------


def test_each_instance_declares_the_features_its_own_numbers_read() -> None:
    assert bb(short_window=12).metadata.required_features == (
        "stdev_12",
        "sma_12",
        "stdev_168",
        "sma_168",
        "donchian_high_20",
        "donchian_low_10",
    )
    assert vol(short_vol=48).metadata.required_features == (
        "volratio_48_168",
        "donchian_high_20",
        "donchian_low_10",
    )


def test_warm_up_is_the_deepest_window_the_instance_reads() -> None:
    assert bb().metadata.required_history == 168
    assert vol().metadata.required_history == 169
    assert rng().metadata.required_history == 169


def test_a_configuration_whose_windows_collide_names_each_feature_once() -> None:
    # entry_lookback equal to short_window would otherwise declare donchian_high_24 twice.
    names = rng(short_window=24, entry_lookback=24).metadata.required_features

    assert len(names) == len(set(names))
    assert "donchian_high_24" in names


# --- The declared parameter domain ----------------------------------------------------------------


def test_a_short_window_must_be_shorter_than_the_long_one() -> None:
    # The registry wraps pydantic's complaint, so the failure an operator sees names the
    # strategy rather than only the field.
    with pytest.raises(StrategyParameterError, match="validation"):
        bb(short_window=168, long_window=168)
    with pytest.raises(StrategyParameterError, match="validation"):
        vol(short_vol=200, long_vol=168)


def test_a_threshold_above_one_would_switch_the_filter_off_and_is_refused() -> None:
    # Above one the rule would admit a market noisier than noise, which is not a looser filter
    # but no filter, and would make the variant indistinguishable from plain breakout.
    with pytest.raises(StrategyParameterError):
        bb(max_compression=Decimal("1.5"))
    with pytest.raises(StrategyParameterError):
        bb(max_compression=Decimal(0))
