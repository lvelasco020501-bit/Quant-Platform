"""M31's pre-declaration is a promise, and these tests are what make it one.

The promise this milestone lives or dies on is "do not change the signals". A comment saying so
is worth nothing; ``test_the_frozen_rules_are_m30_s_own_objects`` is worth something, because it
fails if anyone restates a lookback rather than importing it. The second load-bearing test is
``test_the_gate_is_exactly_the_seven_conditions_that_were_declared``: M30's gate was wider, and
quietly carrying an extra criterion into M31 would reject a variant on grounds nobody declared,
which is the same breach as relaxing one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import MAX_SINGLE_YEAR_SHARE, MIN_CALMAR
from quantplatform.research.m30 import MAX_SINGLE_ASSET_SHARE, RULES_M30
from quantplatform.research.m31 import (
    COST_STRESS_MULTIPLIERS,
    DRAWDOWN_POWERS,
    FIXED_LEVELS,
    FROZEN,
    FROZEN_KEYS,
    ONE_WAY_COST_BASIS_POINTS,
    POLICIES,
    SHORT_VOLATILITY_WINDOW,
    VOLATILITY_WINDOW,
    Controlled,
    ControlRobustness,
    ExposureMechanism,
    policy_for,
    registry_is_untouched,
    survives,
    variants,
)
from quantplatform.research.sprint import CANDIDATES

PASSING: dict[str, object] = {
    "max_drawdown": Decimal("0.28"),
    "calmar_ratio": Decimal("0.90"),
    "single_year_share": Decimal("0.42"),
    "top_asset_share": Decimal("0.48"),
    "annual_at_double_cost": Decimal("0.18"),
    "annual_at_triple_cost": Decimal("0.11"),
    "out_of_sample_return": Decimal("0.35"),
}
"""A variant clearing every declared gate, used as the baseline each test breaks one field of."""


def _survives(**overrides: object) -> tuple[bool, tuple[ControlRobustness, ...]]:
    return survives(Controlled(**{**PASSING, **overrides}))  # type: ignore[arg-type]


# --- The signals are frozen ----------------------------------------------------------------------


def test_the_frozen_rules_are_m30_s_own_objects() -> None:
    # Identity, not equality. If anyone ever restates CS2's threshold or RF1's filter in M31
    # instead of importing it, there would be two copies able to drift apart and this fails.
    for rule in FROZEN:
        assert rule is next(other for other in RULES_M30 if other.key == rule.key)


def test_only_cs2_and_rf1_are_carried_forward() -> None:
    assert FROZEN_KEYS == ("CS2", "RF1")
    assert [rule.key for rule in FROZEN] == ["CS2", "RF1"]


def test_the_frozen_signals_still_carry_the_parameters_m30_screened() -> None:
    declared = {c.strategy_id: dict(c.params) for c in CANDIDATES}
    cs2 = next(rule for rule in FROZEN if rule.key == "CS2")
    rf1 = next(rule for rule in FROZEN if rule.key == "RF1")

    assert cs2.lookback == int(declared["momentum_roc"]["lookback"])
    assert cs2.entry_threshold == Decimal(declared["vol_momentum"]["threshold"])
    assert cs2.volatility_normalised is True
    assert rf1.regime_filter == int(declared["breakout_trend"]["trend_period"])
    assert rf1.lookback == int(declared["momentum_roc"]["lookback"])
    assert rf1.entry_threshold is None


def test_the_144_bar_lookback_m30_noticed_is_not_a_parameter_here() -> None:
    # M30 saw RF1 improve at 144 bars. That is a future hypothesis, and adjusting a parameter
    # because a result pointed at it is exactly what a pre-declaration exists to prevent.
    assert all(rule.lookback != 144 for rule in FROZEN)
    assert 144 not in (VOLATILITY_WINDOW, SHORT_VOLATILITY_WINDOW)
    assert 144 not in DRAWDOWN_POWERS


def test_m31_adds_no_strategy_and_alters_none() -> None:
    assert registry_is_untouched()


# --- The mechanisms ------------------------------------------------------------------------------


def test_the_declared_fixed_levels_are_the_three_that_were_asked_for() -> None:
    assert (Decimal("0.25"), Decimal("0.50"), Decimal("0.75")) == FIXED_LEVELS


def test_no_mechanism_can_reach_above_full_investment() -> None:
    # No leverage, by instruction. Every declared policy caps at the whole account.
    for _, _, policy in POLICIES:
        assert policy.fixed is None or policy.fixed <= Decimal(1)


def test_both_volatility_windows_are_ones_the_project_already_declared() -> None:
    declared = {c.strategy_id: dict(c.params) for c in CANDIDATES}

    assert int(declared["vol_momentum"]["vol_window"]) == VOLATILITY_WINDOW
    assert int(declared["vol_filtered_momentum"]["short_vol"]) == SHORT_VOLATILITY_WINDOW


def test_the_drawdown_response_contains_no_level_to_tune() -> None:
    # The mechanism is (1 - drawdown) ** power. There is no threshold in it, so there is
    # nothing for a return to have chosen.
    assert DRAWDOWN_POWERS == (1, 2)
    for power in DRAWDOWN_POWERS:
        policy = policy_for(f"drawdown ^{power}")
        assert policy.drawdown_power == power
        assert policy.fixed is None
        assert policy.volatility_window is None


def test_every_mechanism_is_represented_and_each_policy_sets_one() -> None:
    mechanisms = {mechanism for _, mechanism, _ in POLICIES}
    assert mechanisms == set(ExposureMechanism)
    for label, mechanism, policy in POLICIES:
        set_count = sum(
            value is not None
            for value in (policy.fixed, policy.volatility_window, policy.drawdown_power)
        )
        assert set_count == (0 if mechanism is ExposureMechanism.NONE else 1), label


def test_each_frozen_signal_gets_an_uncontrolled_reference() -> None:
    # Without it there is nothing to say how much CAGR a control cost or how much drawdown it
    # removed, which are two of the milestone's deliverables.
    references = [v for v in variants() if v.mechanism is ExposureMechanism.NONE]
    assert {v.signal for v in references} == {"CS2", "RF1"}


def test_the_matrix_is_both_signals_against_every_policy() -> None:
    cells = variants()
    assert len(cells) == len(FROZEN) * len(POLICIES) == 16
    assert len({v.key for v in cells}) == len(cells)


def test_mechanism_d_is_the_rf1_half_of_the_matrix_not_a_ninth_policy() -> None:
    # "Keep RF1 exactly, cash when the regime is unfavourable, exposure control when it is"
    # is what RF1 plus any of these policies already does: the filter lives in RF1's own
    # frozen spec. A separate D would count the same experiment twice.
    rf1 = next(rule for rule in FROZEN if rule.key == "RF1")
    assert rf1.regime_filter is not None
    rf1_cells = [v for v in variants() if v.signal == "RF1"]
    assert len(rf1_cells) == len(POLICIES)


def test_costs_are_the_same_fifteen_basis_points_m30_charged() -> None:
    assert Decimal(15) == ONE_WAY_COST_BASIS_POINTS
    assert COST_STRESS_MULTIPLIERS == (2, 3)


# --- The gate ------------------------------------------------------------------------------------


def test_a_variant_clearing_every_gate_survives() -> None:
    assert _survives() == (True, ())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("max_drawdown", Decimal("0.36"), ControlRobustness.DRAWDOWN),
        ("calmar_ratio", Decimal("0.49"), ControlRobustness.LOW_CALMAR),
        ("calmar_ratio", None, ControlRobustness.LOW_CALMAR),
        ("single_year_share", Decimal("0.51"), ControlRobustness.SINGLE_YEAR),
        ("single_year_share", None, ControlRobustness.SINGLE_YEAR),
        ("top_asset_share", Decimal("0.61"), ControlRobustness.ASSET_CONCENTRATION),
        ("top_asset_share", None, ControlRobustness.ASSET_CONCENTRATION),
        ("annual_at_double_cost", Decimal(0), ControlRobustness.COST_FRAGILE),
        ("annual_at_triple_cost", Decimal("-0.01"), ControlRobustness.COST_FRAGILE),
        ("out_of_sample_return", Decimal("-0.01"), ControlRobustness.OUT_OF_SAMPLE_NEGATIVE),
        ("out_of_sample_return", None, ControlRobustness.OUT_OF_SAMPLE_NEGATIVE),
    ],
)
def test_each_declared_condition_can_fail_on_its_own(
    field: str, value: object, expected: ControlRobustness
) -> None:
    passed, reasons = _survives(**{field: value})

    assert passed is False
    assert expected in reasons


def test_the_gate_is_exactly_the_seven_conditions_that_were_declared() -> None:
    # M30's gate was wider: it also read sample size, cross-asset breadth and walk-forward
    # stability. Those are measured and reported in M31 but must not gate, because adding a
    # rejection criterion after the fact is the same breach as removing one.
    assert set(ControlRobustness) == {
        ControlRobustness.DRAWDOWN,
        ControlRobustness.LOW_CALMAR,
        ControlRobustness.SINGLE_YEAR,
        ControlRobustness.ASSET_CONCENTRATION,
        ControlRobustness.COST_FRAGILE,
        ControlRobustness.OUT_OF_SAMPLE_NEGATIVE,
    }
    assert set(Controlled.model_fields) == {
        "max_drawdown",
        "calmar_ratio",
        "single_year_share",
        "top_asset_share",
        "annual_at_double_cost",
        "annual_at_triple_cost",
        "out_of_sample_return",
    }


def test_the_gate_reads_no_cagr_of_its_own() -> None:
    # The objective is a drawdown under the cap without the edge collapsing. CAGR enters only
    # through Calmar, as a floor, so a variant cannot be admitted for being profitable.
    assert "annual" not in Controlled.model_fields
    assert "cagr" not in Controlled.model_fields


def test_holding_almost_nothing_is_not_a_way_to_pass() -> None:
    # The obvious way to satisfy "drawdown below 35%" is to stop investing. A tiny drawdown
    # bought with a tiny return fails the Calmar floor, which is why that floor is one of the
    # seven and not a ranking.
    passed, reasons = _survives(max_drawdown=Decimal("0.02"), calmar_ratio=Decimal("0.10"))

    assert passed is False
    assert ControlRobustness.LOW_CALMAR in reasons


def test_surviving_double_costs_is_not_enough_on_its_own() -> None:
    # The user declared both x2 and x3. Passing one and failing the other is a failure.
    passed, reasons = _survives(annual_at_triple_cost=Decimal("-0.02"))

    assert passed is False
    assert ControlRobustness.COST_FRAGILE in reasons


def test_the_gate_names_every_reason_not_just_the_first() -> None:
    passed, reasons = _survives(
        max_drawdown=Decimal("0.80"),
        calmar_ratio=Decimal("0.20"),
        single_year_share=Decimal("2.40"),
    )

    assert passed is False
    assert {
        ControlRobustness.DRAWDOWN,
        ControlRobustness.LOW_CALMAR,
        ControlRobustness.SINGLE_YEAR,
    } <= set(reasons)


# --- Every threshold is the one its own milestone set --------------------------------------------


def test_the_drawdown_cap_is_the_thirty_five_percent_that_was_pre_declared() -> None:
    # The number M30's six rules and both its benchmarks failed against. Unchanged.
    assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN


def test_every_other_threshold_is_inherited_unchanged() -> None:
    assert Decimal("0.50") == MIN_CALMAR
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
    assert Decimal("0.60") == MAX_SINGLE_ASSET_SHARE
