"""Sizing a position from the capital it risks rather than a fixed slice of the account.

Week 5 committed roughly 95% of the account to one entry because `entry_fraction = 0.95`
says to, and nothing in the platform could express the other question: given where this
position stops losing money, how large may it be before a stop-out costs more than the
budget allows? These tests pin that arithmetic.

**Nothing calls either sizer yet.** They are constructed and exercised here in isolation;
`StandardRiskEngine` still sizes exactly as it did, which is what makes V1 equivalence
structural rather than asserted. Wiring belongs with enforcement, in the milestone that can
prove a stop is actually honoured.

The cost treatment is the part worth reading closely. A stop-out does not cost
`quantity * stop_distance`; it costs that plus the fee to enter, the fee to exit, and
whatever slippage the exit suffers. Week 5's fees were 26% of its realised loss, so a
budget that ignored them would be overshot by roughly a quarter — silently, which is the
exact failure mode these contracts exist to make impossible.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import CommissionModel, OrderSide, SlippageModel, StopKind
from quantplatform.core.errors import RiskSizingError
from quantplatform.core.models.execution_policy import ExecutionPolicy, FeePolicy, SlippagePolicy
from quantplatform.core.models.risk import RiskBudget
from quantplatform.core.models.stops import StopSpecification
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.sizing import (
    FixedFractionSizer,
    RiskBasedSizer,
    SizingRequest,
    select_sizer,
)
from tests.factories import make_symbol_rules

_ENTRY = Decimal("100000")
_STOP = Decimal("98000")
"""A 2 000-wide stop on a 100 000 entry: 2% away, chosen so hand-arithmetic stays legible."""


def _budget(**overrides: object) -> RiskBudget:
    defaults: dict[str, object] = {
        "risk_per_trade_pct": Decimal("0.01"),
        "max_position_exposure_pct": Decimal("1"),
        "min_stop_distance_bps": Decimal(1),
        "max_stop_distance_bps": Decimal(10_000),
    }
    return RiskBudget(**{**defaults, **overrides})  # type: ignore[arg-type]


def _request(**overrides: object) -> SizingRequest:
    defaults: dict[str, object] = {
        "equity": Decimal("100000"),
        "available_quote": Decimal("100000"),
        "entry_price": _ENTRY,
        "stop": StopSpecification(kind=StopKind.HARD, trigger_price=_STOP),
        "side": OrderSide.BUY,
        "rules": make_symbol_rules(),
        "budget": _budget(),
        "policy": ExecutionPolicy(),
    }
    return SizingRequest(**{**defaults, **overrides})  # type: ignore[arg-type]


def _costless() -> ExecutionPolicy:
    return ExecutionPolicy(fee=FeePolicy(), slippage=SlippagePolicy())


# --- The arithmetic ---------------------------------------------------------------------------


def test_size_is_the_risk_budget_divided_by_the_stop_distance() -> None:
    # 100 000 equity, 1% risked = 1 000 at stake. A 2 000-wide stop admits 0.5 units, because
    # 0.5 * 2 000 = exactly the 1 000 the budget allows to be lost.
    outcome = RiskBasedSizer().size(_request(policy=_costless()))

    assert outcome.quantity == Decimal("0.5")


def test_the_money_at_risk_matches_the_configured_budget() -> None:
    # The property that matters more than the quantity: whatever size comes out, being
    # stopped out costs what the budget said it would.
    outcome = RiskBasedSizer().size(_request(policy=_costless()))

    assert outcome.risk_amount == Decimal("1000")


def test_a_wider_stop_buys_a_smaller_position() -> None:
    near = RiskBasedSizer().size(_request(policy=_costless()))
    far = RiskBasedSizer().size(
        _request(
            policy=_costless(),
            stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("96000")),
        )
    )

    assert far.quantity < near.quantity
    # Twice the distance, half the size — and the same money at risk either way.
    assert far.quantity == near.quantity / 2
    assert far.risk_amount == near.risk_amount


def test_a_tighter_stop_buys_a_larger_position() -> None:
    outcome = RiskBasedSizer().size(
        _request(
            policy=_costless(),
            stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("99000")),
        )
    )

    assert outcome.quantity == Decimal("1")


# --- Execution costs are part of the risk -----------------------------------------------------


def test_fees_shrink_the_position_because_a_stop_out_also_pays_them() -> None:
    # A stop-out costs the price move *plus* both commissions. Ignoring them would overshoot
    # the budget by exactly what the venue charges — week 5 charged 26% of its realised loss.
    priced = ExecutionPolicy(
        fee=FeePolicy(model=CommissionModel.BASIS_POINTS, basis_points=Decimal(10))
    )

    with_fees = RiskBasedSizer().size(_request(policy=priced))
    without = RiskBasedSizer().size(_request(policy=_costless()))

    assert with_fees.quantity < without.quantity
    assert with_fees.risk_amount <= Decimal("1000")


def test_slippage_on_the_exit_shrinks_the_position_too() -> None:
    slipping = ExecutionPolicy(
        slippage=SlippagePolicy(model=SlippageModel.FIXED_BPS, basis_points=Decimal(20))
    )

    with_slippage = RiskBasedSizer().size(_request(policy=slipping))
    without = RiskBasedSizer().size(_request(policy=_costless()))

    assert with_slippage.quantity < without.quantity


def test_a_flat_commission_is_subtracted_rather_than_scaled() -> None:
    # A flat fee does not grow with size, so it comes off the budget once per leg instead of
    # widening the per-unit loss. Getting this wrong would size every position as though the
    # fee scaled, which for a large position is wildly conservative and for a small one is
    # wrong in the dangerous direction.
    flat = ExecutionPolicy(fee=FeePolicy(model=CommissionModel.FLAT, flat_amount=Decimal(10)))

    outcome = RiskBasedSizer().size(_request(policy=flat))

    # (1 000 budget - 2 * 10 flat) / 2 000 distance = 0.49
    assert outcome.quantity == Decimal("0.49")


def test_a_budget_smaller_than_the_fixed_costs_admits_no_position() -> None:
    flat = ExecutionPolicy(fee=FeePolicy(model=CommissionModel.FLAT, flat_amount=Decimal(5000)))

    outcome = RiskBasedSizer().size(_request(policy=flat))

    assert outcome.quantity == Decimal(0)
    assert "cost" in outcome.reason


# --- Refusals: configuration errors ------------------------------------------------------------


def test_a_stop_at_the_entry_price_is_refused() -> None:
    # Not a small position — an impossible one. Dividing by a zero stop distance is the
    # arithmetic form of "this trade cannot lose", which is never true.
    with pytest.raises(RiskSizingError, match="zero"):
        RiskBasedSizer().size(
            _request(stop=StopSpecification(kind=StopKind.HARD, trigger_price=_ENTRY))
        )


def test_a_stop_above_the_entry_on_a_long_is_refused() -> None:
    # A long protected by a stop above its entry is not protected; it is a position that
    # profits by being stopped out, which means the level was configured backwards.
    with pytest.raises(RiskSizingError, match="above"):
        RiskBasedSizer().size(
            _request(stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("101000")))
        )


def test_a_stop_without_an_absolute_level_is_refused_for_now() -> None:
    # A distance-based stop is meaningful only once a fill price exists to measure from.
    # Refusing is honest; guessing the level would size against a number nobody set.
    with pytest.raises(RiskSizingError, match="trigger price"):
        RiskBasedSizer().size(
            _request(stop=StopSpecification(kind=StopKind.TRAILING, distance_bps=Decimal(200)))
        )


def test_a_stop_closer_than_the_budget_permits_is_refused() -> None:
    with pytest.raises(RiskSizingError, match="min_stop_distance_bps"):
        RiskBasedSizer().size(
            _request(
                budget=_budget(min_stop_distance_bps=Decimal(500)),
                stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("99900")),
            )
        )


def test_a_stop_wider_than_the_budget_permits_is_refused() -> None:
    with pytest.raises(RiskSizingError, match="max_stop_distance_bps"):
        RiskBasedSizer().size(
            _request(
                budget=_budget(max_stop_distance_bps=Decimal(100)),
                stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("90000")),
            )
        )


# --- Caps are not this sizer's job ---------------------------------------------------------------
#
# An earlier draft of RiskBasedSizer applied exposure and balance caps itself. Integrating it
# exposed that as two owners of one rule: StandardRiskEngine._constrain has applied those same
# caps to every order the platform has ever approved. Idempotent today, and a source of drift
# the first time either changed. The sizer now answers exactly one question, and the tests
# below pin that boundary rather than the behaviour that was removed.


def test_the_sizer_answers_only_how_much_risk_not_how_much_is_affordable() -> None:
    # An account with almost no free quote still receives the full risk-implied size. That is
    # correct: whether it can be paid for is the engine's question, asked with the same
    # balance cap it applies to every other order.
    poor = RiskBasedSizer().size(_request(policy=_costless(), available_quote=Decimal("1")))
    rich = RiskBasedSizer().size(_request(policy=_costless()))

    assert poor.quantity == rich.quantity
    assert poor.capped_by is None


def test_the_sizer_does_not_apply_the_exposure_ceiling_either() -> None:
    unconstrained = RiskBasedSizer().size(_request(policy=_costless()))
    with_ceiling = RiskBasedSizer().size(
        _request(policy=_costless(), budget=_budget(max_position_exposure_pct=Decimal("0.01")))
    )

    assert with_ceiling.quantity == unconstrained.quantity
    assert with_ceiling.capped_by is None


def test_the_reported_risk_matches_the_size_the_sizer_returned() -> None:
    # Whatever quantity comes out, risk_amount describes that quantity and not the budget.
    # The engine may shrink the position afterwards; recomputing the risk it then carries is
    # a later milestone's job, and this one must not pretend to have done it.
    outcome = RiskBasedSizer().size(_request(policy=_costless()))

    assert outcome.risk_amount is not None
    assert outcome.risk_amount == outcome.quantity * (_ENTRY - _STOP)


def test_a_size_below_the_venue_minimum_is_no_size_at_all() -> None:
    outcome = RiskBasedSizer().size(_request(policy=_costless(), equity=Decimal("0.0000001")))

    assert outcome.quantity == Decimal(0)
    assert outcome.risk_amount is None


# --- V1 equivalence ----------------------------------------------------------------------------


def test_the_fixed_fraction_sizer_reproduces_v1_sizing_exactly() -> None:
    # The golden test. build_intent sizes a long as equity * entry_fraction, expressed as a
    # notional; this must be that number and nothing else.
    outcome = FixedFractionSizer(entry_fraction=Decimal("0.95")).size(_request())

    assert outcome.notional == Decimal("100000") * Decimal("0.95")
    assert outcome.risk_amount is None


def test_the_fixed_fraction_sizer_reports_no_risk_amount() -> None:
    # It cannot: sizing by a slice of equity says nothing about what a stop-out would cost.
    # That absence is the whole reason the risk-based sizer exists.
    outcome = FixedFractionSizer(entry_fraction=Decimal("0.95")).size(_request())

    assert outcome.risk_amount is None


def _v2_config() -> RiskConfiguration:
    """A V2 configuration: a budget requires a stop distance to construct at all."""
    return RiskConfiguration(risk_budget=_budget(), initial_stop_distance_bps=Decimal(200))


# --- Choosing between them ---------------------------------------------------------------------


def test_v1_is_selected_when_no_risk_budget_is_configured() -> None:
    sizer = select_sizer(RiskConfiguration(), has_stop=True, entry_fraction=Decimal("0.95"))

    assert isinstance(sizer, FixedFractionSizer)


def test_v1_is_selected_when_a_budget_exists_but_the_intent_carries_no_stop() -> None:
    # Explicit, not a silent fallback: a risk budget cannot size anything without knowing
    # where the loss stops, and inventing a stop to satisfy the configuration would be the
    # fabricated-data failure this milestone is written against.
    sizer = select_sizer(_v2_config(), has_stop=False, entry_fraction=Decimal("0.95"))

    assert isinstance(sizer, FixedFractionSizer)


def test_risk_based_sizing_requires_both_a_budget_and_a_stop() -> None:
    sizer = select_sizer(_v2_config(), has_stop=True, entry_fraction=Decimal("0.95"))

    assert isinstance(sizer, RiskBasedSizer)


def test_the_two_sizers_are_never_both_selected() -> None:
    # One decision, one sizer. The failure this forecloses is entry_fraction and
    # risk_per_trade_pct both applying to the same order and neither being obviously wrong.
    for has_stop in (True, False):
        for budget in (None, _budget()):
            sizer = select_sizer(
                _v2_config() if budget is not None else RiskConfiguration(),
                has_stop=has_stop,
                entry_fraction=Decimal("0.95"),
            )
            assert isinstance(sizer, FixedFractionSizer | RiskBasedSizer)
