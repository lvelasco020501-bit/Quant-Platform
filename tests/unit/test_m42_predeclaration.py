"""M42's pre-declaration: the gate, and the fact that none of it is M42's own invention.

Two things carry weight here. The first is that every threshold is the object an earlier
milestone declared, asserted by identity where identity is available -- a copied number would
let M42's notion of "enough edge conserved" drift from M37's while still looking inherited. The
second is that each gate fires on its own cause and on nothing else, because a gate that fires
for the wrong reason is indistinguishable from a gate that was moved.

``test_the_declared_gate_is_close_to_self_excluding`` is the honest one. It fixes in a test what
the module docstring says in prose: at Risk V2's own return, conserving half of it and clearing
the Calmar floor together demand a drawdown far under the 35% cap. It exists so that nobody --
including a later me -- can read a NO-GO and conclude the gate must have been chosen badly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.features.indicators import IndicatorFeatures
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
)
from quantplatform.research.m30 import MAX_SINGLE_ASSET_SHARE, OOS_START
from quantplatform.research.m32 import LIQUIDITY_WINDOW
from quantplatform.research.m34 import MAX_PER_SLEEVE_SHARE
from quantplatform.research.m36 import BREADTHS
from quantplatform.research.m37 import EDGE_CONSERVATION_FOR_ALTERNATIVE
from quantplatform.research.m42 import (
    EXPOSURE_MATCH_TOLERANCE,
    MIN_CAGR_CONSERVATION,
    VOLATILITY_FEATURE,
    VOLATILITY_WINDOW,
    Arm,
    ArmResult,
    Gate,
    Unmeasurable,
    cagr_conserved,
    passes,
)

BASELINE_ANNUAL = Decimal("0.0753304676514326")
"""Risk V2's own full-universe annual return at breadth six, as M38 measured it. Used as the
baseline a candidate is scored against, never as a target."""


def result(**overrides: object) -> ArmResult:
    """Return an arm that clears every gate, so one override isolates one cause."""
    fields: dict[str, object] = {
        "arm": Arm.INVERSE_VOL,
        "breadth": 6,
        "annual": Decimal("0.15"),
        "max_drawdown": Decimal("0.20"),
        "calmar_ratio": Decimal("0.75"),
        "profit_factor": Decimal("1.40"),
        "turnover": Decimal(100),
        "fees": Decimal(500),
        "exposure": Decimal("0.60"),
        "out_of_sample_return": Decimal("0.10"),
        "annual_at_double_cost": Decimal("0.08"),
        "annual_at_triple_cost": Decimal("0.03"),
        "single_year_share": Decimal("0.40"),
        "years_positive_share": Decimal("0.70"),
        "top_asset_share": Decimal("0.30"),
        "top_sleeve_share": Decimal("0.55"),
    }
    return ArmResult(**{**fields, **overrides})  # type: ignore[arg-type]


def baseline() -> ArmResult:
    """Return the equal-weight arm the candidate is judged against."""
    return result(arm=Arm.EQUAL_WEIGHT, annual=BASELINE_ANNUAL, calmar_ratio=Decimal("0.19"))


# --- Nothing here is M42's own number -------------------------------------------------------------


def test_the_volatility_window_is_the_liquidity_window() -> None:
    # Taken rather than chosen, which is also what makes the insufficient-history case nearly
    # empty: eligible_universe already refuses a market with fewer than this many bars.
    assert VOLATILITY_WINDOW == LIQUIDITY_WINDOW


def test_the_conservation_fraction_is_m37s_object_not_a_copy_of_its_value() -> None:
    # Identity, not equality. Two Decimal("0.50") would compare equal and could still drift
    # apart the day one of them is revised.
    assert MIN_CAGR_CONSERVATION is EDGE_CONSERVATION_FOR_ALTERNATIVE


@pytest.mark.parametrize(
    ("declared", "inherited"),
    [
        (Gate.DRAWDOWN, SCREEN_MAX_DRAWDOWN),
        (Gate.LOW_CALMAR, MIN_CALMAR),
        (Gate.ASSET_CONCENTRATION, MAX_SINGLE_ASSET_SHARE),
        (Gate.SLEEVE_CONCENTRATION, MAX_PER_SLEEVE_SHARE),
        (Gate.SINGLE_YEAR_CONCENTRATION, MAX_SINGLE_YEAR_SHARE),
        (Gate.WEAK_PROFIT_FACTOR, MIN_NEIGHBOUR_PROFIT_FACTOR),
    ],
)
def test_every_performance_threshold_came_from_an_earlier_milestone(
    declared: Gate, inherited: Decimal
) -> None:
    # The import itself is the assertion: these names resolve in the milestones that declared
    # them, so this test fails at collection if M42 ever grows a private copy.
    assert isinstance(inherited, Decimal)
    assert declared in Gate


def test_the_cost_stress_and_out_of_sample_window_are_the_inherited_ones() -> None:
    assert COST_STRESS_MULTIPLIERS == (2, 3)
    assert OOS_START.year == 2024
    assert BREADTHS == (6, 12)


def test_the_volatility_definition_is_the_production_indicator() -> None:
    # rvol_<n> is the population standard deviation of n one-bar returns, which needs n + 1
    # closes. That one extra bar is why a market can be eligible for exactly one bar before its
    # volatility exists, and the declared rule has to cover it.
    assert VOLATILITY_FEATURE.startswith("rvol_")
    assert IndicatorFeatures([VOLATILITY_FEATURE]).required_history == VOLATILITY_WINDOW + 1


def test_every_reason_a_holding_can_go_unweighted_is_named() -> None:
    # Three, and no "other": an unmeasurable volatility that did not match a declared reason
    # would be a silent exclusion, which is the thing this enum exists to prevent.
    assert {reason.value for reason in Unmeasurable} == {
        "short_history",
        "gapped_window",
        "zero_volatility",
    }


# --- The gate is what it says it is ---------------------------------------------------------------


def test_an_arm_that_clears_everything_passes() -> None:
    ok, failed = passes(result(), baseline())

    assert ok
    assert failed == ()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"max_drawdown": SCREEN_MAX_DRAWDOWN + Decimal("0.0001")}, Gate.DRAWDOWN),
        ({"calmar_ratio": MIN_CALMAR - Decimal("0.0001")}, Gate.LOW_CALMAR),
        ({"calmar_ratio": None}, Gate.LOW_CALMAR),
        ({"out_of_sample_return": Decimal(0)}, Gate.OUT_OF_SAMPLE_NEGATIVE),
        ({"out_of_sample_return": None}, Gate.OUT_OF_SAMPLE_NEGATIVE),
        ({"annual_at_double_cost": Decimal("-0.01")}, Gate.COST_FRAGILE),
        ({"annual_at_triple_cost": Decimal(0)}, Gate.COST_FRAGILE),
        ({"top_asset_share": Decimal("0.61")}, Gate.ASSET_CONCENTRATION),
        ({"top_sleeve_share": Decimal("0.61")}, Gate.SLEEVE_CONCENTRATION),
        ({"single_year_share": Decimal("0.51")}, Gate.SINGLE_YEAR_CONCENTRATION),
        ({"profit_factor": Decimal("0.99")}, Gate.WEAK_PROFIT_FACTOR),
        ({"profit_factor": None}, Gate.WEAK_PROFIT_FACTOR),
        ({"annual": Decimal("0.03")}, Gate.CAGR_NOT_CONSERVED),
        ({"capital_deficit": Decimal("0.01")}, Gate.EXPOSURE_NOT_MATCHED),
    ],
)
def test_each_gate_fires_on_its_own_cause_and_on_no_other(
    overrides: dict[str, object], expected: Gate
) -> None:
    ok, failed = passes(result(**overrides), baseline())

    assert not ok
    assert failed == (expected,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_drawdown", SCREEN_MAX_DRAWDOWN),
        ("top_asset_share", MAX_SINGLE_ASSET_SHARE),
        ("top_sleeve_share", MAX_PER_SLEEVE_SHARE),
        ("single_year_share", MAX_SINGLE_YEAR_SHARE),
        ("profit_factor", MIN_NEIGHBOUR_PROFIT_FACTOR),
        ("calmar_ratio", MIN_CALMAR),
    ],
)
def test_a_value_exactly_at_a_limit_is_inside_it(field: str, value: Decimal) -> None:
    # The caps are maxima and the floors are minima, both inclusive. Stated as a test because
    # "<= 35%" and "< 35%" differ by exactly the cases a result is most likely to land on.
    ok, _ = passes(result(**{field: value}), baseline())

    assert ok


def test_rounding_alone_does_not_count_as_an_exposure_mismatch() -> None:
    ok, _ = passes(result(capital_deficit=EXPOSURE_MATCH_TOLERANCE), baseline())

    assert ok


# --- Conserving an edge that does not exist -------------------------------------------------------


@pytest.mark.parametrize("annual", [Decimal(0), Decimal("-0.05"), None])
def test_a_baseline_that_earned_nothing_cannot_be_conserved(annual: Decimal | None) -> None:
    # Half of a loss is a smaller loss, and scoring against it would make the gate easier the
    # worse the baseline did. Refused rather than scored, as M39 refused a losing signal basis.
    assert not cagr_conserved(result(annual=Decimal("0.50")), result(annual=annual))


def test_conserving_exactly_the_declared_fraction_is_enough() -> None:
    exact = BASELINE_ANNUAL * MIN_CAGR_CONSERVATION

    assert cagr_conserved(result(annual=exact), baseline())
    assert not cagr_conserved(result(annual=exact - Decimal("0.000001")), baseline())


def test_a_candidate_with_no_annual_rate_does_not_conserve_anything() -> None:
    # cagr() returns None when a run lost everything. That is not a pass by omission.
    assert not cagr_conserved(result(annual=None), baseline())


# --- The warning, fixed in a test -----------------------------------------------------------------


def test_the_declared_gate_is_close_to_self_excluding() -> None:
    # Written before any M42 result existed. Conserving half of Risk V2's 7.53% leaves 3.77%,
    # and a Calmar floor of 0.50 then requires a drawdown at or under 7.5% -- a fifth of the 35%
    # the drawdown gate alone would allow. So the gates together demand a re-weighting that
    # raises the return AND lowers the drawdown; they do not accept a trade between the two.
    floor = BASELINE_ANNUAL * MIN_CAGR_CONSERVATION
    implied_drawdown_cap = floor / MIN_CALMAR

    assert implied_drawdown_cap < SCREEN_MAX_DRAWDOWN / 4
    # And an arm that merely conserves the edge while halving the drawdown still fails.
    ok, failed = passes(
        result(annual=floor, max_drawdown=Decimal("0.13"), calmar_ratio=floor / Decimal("0.13")),
        baseline(),
    )
    assert not ok
    assert failed == (Gate.LOW_CALMAR,)
