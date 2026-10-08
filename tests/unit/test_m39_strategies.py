"""M39's three families, and the entry conditions that make them risk-native or not.

Each family is defined by *where* it enters, because that is what decides where Risk V2's 600
bps stop lands. The tests below pin those conditions directly: a pullback entry requires the
short horizon to be negative while the long one is positive, a retest entry requires a breakout
to have happened recently *and* price to have come back below it, and the control enters only
when both horizons agree. The retest family's recency test is an identity over two Donchian
windows rather than stored state, so it gets its own tests.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, PositionState, SignalAction, Timeframe
from quantplatform.core.errors import StrategyNotFoundError, StrategyParameterError
from quantplatform.core.models.signals import StrategyContext
from quantplatform.strategies.registry import build_default_registry
from quantplatform.strategies.research import build_research_registry
from tests.factories import SYMBOL, make_bar, make_bars, make_symbol_rules

PULLBACK_PARAMS: dict[str, object] = {"trend_window": 168, "pullback_window": 24}
RETEST_PARAMS: dict[str, object] = {
    "trend_window": 168,
    "breakout_lookback": 20,
    "recent_lookback": 10,
    "exit_lookback": 10,
}
DUAL_PARAMS: dict[str, object] = {"fast_window": 24, "slow_window": 168}


def context(
    features: dict[str, Decimal],
    *,
    close: Decimal = Decimal(100),
    state: PositionState = PositionState.FLAT,
) -> StrategyContext:
    """Return a context whose last bar closes at ``close``, with the given features."""
    history = make_bars(tuple(Decimal(100) for _ in range(3)), timeframe=Timeframe.D1)
    latest = make_bar(
        index=len(history),
        close=close,
        open_price=close,
        high=close,
        low=close,
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


def _build(strategy_id: str, params: dict[str, object], **overrides: object) -> object:
    return build_research_registry().create(strategy_id, {**params, **overrides})


def _actions(signals: object) -> list[SignalAction]:
    return [signal.action for signal in signals]  # type: ignore[attr-defined]


class TestPullbackEntersWeaknessInsideStrength:
    """The defining condition: long horizon up, short horizon down."""

    @staticmethod
    def _signals(trend: str, pullback: str, **kw: object) -> object:
        strategy = _build("trend_pullback", PULLBACK_PARAMS)
        return strategy.generate(  # type: ignore[attr-defined]
            context({"roc_168": Decimal(trend), "roc_24": Decimal(pullback)}, **kw)  # type: ignore[arg-type]
        )

    def test_it_enters_when_the_trend_is_up_and_the_short_horizon_is_down(self) -> None:
        assert _actions(self._signals("0.20", "-0.05")) == [SignalAction.ENTER_LONG]

    def test_it_does_not_enter_while_the_short_horizon_is_still_rising(self) -> None:
        # This is the whole point of the family: entering here would be an extended price.
        assert _actions(self._signals("0.20", "0.05")) == []

    def test_it_does_not_enter_when_the_trend_is_down(self) -> None:
        assert _actions(self._signals("-0.20", "-0.05")) == []

    def test_a_flat_short_horizon_is_not_a_pullback(self) -> None:
        assert _actions(self._signals("0.20", "0")) == []

    def test_it_exits_when_the_trend_turns_negative(self) -> None:
        assert _actions(self._signals("-0.01", "0.10", state=PositionState.LONG)) == [
            SignalAction.EXIT_LONG
        ]

    def test_it_holds_while_the_trend_is_still_up(self) -> None:
        assert _actions(self._signals("0.20", "-0.30", state=PositionState.LONG)) == []

    def test_a_pullback_window_at_or_above_the_trend_window_is_refused(self) -> None:
        with pytest.raises(StrategyParameterError):
            _build("trend_pullback", PULLBACK_PARAMS, pullback_window=168)


class TestRetestEntersAfterTheBreakout:
    """Recency is read from two windows, and the entry must be below the high."""

    @staticmethod
    def _signals(
        trend: str, breakout_high: str, recent_high: str, close: str, **kw: object
    ) -> object:
        strategy = _build("breakout_retest", RETEST_PARAMS)
        return strategy.generate(  # type: ignore[attr-defined]
            context(
                {
                    "roc_168": Decimal(trend),
                    "donchian_high_20": Decimal(breakout_high),
                    "donchian_high_10": Decimal(recent_high),
                    "donchian_low_10": Decimal(90),
                },
                close=Decimal(close),
                **kw,  # type: ignore[arg-type]
            )
        )

    def test_it_enters_when_a_recent_breakout_is_being_retested(self) -> None:
        # The 10-bar high equals the 20-bar high, so the 20-bar high was set in the last 10
        # bars; and the close sits below it, which is the retest rather than the extension.
        assert _actions(self._signals("0.20", "110", "110", "105")) == [SignalAction.ENTER_LONG]

    def test_it_does_not_enter_when_the_breakout_is_old(self) -> None:
        # A 10-bar high below the 20-bar high means the 20-bar high was set earlier than the
        # last 10 bars, so there is no recent breakout to retest.
        assert _actions(self._signals("0.20", "110", "104", "100")) == []

    def test_it_does_not_enter_at_the_extreme(self) -> None:
        # Price at or above the recent high is the extension, which is what this family avoids.
        assert _actions(self._signals("0.20", "110", "110", "110")) == []

    def test_it_does_not_enter_when_the_trend_is_down(self) -> None:
        assert _actions(self._signals("-0.20", "110", "110", "105")) == []

    def test_it_exits_when_the_close_breaks_the_exit_low(self) -> None:
        assert _actions(self._signals("0.20", "110", "110", "89", state=PositionState.LONG)) == [
            SignalAction.EXIT_LONG
        ]

    def test_it_holds_while_the_close_stays_above_the_exit_low(self) -> None:
        assert _actions(self._signals("0.20", "110", "110", "95", state=PositionState.LONG)) == []

    def test_a_recent_window_at_or_above_the_breakout_window_is_refused(self) -> None:
        # The recency test would be vacuous: the two windows would always be equal.
        with pytest.raises(StrategyParameterError):
            _build("breakout_retest", RETEST_PARAMS, recent_lookback=20)


class TestTheControlEntersStrength:
    """M39's control. It enters extended, which is what the hypothesis says will cost it."""

    @staticmethod
    def _signals(fast: str, slow: str, **kw: object) -> object:
        strategy = _build("dual_horizon_momentum", DUAL_PARAMS)
        return strategy.generate(  # type: ignore[attr-defined]
            context({"roc_24": Decimal(fast), "roc_168": Decimal(slow)}, **kw)  # type: ignore[arg-type]
        )

    def test_it_enters_when_both_horizons_are_positive(self) -> None:
        assert _actions(self._signals("0.05", "0.20")) == [SignalAction.ENTER_LONG]

    def test_it_does_not_enter_when_only_the_fast_horizon_is_positive(self) -> None:
        assert _actions(self._signals("0.05", "-0.20")) == []

    def test_it_does_not_enter_when_only_the_slow_horizon_is_positive(self) -> None:
        assert _actions(self._signals("-0.05", "0.20")) == []

    def test_it_enters_where_the_pullback_family_refuses(self) -> None:
        # The two families are opposites by construction: this is the extended price that the
        # pullback rule declines, and the reason the control is expected to fare worse.
        extended = {"roc_24": Decimal("0.05"), "roc_168": Decimal("0.20")}
        control = _build("dual_horizon_momentum", DUAL_PARAMS)
        pullback = _build("trend_pullback", PULLBACK_PARAMS)
        assert _actions(control.generate(context(extended))) == [  # type: ignore[attr-defined]
            SignalAction.ENTER_LONG
        ]
        assert _actions(pullback.generate(context(extended))) == []  # type: ignore[attr-defined]

    def test_it_exits_when_the_fast_horizon_turns(self) -> None:
        assert _actions(self._signals("-0.01", "0.20", state=PositionState.LONG)) == [
            SignalAction.EXIT_LONG
        ]

    def test_a_fast_window_at_or_above_the_slow_window_is_refused(self) -> None:
        with pytest.raises(StrategyParameterError):
            _build("dual_horizon_momentum", DUAL_PARAMS, fast_window=168)


class TestNoneOfThemReachesPaperTrading:
    """Research strategies must stay out of the default registry."""

    def test_the_default_registry_refuses_each_of_them_by_name(self) -> None:
        default = build_default_registry()
        for strategy_id in ("trend_pullback", "breakout_retest", "dual_horizon_momentum"):
            with pytest.raises(StrategyNotFoundError) as raised:
                default.create(strategy_id, {})
            # The refusal names what *is* available, which is the stronger statement: the
            # default registry still holds exactly the three shipped strategies and nothing M39
            # added. Asserting the message's list rather than only the exception means a future
            # registration would fail here instead of passing quietly.
            assert "available=['breakout', 'breakout_trend', 'ema_trend']" in str(raised.value)
