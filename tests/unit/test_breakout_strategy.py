"""The Donchian breakout strategy: entries and exits, never sizing, never stops.

Tests assert behaviour under stated conditions, never that the rule is any good — the same
discipline `test_ema_trend_strategy.py` holds itself to. `entry_lookback=20` and
`exit_lookback=10` are an operational default to make the class constructible, explicitly not
a research finding; nothing here tests whether they perform well, because nothing here runs a
backtest.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.errors import StrategyParameterError
from quantplatform.core.models.signals import StrategyContext
from quantplatform.strategies.breakout import BreakoutParameters, BreakoutStrategy
from quantplatform.strategies.registry import BUILTIN_STRATEGIES, build_default_registry
from tests.factories import make_bar, make_bars, make_context


def _strategy(**parameters: int) -> BreakoutStrategy:
    defaults = {"entry_lookback": 20, "exit_lookback": 10}
    return BreakoutStrategy(BreakoutParameters(**{**defaults, **parameters}))


def _context(
    *,
    current_high: str,
    current_low: str,
    entry_level: str | None = "100",
    exit_level: str | None = "90",
    position: PositionState,
) -> StrategyContext:
    prior = make_bars([Decimal(100)])
    high, low = Decimal(current_high), Decimal(current_low)
    current = make_bar(index=1, close=(high + low) / 2, high=high, low=low)
    features = {}
    if entry_level is not None:
        features["donchian_high_20"] = Decimal(entry_level)
    if exit_level is not None:
        features["donchian_low_10"] = Decimal(exit_level)
    return make_context(bars=(*prior, current), features=features, position_state=position)


# --- Parameters ---------------------------------------------------------------------------------


def test_lookbacks_are_required_with_no_default() -> None:
    with pytest.raises(ValueError, match="Field required"):
        BreakoutParameters()  # type: ignore[call-arg]


def test_parameters_are_typed_and_frozen() -> None:
    parameters = BreakoutParameters(entry_lookback=20, exit_lookback=10)

    with pytest.raises(ValueError, match="frozen"):
        parameters.entry_lookback = 5  # type: ignore[misc]
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        BreakoutParameters(entry_lookback=20, exit_lookback=10, lookback=5)  # type: ignore[call-arg]


def test_feature_names_derive_from_the_lookbacks() -> None:
    parameters = BreakoutParameters(entry_lookback=20, exit_lookback=10)

    assert parameters.entry_feature == "donchian_high_20"
    assert parameters.exit_feature == "donchian_low_10"


# --- Contract -------------------------------------------------------------------------------


def test_the_strategy_declares_what_it_needs() -> None:
    metadata = BreakoutStrategy.METADATA

    assert metadata.strategy_id == "breakout"
    assert metadata.required_history == 21
    assert metadata.required_features == ("donchian_high_20", "donchian_low_10")
    assert metadata.supported_timeframes == (Timeframe.H1,)
    assert metadata.supported_market_types == (MarketType.SPOT,)
    assert metadata.allows_short is False
    assert metadata.operates_intrabar is False


def test_the_strategy_is_registered_as_a_builtin() -> None:
    assert BreakoutStrategy in BUILTIN_STRATEGIES

    registry = build_default_registry()
    resolved = registry.create("breakout", {"entry_lookback": 20, "exit_lookback": 10})

    assert isinstance(resolved, BreakoutStrategy)
    assert isinstance(resolved.parameters, BreakoutParameters)


def test_lookbacks_that_contradict_the_declared_contract_are_refused() -> None:
    with pytest.raises(StrategyParameterError, match="do not match the features"):
        build_default_registry().create("breakout", {"entry_lookback": 15, "exit_lookback": 5})


def test_the_declared_lookbacks_are_accepted() -> None:
    resolved = build_default_registry().create(
        "breakout", {"entry_lookback": 20, "exit_lookback": 10}
    )

    assert resolved.parameters.entry_lookback == 20  # type: ignore[attr-defined]
    assert resolved.parameters.exit_lookback == 10  # type: ignore[attr-defined]


# --- Entry ------------------------------------------------------------------------------------


def test_a_high_above_the_entry_level_while_flat_enters_long() -> None:
    signals = _strategy().generate(
        _context(
            current_high="101", current_low="99", entry_level="100", position=PositionState.FLAT
        )
    )

    assert len(signals) == 1
    assert signals[0].action is SignalAction.ENTER_LONG
    assert "broke" in signals[0].reason


def test_a_high_not_above_the_entry_level_while_flat_says_nothing() -> None:
    assert (
        _strategy().generate(
            _context(
                current_high="99", current_low="98", entry_level="100", position=PositionState.FLAT
            )
        )
        == ()
    )


def test_a_high_exactly_at_the_entry_level_does_not_trigger() -> None:
    assert (
        _strategy().generate(
            _context(
                current_high="100", current_low="98", entry_level="100", position=PositionState.FLAT
            )
        )
        == ()
    )


def test_an_entry_breakout_while_already_long_says_nothing() -> None:
    assert (
        _strategy().generate(
            _context(
                current_high="101", current_low="99", entry_level="100", position=PositionState.LONG
            )
        )
        == ()
    )


# --- Exit -------------------------------------------------------------------------------------


def test_a_low_below_the_exit_level_while_long_exits() -> None:
    signals = _strategy().generate(
        _context(current_high="95", current_low="89", exit_level="90", position=PositionState.LONG)
    )

    assert len(signals) == 1
    assert signals[0].action is SignalAction.EXIT_LONG
    assert "broke" in signals[0].reason


def test_a_low_not_below_the_exit_level_while_long_says_nothing() -> None:
    assert (
        _strategy().generate(
            _context(
                current_high="95", current_low="91", exit_level="90", position=PositionState.LONG
            )
        )
        == ()
    )


def test_a_low_exactly_at_the_exit_level_does_not_trigger() -> None:
    assert (
        _strategy().generate(
            _context(
                current_high="95", current_low="90", exit_level="90", position=PositionState.LONG
            )
        )
        == ()
    )


def test_an_exit_breakdown_while_flat_says_nothing() -> None:
    assert (
        _strategy().generate(
            _context(
                current_high="95", current_low="89", exit_level="90", position=PositionState.FLAT
            )
        )
        == ()
    )


# --- Warm-up, determinism, short --------------------------------------------------------------


def test_a_window_too_short_for_the_level_says_nothing() -> None:
    context = _context(
        current_high="200",
        current_low="180",
        entry_level=None,
        exit_level=None,
        position=PositionState.FLAT,
    )

    assert _strategy().generate(context) == ()


def test_the_same_context_always_produces_the_same_signal() -> None:
    context = _context(
        current_high="101", current_low="99", entry_level="100", position=PositionState.FLAT
    )
    strategy = _strategy()

    first = strategy.generate(context)
    second = strategy.generate(context)

    assert first == second
    assert first[0].signal_id == second[0].signal_id


def test_a_signal_carries_the_feature_that_justified_it() -> None:
    signals = _strategy().generate(
        _context(
            current_high="101", current_low="99", entry_level="100", position=PositionState.FLAT
        )
    )

    assert signals[0].features == {"donchian_high_20": Decimal(100)}


def test_the_strategy_never_emits_a_short_signal() -> None:
    for position in (PositionState.FLAT, PositionState.LONG):
        for high, low in (("101", "99"), ("95", "89")):
            for signal in _strategy().generate(
                _context(
                    current_high=high,
                    current_low=low,
                    entry_level="100",
                    exit_level="90",
                    position=position,
                )
            ):
                assert signal.action is not SignalAction.ENTER_SHORT
                assert signal.action is not SignalAction.EXIT_SHORT
