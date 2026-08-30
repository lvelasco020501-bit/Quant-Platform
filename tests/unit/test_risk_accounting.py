"""What a position risked, and what it still risks.

One number was doing two jobs. `risk_amount` was restated from the size that remained after
every fill, while its contract described what the position risked when it opened — so the
denominator of an R-multiple moved every time the position was reduced, and R quietly meant
something different for every trade it was computed over.

The same confusion had a second face in the arithmetic. `projected_stop_out_cost` adds the
entry's commission, which is right when it is handed a valuation — a price from before the
fill, with no fee in it. Handed an ``avg_entry_price``, which the portfolio engine already
computes fee-inclusive, it charges that commission twice. Both callers were passing a
parameter named ``entry_price`` and getting different meanings out of it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.constants import ZERO
from quantplatform.core.enums import CommissionModel, OrderSide, StopKind
from quantplatform.core.models.stops import StopSpecification
from quantplatform.risk.sizing import (
    break_even_price,
    open_risk_amount,
    projected_stop_out_cost,
)
from tests.factories import make_execution_policy

_QUANTITY = Decimal("0.1")
_ENTRY = Decimal(50_000)
_STOP = StopSpecification(kind=StopKind.HARD, trigger_price=Decimal(49_000))

_BPS = make_execution_policy(
    slippage_bps=Decimal(10), fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(20)
)
_FLAT = make_execution_policy(
    slippage_bps=Decimal(10), fee_model=CommissionModel.FLAT, flat_amount=Decimal("7.50")
)


# --- The two costs are different questions ------------------------------------------------------


@pytest.mark.parametrize("policy", [_BPS, _FLAT], ids=["basis_points", "flat"])
def test_the_prefill_and_postfill_costs_differ_by_exactly_the_entry_commission(
    policy: object,
) -> None:
    # The double-count, stated as an equation rather than as a suspicion. A valuation carries
    # no fee, so the pre-fill figure adds one; `avg_entry_price` already carries it, so the
    # post-fill figure must not. If the two agreed, one of them would be wrong.
    prefill = projected_stop_out_cost(
        quantity=_QUANTITY,
        valuation_price=_ENTRY,
        stop=_STOP,
        side=OrderSide.BUY,
        policy=policy,  # type: ignore[arg-type]
    )
    postfill = open_risk_amount(
        quantity=_QUANTITY,
        avg_entry_price=_ENTRY,
        stop=_STOP,
        policy=policy,  # type: ignore[arg-type]
    )

    assert prefill is not None
    assert postfill is not None
    entry_fee = policy.fee.fee_for(_QUANTITY * _ENTRY, is_first_fill=True)  # type: ignore[attr-defined]
    assert prefill - postfill == entry_fee


def test_the_postfill_cost_charges_no_entry_commission_at_all() -> None:
    # Read directly rather than by difference: what the account gets back for selling at the
    # stop, subtracted from what it paid. Nothing else belongs in it.
    postfill = open_risk_amount(quantity=_QUANTITY, avg_entry_price=_ENTRY, stop=_STOP, policy=_BPS)

    exit_price = _BPS.slippage.adjust(Decimal(49_000), OrderSide.SELL)
    proceeds = _QUANTITY * exit_price
    expected = _QUANTITY * _ENTRY - (proceeds - _BPS.fee.fee_for(proceeds, is_first_fill=True))
    assert postfill == expected


def test_a_costless_policy_makes_the_two_costs_agree() -> None:
    # The control. With no commission there is no commission to count twice, so the two
    # figures coincide — which is why the defect survived every test written before this one.
    free = make_execution_policy()
    prefill = projected_stop_out_cost(
        quantity=_QUANTITY,
        valuation_price=_ENTRY,
        stop=_STOP,
        side=OrderSide.BUY,
        policy=free,
    )
    postfill = open_risk_amount(quantity=_QUANTITY, avg_entry_price=_ENTRY, stop=_STOP, policy=free)

    assert prefill == postfill


def test_a_stop_with_no_level_describes_no_cost_either_way() -> None:
    trailing = StopSpecification(kind=StopKind.TRAILING, distance_bps=Decimal(200))

    assert (
        open_risk_amount(quantity=_QUANTITY, avg_entry_price=_ENTRY, stop=trailing, policy=_BPS)
        is None
    )


# --- The two costs and the break-even level are one arithmetic ----------------------------------


@pytest.mark.parametrize("policy", [_BPS, _FLAT], ids=["basis_points", "flat"])
def test_break_even_is_exactly_where_the_open_risk_reaches_zero(policy: object) -> None:
    # The relationship that makes these one function rather than three that must agree: the
    # break-even level is the price at which what the position still risks is nothing.
    level = break_even_price(
        quantity=_QUANTITY,
        entry_price=_ENTRY,
        side=OrderSide.BUY,
        policy=policy,  # type: ignore[arg-type]
    )

    at_break_even = open_risk_amount(
        quantity=_QUANTITY,
        avg_entry_price=_ENTRY,
        stop=StopSpecification(kind=StopKind.BREAK_EVEN, trigger_price=level),
        policy=policy,  # type: ignore[arg-type]
    )
    assert at_break_even is not None
    # One-sided, and deliberately so: an exact root is not representable, so the level is
    # rounded up and what is guaranteed is that nothing is still at risk there.
    assert at_break_even <= ZERO
    assert abs(at_break_even) < Decimal("1E-20")


def test_a_stop_above_break_even_describes_a_negative_risk() -> None:
    # Not a defect: a stop that far ahead locks in a gain, so "what it still risks" is a
    # profit already secured. The figure says so rather than clamping to zero and claiming
    # the position has nothing left to lose.
    level = break_even_price(
        quantity=_QUANTITY, entry_price=_ENTRY, side=OrderSide.BUY, policy=_BPS
    )
    ahead = StopSpecification(kind=StopKind.TRAILING, trigger_price=level + Decimal(1_000))

    risk = open_risk_amount(quantity=_QUANTITY, avg_entry_price=_ENTRY, stop=ahead, policy=_BPS)

    assert risk is not None
    assert risk < ZERO
