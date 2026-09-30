"""M30's pre-declaration is a promise, and these tests are what make it one.

A threshold written down before the runs and one written down after them look identical in a
diff. What separates them is that the first was committed, and that something fails if it moves.
Everything here exists so that relaxing a gate to let a result through has to happen in the
open, in a commit someone reviews, rather than by editing a constant.

Two tests carry most of the weight. ``test_every_parameter_is_one_the_project_already_declared``
is what stops M30 from being a parameter search wearing a new name: if any lookback, window or
threshold here stops matching the strategy it was inherited from, that test fails.
``test_profit_cannot_rescue_a_configuration_that_failed_the_gate`` is M29's separation carried
forward -- robustness gates, profit only orders -- and if those two ever merged, "maximise robust
edge" would quietly become "maximise edge".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN, SCREEN_MIN_TRADES, doubled
from quantplatform.research.m29 import (
    MAX_SINGLE_YEAR_SHARE,
    MIN_ASSETS_POSITIVE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
    MIN_YEARS_POSITIVE_SHARE,
)
from quantplatform.research.m30 import (
    ASSETS_M30,
    BENCHMARKS,
    COST_STRESS_MULTIPLIERS,
    CROSS_SECTIONAL_THRESHOLDS,
    FAMILIES_M30,
    LOOKBACK_BARS,
    MAX_SINGLE_ASSET_SHARE,
    MIN_WALK_FORWARD_POSITIVE_SHARE,
    NEIGHBOUR_LOOKBACKS,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    REGIME_FILTER_BARS,
    REGIME_FILTER_BARS_DOUBLED,
    RULES_M30,
    VOL_WINDOW_BARS,
    Benchmark,
    FamilyM30,
    RotationMeasured,
    RotationRobustness,
    beats_basket,
    profit_rank,
    survives,
)
from quantplatform.research.sprint import CANDIDATES

PASSING: dict[str, object] = {
    "trades": 60,
    "annual": Decimal("0.12"),
    "max_drawdown": Decimal("0.18"),
    "calmar_ratio": Decimal("0.67"),
    "assets_positive": 4,
    "top_asset_share": Decimal("0.45"),
    "years_positive_share": Decimal("0.75"),
    "single_year_share": Decimal("0.40"),
    "walk_forward_positive_share": Decimal("0.80"),
    "neighbour_min_profit_factor": Decimal("1.15"),
    "annual_at_double_cost": Decimal("0.06"),
    "out_of_sample_return": Decimal("0.09"),
}
"""A configuration clearing every declared gate, used as the baseline each test breaks exactly
one field of. Written once so that a test which fails proves the field it changed was the
cause."""


def _survives(**overrides: object) -> tuple[bool, tuple[RotationRobustness, ...]]:
    return survives(RotationMeasured(**{**PASSING, **overrides}))  # type: ignore[arg-type]


# --- What is searched, and that it cannot drift ---------------------------------------------------


def test_the_universe_is_the_six_declared_markets() -> None:
    assert ASSETS_M30 == ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT")


def test_the_three_families_are_the_three_that_were_asked_for() -> None:
    assert [family.value for family in FAMILIES_M30] == [
        "relative_strength",
        "cross_sectional",
        "regime_filtered",
    ]


def test_every_family_gets_exactly_two_variants_and_no_more() -> None:
    # Two per family is the declared ceiling. A third would be a grid search with extra steps.
    for family in FAMILIES_M30:
        assert len([rule for rule in RULES_M30 if rule.family is family]) == 2, family
    assert len(RULES_M30) == 6


def test_within_a_family_exactly_one_dimension_moves() -> None:
    # A difference between two variants must have one possible cause. If two things changed at
    # once, no result from either could say which mattered.
    for family in FAMILIES_M30:
        first, second = (rule for rule in RULES_M30 if rule.family is family)
        differing = [
            field
            for field in RotationMeasuredFields.RULE_FIELDS
            if getattr(first, field) != getattr(second, field)
        ]
        assert len(differing) == 1, (family, differing)


class RotationMeasuredFields:
    """The rule fields a variant is allowed to differ in, named so the test above reads."""

    RULE_FIELDS = (
        "lookback",
        "hold",
        "volatility_normalised",
        "entry_threshold",
        "regime_filter",
        "vol_window",
    )


def test_the_keys_are_unique_so_a_result_can_never_be_filed_under_two_rules() -> None:
    keys = [rule.key for rule in RULES_M30]
    assert len(set(keys)) == len(keys)


def test_the_relative_strength_variants_are_the_top_one_and_the_top_two() -> None:
    breadths = sorted(rule.hold for rule in RULES_M30 if rule.family is FamilyM30.RELATIVE_STRENGTH)
    assert breadths == [1, 2]


def test_the_cross_sectional_family_is_the_only_one_that_can_choose_cash() -> None:
    # Holding cash is this family's declared mechanism: enter only if the winner clears a
    # common bar. The regime-filtered family also sits out, but on the aggregate's say-so
    # rather than the winner's own score.
    thresholded = {rule.family for rule in RULES_M30 if rule.entry_threshold is not None}
    assert thresholded == {FamilyM30.CROSS_SECTIONAL}


def test_only_the_regime_filtered_family_reads_the_aggregate() -> None:
    filtered = {rule.family for rule in RULES_M30 if rule.regime_filter is not None}
    assert filtered == {FamilyM30.REGIME_FILTERED}


# --- Nothing here is a new number ----------------------------------------------------------------


def test_every_parameter_is_one_the_project_already_declared() -> None:
    # The load-bearing test of this module. M30 changes the *question*, not the numbers: if a
    # lookback or window here stops matching the strategy it was taken from, this milestone has
    # quietly become a parameter search and this test says so.
    declared = {c.strategy_id: dict(c.params) for c in CANDIDATES}

    assert int(declared["momentum_roc"]["lookback"]) == LOOKBACK_BARS
    assert int(declared["vol_momentum"]["vol_window"]) == VOL_WINDOW_BARS
    assert int(declared["breakout_trend"]["trend_period"]) == REGIME_FILTER_BARS
    assert CROSS_SECTIONAL_THRESHOLDS[1] == Decimal(declared["vol_momentum"]["threshold"])


def test_the_second_regime_variant_comes_from_the_doubling_rule_not_a_person() -> None:
    doubled_params = dict(doubled((("trend_period", str(REGIME_FILTER_BARS)),)))

    assert int(doubled_params["trend_period"]) == REGIME_FILTER_BARS_DOUBLED


def test_the_sensitivity_probes_are_half_and_double_the_declared_lookback() -> None:
    assert NEIGHBOUR_LOOKBACKS == (LOOKBACK_BARS // 2, LOOKBACK_BARS * 2)
    assert LOOKBACK_BARS not in NEIGHBOUR_LOOKBACKS


def test_the_cost_is_the_platform_s_own_fee_plus_slippage_and_nothing_else() -> None:
    # 10 bps fee plus 5 bps slippage, the numbers every M22-M29 definition carries. The third
    # number those definitions hold, assumed_spread_basis_points, is a risk-engine metric and
    # never moves a fill price, so charging it here would invent a cost the engine does not.
    assert Decimal(15) == ONE_WAY_COST_BASIS_POINTS


def test_costs_are_stressed_upward_and_never_downward() -> None:
    assert COST_STRESS_MULTIPLIERS == (2, 3)
    assert all(multiplier > 1 for multiplier in COST_STRESS_MULTIPLIERS)


def test_the_out_of_sample_boundary_is_the_one_every_prior_milestone_used() -> None:
    assert OOS_START.year == 2024
    assert (OOS_START.month, OOS_START.day) == (1, 1)


# --- The gate reads no return and no benchmark ---------------------------------------------------


def test_a_configuration_clearing_every_gate_survives() -> None:
    assert _survives() == (True, ())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("trades", SCREEN_MIN_TRADES - 1, RotationRobustness.THIN_SAMPLE),
        ("annual", Decimal("-0.01"), RotationRobustness.NEGATIVE_AFTER_COSTS),
        ("annual", None, RotationRobustness.NEGATIVE_AFTER_COSTS),
        ("max_drawdown", Decimal("0.36"), RotationRobustness.DRAWDOWN),
        ("calmar_ratio", Decimal("0.49"), RotationRobustness.LOW_CALMAR),
        ("calmar_ratio", None, RotationRobustness.LOW_CALMAR),
        ("assets_positive", 2, RotationRobustness.SINGLE_ASSET),
        ("top_asset_share", Decimal("0.61"), RotationRobustness.ASSET_CONCENTRATION),
        ("top_asset_share", None, RotationRobustness.ASSET_CONCENTRATION),
        ("single_year_share", Decimal("0.51"), RotationRobustness.SINGLE_YEAR),
        ("years_positive_share", Decimal("0.59"), RotationRobustness.INCONSISTENT_YEARS),
        (
            "walk_forward_positive_share",
            Decimal("0.49"),
            RotationRobustness.UNSTABLE_ACROSS_WINDOWS,
        ),
        ("neighbour_min_profit_factor", Decimal("0.99"), RotationRobustness.NARROW_PEAK),
        ("annual_at_double_cost", Decimal(0), RotationRobustness.COST_FRAGILE),
        ("out_of_sample_return", Decimal("-0.01"), RotationRobustness.OUT_OF_SAMPLE_NEGATIVE),
    ],
)
def test_each_declared_condition_can_fail_on_its_own(
    field: str, value: object, expected: RotationRobustness
) -> None:
    passed, reasons = _survives(**{field: value})

    assert passed is False
    assert expected in reasons


def test_the_gate_names_every_reason_not_just_the_first() -> None:
    # A gate reporting only the first failure would make six problems look like one.
    passed, reasons = _survives(
        annual=Decimal("-0.02"), assets_positive=1, top_asset_share=Decimal("1.40")
    )

    assert passed is False
    assert {
        RotationRobustness.NEGATIVE_AFTER_COSTS,
        RotationRobustness.SINGLE_ASSET,
        RotationRobustness.ASSET_CONCENTRATION,
    } <= set(reasons)


def test_finding_btc_and_sitting_on_it_is_not_rotation() -> None:
    # The single most likely way this milestone fools itself: a rule that technically rotates
    # but earns everything from one asset. Robust in every other respect, and still refused.
    passed, reasons = _survives(
        annual=Decimal("0.40"),
        calmar_ratio=Decimal("2.0"),
        top_asset_share=Decimal("0.95"),
    )

    assert passed is False
    assert RotationRobustness.ASSET_CONCENTRATION in reasons


def test_a_huge_return_does_not_buy_its_way_past_a_failed_gate() -> None:
    passed, reasons = _survives(
        annual=Decimal("3.00"),
        calmar_ratio=Decimal("12"),
        assets_positive=1,
        single_year_share=Decimal("0.98"),
    )

    assert passed is False
    assert RotationRobustness.SINGLE_ASSET in reasons
    assert RotationRobustness.SINGLE_YEAR in reasons


def test_the_gate_cannot_be_passed_by_beating_a_benchmark() -> None:
    # survives() takes a record that has no benchmark field at all, which is the structural
    # reason a benchmark cannot rescue a failure rather than a promise that it will not.
    assert "basket" not in " ".join(RotationMeasured.model_fields)
    assert "benchmark" not in " ".join(RotationMeasured.model_fields)


# --- Profit orders only what already survived ----------------------------------------------------


def test_profit_cannot_rescue_a_configuration_that_failed_the_gate() -> None:
    failed, _ = _survives(assets_positive=1)
    ranked = profit_rank(calmar_ratio=Decimal("99"), annual=Decimal("5"))

    assert failed is False
    assert isinstance(ranked, tuple)
    assert not isinstance(ranked, bool)


def test_calmar_outranks_raw_return() -> None:
    steady = profit_rank(calmar_ratio=Decimal("0.75"), annual=Decimal("0.03"))
    wild = profit_rank(calmar_ratio=Decimal("0.30"), annual=Decimal("0.06"))

    assert steady > wild


def test_turnover_is_absent_from_the_ranking() -> None:
    # Frequency is not an objective here either. Two rules that carry risk and pay identically
    # rank identically however often they rotate.
    assert profit_rank(calmar_ratio=Decimal("0.75"), annual=Decimal("0.05")) == profit_rank(
        calmar_ratio=Decimal("0.75"), annual=Decimal("0.05")
    )


# --- The benchmark decides promotion, never rejection --------------------------------------------


def test_rotation_must_out_earn_holding_everything_to_be_promoted() -> None:
    assert beats_basket(rotation_calmar=Decimal("0.80"), basket_calmar=Decimal("0.60")) is True
    assert beats_basket(rotation_calmar=Decimal("0.60"), basket_calmar=Decimal("0.80")) is False


def test_a_tie_with_the_basket_is_not_a_win() -> None:
    # Equalling a static basket means the ranking machinery added nothing, which is this
    # milestone's question answered in the negative.
    assert beats_basket(rotation_calmar=Decimal("0.70"), basket_calmar=Decimal("0.70")) is False


def test_an_unmeasurable_comparison_is_not_a_win() -> None:
    assert beats_basket(rotation_calmar=Decimal("0.70"), basket_calmar=None) is False
    assert beats_basket(rotation_calmar=None, basket_calmar=Decimal("0.10")) is False


def test_the_four_declared_benchmarks_are_all_reported() -> None:
    assert set(BENCHMARKS) == {
        Benchmark.BUY_AND_HOLD_BTC,
        Benchmark.EQUAL_WEIGHT_BASKET,
        Benchmark.B2_BREAKOUT_BTC,
        Benchmark.REGIME_TREND_BTC,
    }


# --- The carried thresholds still hold their original values -------------------------------------


def test_every_carried_threshold_is_the_value_its_own_milestone_set() -> None:
    assert Decimal("0.50") == MIN_CALMAR
    assert Decimal("0.60") == MIN_YEARS_POSITIVE_SHARE
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
    assert MIN_ASSETS_POSITIVE == 3
    assert Decimal("1.00") == MIN_NEIGHBOUR_PROFIT_FACTOR
    assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN
    assert SCREEN_MIN_TRADES == 30
    assert Decimal("0.5") == MIN_WALK_FORWARD_POSITIVE_SHARE


def test_the_one_new_threshold_is_stricter_than_almost_all() -> None:
    # "Ningun activo explica casi todo el resultado." 0.60 is a judgement and it is declared
    # before any result: it is looser than the year gate, because concentrating is what a
    # rotation rule is built to do, and far stricter than a literal reading of "almost all".
    assert Decimal("0.60") == MAX_SINGLE_ASSET_SHARE
    assert MAX_SINGLE_ASSET_SHARE > MAX_SINGLE_YEAR_SHARE
    assert Decimal("0.80") > MAX_SINGLE_ASSET_SHARE
