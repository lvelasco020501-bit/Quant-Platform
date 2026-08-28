"""One protective level, moved forward and never back.

A hard stop, a trailing stop and a break-even stop are not three exits competing for the
same position: they are three ways of setting the *same* level. A position carries one
trigger price, and what M8a adds is the ability for that one number to move — upward only,
and never in time to judge the bar that produced it.

The timing rule is the whole of the honesty here. A trailing level computed from a bar's
own high was not in the market during the part of that bar which preceded the high, so it
governs from the next bar onward. Applying it to the bar that produced it would invent
protection that never existed and report a stop-out at a price the account could not have
obtained — the same fiction that next-bar execution exists to prevent, one level up.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.constants import ZERO
from quantplatform.core.enums import CommissionModel, OrderSide, StopKind
from quantplatform.core.errors import RiskSizingError
from quantplatform.core.models.execution_policy import ExecutionPolicy
from quantplatform.core.models.risk import PositionRiskState
from quantplatform.core.models.stops import StopSpecification
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.sizing import break_even_price
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_bar,
    make_execution_policy,
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
        "opened_at": ANCHOR,
    }
    return PositionRiskState(**{**defaults, **overrides})  # type: ignore[arg-type]


def _advance(
    *,
    high: Decimal,
    state: PositionRiskState | None = None,
    triggered: frozenset[str] = frozenset(),
    trailing: bool = False,
    break_even_activation_bps: Decimal | None = None,
    policy: ExecutionPolicy | None = None,
) -> PositionRiskState | None:
    """Run one bar's advance and return what the position is now protected by."""
    engine = make_risk_engine(
        trailing_activation_bps=Decimal(100) if trailing else None,
        trailing_distance_bps=Decimal(200) if trailing else None,
        break_even_activation_bps=break_even_activation_bps,
        execution_policy=policy if policy is not None else make_execution_policy(),
    )
    resolved = state if state is not None else _state()
    bar = make_bar(
        index=0,
        open_price=high - Decimal(100),
        high=high,
        low=high - Decimal(200),
        close=high,
    )
    advanced = engine.advance_position_risk(
        positions=(make_position(quantity=Decimal("0.1")),),
        position_risk={SYMBOL: resolved},
        bar=bar,
        triggered=triggered,
    )
    return advanced.get(SYMBOL)


# --- The anchor ---------------------------------------------------------------------------------


def test_a_bar_that_produced_no_fill_still_moves_the_anchor() -> None:
    # D2. Until now the risk state was rebuilt only for symbols that had a fill, so a
    # position held quietly through a rally recorded none of it. A trailing stop reads
    # exactly that record, and one updated only on fills would trail nothing.
    advanced = _advance(high=Decimal(52_000), trailing=True)

    assert advanced is not None
    assert advanced.highest_price_seen == Decimal(52_000)


def test_the_anchor_never_retreats_on_a_lower_high() -> None:
    seen = _state(highest_price_seen=Decimal(53_000))

    advanced = _advance(high=Decimal(51_000), state=seen, trailing=True)

    assert advanced is not None
    assert advanced.highest_price_seen == Decimal(53_000)


def test_an_unset_anchor_starts_from_the_entry_rather_than_from_nothing() -> None:
    # A position that has never traded above its entry is not a position with no favourable
    # extreme; its extreme is where it opened. Starting from None would make the first
    # trailing computation depend on whichever bar happened to arrive first.
    advanced = _advance(high=Decimal(49_500), trailing=True)

    assert advanced is not None
    assert advanced.highest_price_seen == _ENTRY


# --- Trailing -----------------------------------------------------------------------------------


def test_a_trailing_stop_does_not_arm_below_its_activation() -> None:
    # 100 bps of activation on a 50 000 entry arms at 50 500.
    advanced = _advance(high=Decimal(50_400), trailing=True)

    assert advanced is not None
    assert advanced.stop.trigger_price == _HARD_STOP
    assert advanced.stop.kind is StopKind.HARD


def test_a_trailing_stop_arms_at_its_activation() -> None:
    advanced = _advance(high=Decimal(50_500), trailing=True)

    assert advanced is not None
    assert advanced.stop.kind is StopKind.TRAILING
    assert advanced.stop.activated_at is not None


def test_an_armed_trailing_stop_follows_the_anchor() -> None:
    # 200 bps below a 52 000 extreme.
    advanced = _advance(high=Decimal(52_000), trailing=True)

    assert advanced is not None
    assert advanced.stop.trigger_price == Decimal(52_000) * (Decimal(1) - Decimal("0.02"))


def test_a_trailing_stop_never_lowers_the_trigger_it_already_had() -> None:
    # The invariant the whole feature turns on. A stop that retreats hands back risk the
    # account had already retired, and does it silently, at the moment the market is moving
    # against the position.
    # The anchor at 53 000 puts the trailing candidate at 51 940, below a level the stop
    # already sits at. The candidate is a proposal, not an instruction.
    high_water = _state(
        stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal(52_000)),
        highest_price_seen=Decimal(53_000),
    )

    advanced = _advance(high=Decimal(51_500), state=high_water, trailing=True)

    assert advanced is not None
    assert advanced.stop.trigger_price == Decimal(52_000)
    assert advanced.stop.kind is StopKind.HARD


# --- Break-even ---------------------------------------------------------------------------------

_PRICED = make_execution_policy(
    slippage_bps=Decimal(10), fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(20)
)


def _net_at(price: Decimal, *, quantity: Decimal = Decimal("0.1")) -> Decimal:
    """What the modelled round trip nets if the position exits at ``price``."""
    exit_price = _PRICED.slippage.adjust(price, OrderSide.SELL)
    proceeds = quantity * exit_price
    return proceeds - _PRICED.fee.fee_for(proceeds, is_first_fill=True) - quantity * _ENTRY


def test_the_break_even_level_nets_exactly_zero_after_modelled_costs() -> None:
    # The point of the whole helper. Exiting at `entry_price` loses the exit's slippage and
    # commission, so a stop placed there and called break-even is a stop that reports a
    # scratch and books a loss. `avg_entry_price` already carries the entry's fee, which is
    # why nothing is added for it here.
    level = break_even_price(
        quantity=Decimal("0.1"), entry_price=_ENTRY, side=OrderSide.BUY, policy=_PRICED
    )

    assert _net_at(level) == ZERO


def test_the_break_even_level_sits_above_the_entry_when_trading_costs_anything() -> None:
    level = break_even_price(
        quantity=Decimal("0.1"), entry_price=_ENTRY, side=OrderSide.BUY, policy=_PRICED
    )

    assert level > _ENTRY
    assert _net_at(_ENTRY) < ZERO


def test_a_costless_policy_breaks_even_exactly_at_the_entry() -> None:
    # The control that keeps the previous test honest: the level is derived from the policy,
    # not padded by a constant that happens to look conservative.
    level = break_even_price(
        quantity=Decimal("0.1"),
        entry_price=_ENTRY,
        side=OrderSide.BUY,
        policy=make_execution_policy(),
    )

    assert level == _ENTRY


def test_a_policy_that_can_never_break_even_fails_loudly() -> None:
    # Total slippage leaves no price at which the round trip recovers its cost. Returning
    # something anyway would put a "break-even" stop at a level that loses everything.
    with pytest.raises(RiskSizingError, match="breaks even"):
        break_even_price(
            quantity=Decimal("0.1"),
            entry_price=_ENTRY,
            side=OrderSide.BUY,
            policy=make_execution_policy(slippage_bps=Decimal(10_000)),
        )


def test_break_even_does_not_lower_a_hard_stop_that_already_sits_higher() -> None:
    already_ahead = _state(
        stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal(60_000))
    )

    advanced = _advance(
        high=Decimal(60_500),
        state=already_ahead,
        break_even_activation_bps=Decimal(100),
        policy=_PRICED,
    )

    assert advanced is not None
    assert advanced.stop.trigger_price == Decimal(60_000)
    assert advanced.stop.kind is StopKind.HARD


def test_a_trailing_level_above_break_even_keeps_trailing() -> None:
    # Both are armed and both are candidates; the higher one wins and the kind says which,
    # so the record never claims a level it is not actually protecting at.
    advanced = _advance(
        high=Decimal(60_000),
        break_even_activation_bps=Decimal(100),
        policy=_PRICED,
        trailing=True,
    )

    assert advanced is not None
    assert advanced.stop.kind is StopKind.TRAILING
    assert advanced.stop.trigger_price == Decimal(60_000) * (Decimal(1) - Decimal("0.02"))


# --- Timing and configuration -------------------------------------------------------------------


def test_a_position_whose_stop_fired_this_bar_is_not_advanced() -> None:
    # It is on its way out; moving the level it was closed under would rewrite the record of
    # why it closed.
    advanced = _advance(high=Decimal(60_000), triggered=frozenset({SYMBOL}), trailing=True)

    assert advanced is not None
    assert advanced.stop.trigger_price == _HARD_STOP


def test_half_a_trailing_configuration_does_not_construct() -> None:
    # An activation with no distance arms a stop that has nowhere to sit; a distance with no
    # activation never arms at all. Either way the configuration reads as trailing and
    # behaves as a fixed stop.
    with pytest.raises(ValueError, match="trailing"):
        RiskConfiguration(trailing_activation_bps=Decimal(100))
