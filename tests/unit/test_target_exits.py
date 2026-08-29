"""Two more reasons to close, and exactly one order when several apply.

A protective stop asks whether the position failed. A take-profit asks whether it succeeded,
and a time stop asks whether it did neither for long enough to stop paying for the chance.
All three end the position the same way — a market sell filled at the next bar's open — so
what a hierarchy decides here is not the economics but the *reason recorded*, and that only
matters because a run whose exits are all labelled the same is a run nobody can learn from.

Emitting one action per position is the substantive part. Two reasons firing on one bar used
to mean two sells: the second would be refused for want of an unreserved balance, which is
correct by accident and diagnosed as something else entirely.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from quantplatform.core.enums import RiskActionKind, RiskCheckCode, StopKind, Timeframe
from quantplatform.core.models.risk import PositionRiskState
from quantplatform.core.models.stops import StopSpecification
from tests.factories import (
    SYMBOL,
    make_bar,
    make_position,
    make_risk_engine,
)

_ENTRY = Decimal(50_000)
_HARD_STOP = Decimal(49_000)


def _state(**overrides: object) -> PositionRiskState:
    defaults: dict[str, object] = {
        "symbol": SYMBOL,
        "stop": StopSpecification(kind=StopKind.HARD, trigger_price=_HARD_STOP),
        "quantity": Decimal("0.1"),
        "risk_amount": Decimal("100"),
        "entry_price": _ENTRY,
        # A real `opened_at` is the *close* of the bar an entry filled on, never its
        # open: the broker stamps fills at the bar close. Anchoring to bar 0's close
        # makes "bars held" the bar index plus one, which is what the limit counts.
        "opened_at": make_bar(index=0).close_time,
    }
    return PositionRiskState(**{**defaults, **overrides})  # type: ignore[arg-type]


def _evaluate(
    *,
    high: Decimal = Decimal(50_100),
    low: Decimal = Decimal(49_900),
    bars_after_entry: int = 0,
    take_profit_distance_bps: Decimal | None = None,
    max_holding_bars: int | None = None,
) -> tuple[object, ...]:
    """Judge one closed bar, `bars_after_entry` bars on from the entry."""
    engine = make_risk_engine(
        take_profit_distance_bps=take_profit_distance_bps, max_holding_bars=max_holding_bars
    )
    close = (high + low) / 2
    bar = make_bar(index=bars_after_entry, open_price=close, high=high, low=low, close=close)
    return tuple(
        engine.evaluate_open_positions(
            positions=(make_position(quantity=Decimal("0.1")),),
            position_risk={SYMBOL: _state()},
            bar=bar,
        )
    )


# --- Take-profit --------------------------------------------------------------------------------
#
# 200 bps above a 50 000 entry puts the target at 51 000.


def test_a_target_that_was_not_reached_produces_no_action() -> None:
    assert _evaluate(high=Decimal(50_999), take_profit_distance_bps=Decimal(200)) == ()


def test_a_bar_that_reaches_the_target_closes_the_position() -> None:
    actions = _evaluate(high=Decimal(51_500), take_profit_distance_bps=Decimal(200))

    assert len(actions) == 1
    assert actions[0].kind is RiskActionKind.CLOSE  # type: ignore[attr-defined]
    assert actions[0].triggered_by is RiskCheckCode.TAKE_PROFIT  # type: ignore[attr-defined]


def test_a_bar_that_touches_the_target_exactly_closes_the_position() -> None:
    # Symmetric with the stop. A target reached is a target reached; requiring a breach would
    # mean the one bar that traded exactly at the level did not count.
    actions = _evaluate(high=Decimal(51_000), take_profit_distance_bps=Decimal(200))

    assert len(actions) == 1


def test_an_unconfigured_target_never_fires_however_far_price_runs() -> None:
    assert _evaluate(high=Decimal(500_000)) == ()


# --- Time stop ----------------------------------------------------------------------------------
#
# `opened_at` is the close of the bar the entry filled on, so that bar is the first one held.


def test_a_position_inside_its_holding_limit_is_left_alone() -> None:
    assert _evaluate(bars_after_entry=1, max_holding_bars=3) == ()


def test_the_last_bar_before_the_limit_still_produces_nothing() -> None:
    assert _evaluate(bars_after_entry=1, max_holding_bars=3) == ()


def test_the_bar_that_completes_the_holding_limit_closes_the_position() -> None:
    actions = _evaluate(bars_after_entry=2, max_holding_bars=3)

    assert len(actions) == 1
    assert actions[0].triggered_by is RiskCheckCode.TIME_STOP  # type: ignore[attr-defined]


def test_a_one_bar_limit_closes_on_the_bar_the_entry_filled() -> None:
    # The boundary that reads most strangely and is nonetheless right: the fill bar is the
    # first bar held, so a one-bar limit is complete the moment that bar closes.
    actions = _evaluate(bars_after_entry=0, max_holding_bars=1)

    assert len(actions) == 1


def test_an_unconfigured_holding_limit_never_fires() -> None:
    assert _evaluate(bars_after_entry=1000) == ()


# --- One position, one order --------------------------------------------------------------------


def test_a_stop_and_a_target_on_one_bar_produce_a_single_exit_blamed_on_the_stop() -> None:
    # Without intrabar data neither order of events can be proven, so the position is closed
    # for the reason that assumes the worse of the two happened. Both produce the same order
    # at the same price, so nothing favourable is being chosen — only the record differs.
    actions = _evaluate(
        high=Decimal(52_000), low=Decimal(48_000), take_profit_distance_bps=Decimal(200)
    )

    assert len(actions) == 1
    assert actions[0].triggered_by is RiskCheckCode.PROTECTIVE_STOP  # type: ignore[attr-defined]


def test_a_target_and_a_holding_limit_on_one_bar_are_blamed_on_the_target() -> None:
    actions = _evaluate(
        high=Decimal(52_000),
        bars_after_entry=5,
        take_profit_distance_bps=Decimal(200),
        max_holding_bars=3,
    )

    assert len(actions) == 1
    assert actions[0].triggered_by is RiskCheckCode.TAKE_PROFIT  # type: ignore[attr-defined]


def test_every_reason_at_once_still_closes_the_position_only_once() -> None:
    actions = _evaluate(
        high=Decimal(52_000),
        low=Decimal(48_000),
        bars_after_entry=5,
        take_profit_distance_bps=Decimal(200),
        max_holding_bars=3,
    )

    assert len(actions) == 1
    assert actions[0].triggered_by is RiskCheckCode.PROTECTIVE_STOP  # type: ignore[attr-defined]


def test_the_holding_limit_is_measured_in_the_bar_s_own_timeframe() -> None:
    # Bars are the unit, and the bar carries which unit that is. Reading a duration in
    # seconds from configuration would make the same limit mean different things on a
    # different timeframe while the configuration looked unchanged.
    engine = make_risk_engine(max_holding_bars=3)
    daily = make_bar(
        index=0,
        timeframe=Timeframe.D1,
        close=Decimal(50_000),
    )
    opened_two_days_ago = _state(opened_at=daily.close_time - timedelta(days=2))

    actions = engine.evaluate_open_positions(
        positions=(make_position(quantity=Decimal("0.1")),),
        position_risk={SYMBOL: opened_two_days_ago},
        bar=daily,
    )

    assert len(actions) == 1
