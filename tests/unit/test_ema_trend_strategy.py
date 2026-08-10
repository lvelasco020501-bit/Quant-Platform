"""Pre-7C: the one production strategy, and the EMA features it reads.

The tests assert *behaviour under stated conditions*, never that the rule is any good. A
crossover filter has no edge on account of being published, and nothing here claims one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.errors import ConfigurationError, StrategyParameterError
from quantplatform.core.models.signals import StrategyContext
from quantplatform.features import ExponentialMovingAverageFeatures
from quantplatform.strategies.ema_trend import EmaTrendParameters, EmaTrendStrategy
from quantplatform.strategies.registry import BUILTIN_STRATEGIES, build_default_registry
from tests.factories import make_bars, make_context


def _strategy(**parameters: int) -> EmaTrendStrategy:
    return EmaTrendStrategy(EmaTrendParameters(**parameters))


def _context(*, fast: str, slow: str, position: PositionState) -> StrategyContext:
    return make_context(
        closes=[Decimal(50_000)] * 50,
        features={"ema_20": Decimal(fast), "ema_50": Decimal(slow)},
        position_state=position,
    )


# --- Parameters ---------------------------------------------------------------------------------


def test_the_default_periods_are_the_documented_ones() -> None:
    parameters = EmaTrendParameters()

    assert parameters.fast_period == 20
    assert parameters.slow_period == 50
    assert parameters.fast_feature == "ema_20"
    assert parameters.slow_feature == "ema_50"


def test_parameters_are_typed_and_frozen() -> None:
    parameters = EmaTrendParameters()

    with pytest.raises(ValueError, match="frozen"):
        parameters.fast_period = 5  # type: ignore[misc]
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        EmaTrendParameters(lookback=5)  # type: ignore[call-arg]


def test_a_fast_period_that_is_not_faster_is_refused() -> None:
    # Equal periods make the averages identical and the strategy silent for ever, which is
    # a configuration mistake that should not take a week of paper trading to notice.
    with pytest.raises(ValueError, match="strictly shorter"):
        EmaTrendParameters(fast_period=50, slow_period=50)
    with pytest.raises(ValueError, match="strictly shorter"):
        EmaTrendParameters(fast_period=60, slow_period=50)


# --- Contract -----------------------------------------------------------------------------------


def test_the_strategy_declares_what_it_needs() -> None:
    metadata = EmaTrendStrategy.METADATA

    assert metadata.strategy_id == "ema_trend"
    assert metadata.version == "1.0.0"
    assert metadata.required_history == 50
    assert metadata.required_features == ("ema_20", "ema_50")
    assert metadata.supported_timeframes == (Timeframe.H1,)
    assert metadata.supported_market_types == (MarketType.SPOT,)
    assert metadata.allows_short is False
    assert metadata.operates_intrabar is False


def test_the_strategy_is_registered_as_a_builtin() -> None:
    assert EmaTrendStrategy in BUILTIN_STRATEGIES

    registry = build_default_registry()
    resolved = registry.create("ema_trend", {})

    assert isinstance(resolved, EmaTrendStrategy)
    assert isinstance(resolved.parameters, EmaTrendParameters)
    assert resolved.parameters.fast_period == 20


def test_periods_that_contradict_the_declared_contract_are_refused() -> None:
    # The metadata is a class attribute and cannot track an instance's periods. Configuring
    # 9 and 21 would leave the strategy hunting for features nobody promised to compute,
    # and it would go quiet for the whole run rather than fail.
    with pytest.raises(StrategyParameterError, match="do not match the features"):
        build_default_registry().create("ema_trend", {"fast_period": 9, "slow_period": 21})


def test_the_declared_periods_are_accepted() -> None:
    resolved = build_default_registry().create("ema_trend", {"fast_period": 20, "slow_period": 50})

    assert isinstance(resolved.parameters, EmaTrendParameters)
    assert resolved.parameters.fast_period == 20
    assert resolved.parameters.slow_period == 50


# --- Signals ------------------------------------------------------------------------------------


def test_a_bullish_crossover_while_flat_enters_long() -> None:
    signals = _strategy().generate(
        _context(fast="51000", slow="50000", position=PositionState.FLAT)
    )

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG
    assert "above" in signals[0].reason


def test_a_bearish_crossover_while_long_exits() -> None:
    signals = _strategy().generate(
        _context(fast="49000", slow="50000", position=PositionState.LONG)
    )

    assert len(signals) == 1
    assert signals[0].action is SignalAction.EXIT_LONG
    assert "below" in signals[0].reason


def test_a_bullish_reading_while_already_long_says_nothing() -> None:
    # Holding is silence, not a repeated entry: the risk engine would refuse a second
    # position anyway, and an intent per bar would flood the audit trail.
    assert (
        _strategy().generate(_context(fast="51000", slow="50000", position=PositionState.LONG))
        == ()
    )


def test_a_bearish_reading_while_already_flat_says_nothing() -> None:
    assert (
        _strategy().generate(_context(fast="49000", slow="50000", position=PositionState.FLAT))
        == ()
    )


def test_equal_averages_say_nothing() -> None:
    assert (
        _strategy().generate(_context(fast="50000", slow="50000", position=PositionState.FLAT))
        == ()
    )


def test_a_window_too_short_for_an_average_says_nothing() -> None:
    # Warm-up, not misconfiguration: the engine's contract check already confirmed the
    # pipeline can produce these features, so their absence means too few bars.
    context = make_context(closes=[Decimal(50_000)] * 50, features={"ema_20": Decimal(51_000)})

    assert _strategy().generate(context) == ()


def test_the_same_context_always_produces_the_same_signal() -> None:
    # Determinism is the property that makes a paper run reproducible and a regression
    # detectable at all.
    context = _context(fast="51000", slow="50000", position=PositionState.FLAT)
    strategy = _strategy()

    first = strategy.generate(context)
    second = strategy.generate(context)

    assert first == second
    assert first[0].signal_id == second[0].signal_id


def test_a_signal_carries_the_features_that_justified_it() -> None:
    signals = _strategy().generate(
        _context(fast="51000", slow="50000", position=PositionState.FLAT)
    )

    assert signals[0].features == {"ema_20": Decimal(51_000), "ema_50": Decimal(50_000)}


def test_the_strategy_never_emits_a_short_signal() -> None:
    for position in (PositionState.FLAT, PositionState.LONG):
        for fast, slow in (("51000", "50000"), ("49000", "50000")):
            for signal in _strategy().generate(_context(fast=fast, slow=slow, position=position)):
                assert signal.action is not SignalAction.ENTER_SHORT


# --- The EMA feature pipeline -------------------------------------------------------------------


def test_the_pipeline_emits_the_names_the_strategy_declares() -> None:
    pipeline = ExponentialMovingAverageFeatures([20, 50])

    assert set(pipeline.feature_names) >= {"ema_20", "ema_50"}
    assert pipeline.required_history == 50


def test_a_flat_series_averages_to_its_own_level() -> None:
    # The one EMA value that can be checked by inspection.
    pipeline = ExponentialMovingAverageFeatures([5])
    bars = make_bars([Decimal(100)] * 20)

    assert pipeline.compute(bars)["ema_5"] == Decimal(100)


def test_a_rising_series_puts_the_fast_average_above_the_slow_one() -> None:
    pipeline = ExponentialMovingAverageFeatures([5, 20])
    bars = make_bars([Decimal(100 + index) for index in range(40)])

    features = pipeline.compute(bars)

    assert features["ema_5"] > features["ema_20"]


def test_a_falling_series_puts_the_fast_average_below_the_slow_one() -> None:
    pipeline = ExponentialMovingAverageFeatures([5, 20])
    bars = make_bars([Decimal(200 - index) for index in range(40)])

    features = pipeline.compute(bars)

    assert features["ema_5"] < features["ema_20"]


def test_a_period_longer_than_the_window_is_omitted_rather_than_guessed() -> None:
    pipeline = ExponentialMovingAverageFeatures([5, 50])

    features = pipeline.compute(make_bars([Decimal(100)] * 10))

    assert "ema_5" in features
    assert "ema_50" not in features


def test_the_pipeline_is_deterministic_and_stateless() -> None:
    pipeline = ExponentialMovingAverageFeatures([12])
    bars = make_bars([Decimal(100 + index) for index in range(30)])

    assert pipeline.compute(bars) == pipeline.compute(bars)


def test_the_pipeline_refuses_unusable_periods() -> None:
    with pytest.raises(ConfigurationError, match="at least one period"):
        ExponentialMovingAverageFeatures([])
    with pytest.raises(ConfigurationError, match="strictly positive"):
        ExponentialMovingAverageFeatures([0])


def test_an_empty_window_produces_nothing() -> None:
    assert ExponentialMovingAverageFeatures([5]).compute([]) == {}
