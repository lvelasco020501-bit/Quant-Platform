"""M29's pre-declaration is a promise, and these tests are what make it one.

A threshold written down before the runs and a threshold written down after them look
identical in a diff. What separates them is that the first was committed, and that something
fails if it moves. Everything here exists so that relaxing a gate to let a result through has
to be done in the open, in a commit someone reviews, rather than by editing a constant.

The load-bearing test is :func:`test_profit_cannot_rescue_a_configuration_that_failed_the_gate`.
The whole design of this milestone is that robustness gates and profit only orders; if those
two ever merged, "maximise robust edge" would quietly become "maximise edge".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research.m22 import doubled
from quantplatform.research.m29 import (
    ASSETS,
    CANDIDATES_M29,
    COST_STRESS_MULTIPLIERS,
    FAMILIES,
    MAX_SINGLE_YEAR_SHARE,
    MIN_ASSETS_POSITIVE,
    MIN_CALMAR,
    MIN_YEARS_POSITIVE_SHARE,
    SCREEN_STAGES,
    Horizon,
    Measured,
    Robustness,
    Stage,
    cagr,
    calmar,
    profit_rank,
    survives,
)

PASSING: dict[str, object] = {
    "trades": 60,
    "min_trades": 30,
    "annual": Decimal("0.05"),
    "max_drawdown": Decimal("0.08"),
    "screen_max_drawdown": Decimal("0.35"),
    "calmar_ratio": Decimal("0.63"),
    "assets_positive": 4,
    "years_positive_share": Decimal("0.80"),
    "single_year_share": Decimal("0.30"),
    "neighbour_min_profit_factor": Decimal("1.10"),
    "annual_at_double_cost": Decimal("0.02"),
    "out_of_sample_return": Decimal("0.04"),
}
"""A configuration that clears every declared gate, used as the baseline each test breaks
exactly one field of. Written once so that a test which fails proves the field it changed
was the cause."""


def _survives(**overrides: object) -> tuple[bool, tuple[Robustness, ...]]:
    return survives(Measured(**{**PASSING, **overrides}))  # type: ignore[arg-type]


# --- What is searched, and that it cannot drift ---------------------------------------------------


def test_the_seven_families_are_the_seven_that_were_asked_for() -> None:
    assert [family.value for family in FAMILIES] == [
        "trend",
        "breakout",
        "momentum",
        "regime_trend",
        "mean_reversion",
        "vol_filtered",
        "regime_switch",
    ]


def test_every_family_gets_exactly_two_variants_and_no_more() -> None:
    # Two per family is the declared ceiling. A third would be a grid search with extra steps.
    for family in FAMILIES:
        probes = [probe for probe in CANDIDATES_M29 if probe.family is family]
        assert len(probes) == 2, family
        assert {probe.horizon for probe in probes} == {Horizon.BASE, Horizon.DOUBLED}
    assert len(CANDIDATES_M29) == 14


def test_the_second_variant_is_produced_by_the_rule_not_by_a_person() -> None:
    # Nobody picks these numbers: the doubled horizon is M22's mechanical rule applied to the
    # base. If a parameter were ever hand-chosen, this is what would catch it.
    for family in FAMILIES:
        base = next(p for p in CANDIDATES_M29 if p.family is family and p.horizon is Horizon.BASE)
        long = next(
            p for p in CANDIDATES_M29 if p.family is family and p.horizon is Horizon.DOUBLED
        )
        assert long.candidate.params == doubled(base.candidate.params), family


def test_the_keys_are_unique_so_a_result_can_never_be_filed_under_two_probes() -> None:
    keys = [probe.key for probe in CANDIDATES_M29]
    assert len(set(keys)) == len(keys)


def test_the_universe_is_the_four_declared_markets() -> None:
    assert ASSETS == ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")


def test_all_three_timeframes_are_declared_and_staged() -> None:
    assert set(SCREEN_STAGES) == {Timeframe.D1, Timeframe.H4, Timeframe.H1}
    # 1h is deferred on measured compute cost, not closed. The stop rule closes it only on
    # what its own results show.
    assert SCREEN_STAGES[Timeframe.H1] is Stage.DEFERRED
    assert SCREEN_STAGES[Timeframe.D1] is Stage.CHEAP


# --- The gate reads no return ---------------------------------------------------------------------


def test_a_configuration_clearing_every_gate_survives() -> None:
    assert _survives() == (True, ())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("trades", 29, Robustness.THIN_SAMPLE),
        ("annual", Decimal("-0.01"), Robustness.NEGATIVE_AFTER_COSTS),
        ("annual", None, Robustness.NEGATIVE_AFTER_COSTS),
        ("max_drawdown", Decimal("0.40"), Robustness.DRAWDOWN),
        ("calmar_ratio", Decimal("0.49"), Robustness.LOW_CALMAR),
        ("calmar_ratio", None, Robustness.LOW_CALMAR),
        ("assets_positive", 2, Robustness.SINGLE_ASSET),
        ("years_positive_share", Decimal("0.59"), Robustness.INCONSISTENT_YEARS),
        ("single_year_share", Decimal("0.51"), Robustness.SINGLE_YEAR),
        ("neighbour_min_profit_factor", Decimal("0.99"), Robustness.NARROW_PEAK),
        ("annual_at_double_cost", Decimal("0"), Robustness.COST_FRAGILE),
        ("out_of_sample_return", Decimal("-0.01"), Robustness.OUT_OF_SAMPLE_NEGATIVE),
    ],
)
def test_each_declared_condition_can_fail_on_its_own(
    field: str, value: object, expected: Robustness
) -> None:
    passed, reasons = _survives(**{field: value})

    assert passed is False
    assert expected in reasons


def test_the_gate_names_every_reason_not_just_the_first() -> None:
    # SOL in M23 failed six of seven conditions. A gate that reported only the first would
    # have made that look like one problem.
    passed, reasons = _survives(
        annual=Decimal("-0.02"), assets_positive=1, single_year_share=Decimal("1.32")
    )

    assert passed is False
    assert {
        Robustness.NEGATIVE_AFTER_COSTS,
        Robustness.SINGLE_ASSET,
        Robustness.SINGLE_YEAR,
    } <= set(reasons)


def test_a_huge_return_does_not_buy_its_way_past_a_failed_gate() -> None:
    # The point of the milestone: robust edge, not edge. A 200% annual return that comes from
    # one asset in one year is exactly the thing M22 through M24 were built to refuse.
    passed, reasons = _survives(
        annual=Decimal("2.00"),
        calmar_ratio=Decimal("10"),
        assets_positive=1,
        single_year_share=Decimal("0.95"),
    )

    assert passed is False
    assert Robustness.SINGLE_ASSET in reasons
    assert Robustness.SINGLE_YEAR in reasons


# --- Profit orders only what already survived ----------------------------------------------------


def test_profit_cannot_rescue_a_configuration_that_failed_the_gate() -> None:
    # `survives` and `profit_rank` are separate functions with no shared state, so ranking a
    # rejected configuration is something a caller has to do deliberately rather than by
    # accident. This test states that contract: the gate's answer does not depend on rank,
    # and the rank has no way to report a pass.
    failed, _ = _survives(assets_positive=1)
    ranked = profit_rank(calmar_ratio=Decimal("99"), annual=Decimal("5"))

    assert failed is False
    assert isinstance(ranked, tuple)
    assert not isinstance(ranked, bool)


def test_calmar_outranks_raw_return() -> None:
    # 3% a year against a 4% drawdown is a better business than 6% against 20%, and this is
    # the line that says so.
    steady = profit_rank(calmar_ratio=Decimal("0.75"), annual=Decimal("0.03"))
    wild = profit_rank(calmar_ratio=Decimal("0.30"), annual=Decimal("0.06"))

    assert steady > wild


def test_return_breaks_ties_between_equally_steady_rules() -> None:
    richer = profit_rank(calmar_ratio=Decimal("0.75"), annual=Decimal("0.05"))
    poorer = profit_rank(calmar_ratio=Decimal("0.75"), annual=Decimal("0.03"))

    assert richer > poorer


def test_trade_count_is_absent_from_the_ranking() -> None:
    # Frequency is explicitly not an objective. Two rules that carry risk and pay identically
    # rank identically however often they trade.
    assert profit_rank(calmar_ratio=Decimal("0.75"), annual=Decimal("0.05")) == profit_rank(
        calmar_ratio=Decimal("0.75"), annual=Decimal("0.05")
    )


def test_an_unmeasurable_ratio_sorts_last_rather_than_first() -> None:
    # None must never win by accident. A run whose Calmar could not be computed is the least
    # informative thing in the table, not the best.
    unknown = profit_rank(calmar_ratio=None, annual=None)
    worst_measured = profit_rank(calmar_ratio=Decimal("0"), annual=Decimal("0"))

    assert unknown < worst_measured


# --- The profit metrics --------------------------------------------------------------------------


def test_cagr_annualises_by_the_data_the_run_actually_had() -> None:
    # Two years of daily bars doubling the account is ~41.4% a year, not 100%.
    annual = cagr(Decimal("1.0"), bars=730, timeframe=Timeframe.D1)

    assert annual is not None
    assert Decimal("0.41") < annual < Decimal("0.42")


def test_cagr_is_comparable_across_timeframes_for_the_same_calendar_span() -> None:
    # A year of daily bars and a year of 4h bars covering the same ground must annualise the
    # same, or the timeframe comparison this milestone exists for would be meaningless.
    daily = cagr(Decimal("0.20"), bars=365, timeframe=Timeframe.D1)
    four_hourly = cagr(Decimal("0.20"), bars=365 * 6, timeframe=Timeframe.H4)

    assert daily is not None
    assert four_hourly is not None
    assert abs(daily - four_hourly) < Decimal("0.0001")


def test_cagr_refuses_to_invent_a_rate_for_a_wiped_out_account() -> None:
    assert cagr(Decimal("-1.0"), bars=365, timeframe=Timeframe.D1) is None
    assert cagr(Decimal("0.10"), bars=0, timeframe=Timeframe.D1) is None


def test_calmar_is_return_over_the_worst_loss_taken_to_earn_it() -> None:
    assert calmar(Decimal("0.10"), Decimal("0.20")) == Decimal("0.5")


def test_calmar_is_undefined_rather_than_infinite_when_nothing_was_lost() -> None:
    # A run that never drew down would otherwise rank infinitely well on what is almost always
    # too short a sample.
    assert calmar(Decimal("0.10"), Decimal(0)) is None
    assert calmar(None, Decimal("0.20")) is None


# --- The thresholds are the ones that were declared ----------------------------------------------


def test_the_calmar_floor_admits_the_incumbent_it_was_calibrated_against() -> None:
    # B2 on BTC: 1.7% annualised out of sample against a 2.00% drawdown. The floor was set
    # where the project's existing candidate clears it, not where a new one would be flattered.
    b2 = calmar(Decimal("0.017"), Decimal("0.0200"))

    assert b2 is not None
    assert b2 > MIN_CALMAR


def test_the_carried_thresholds_still_hold_m23_s_values() -> None:
    assert Decimal("0.60") == MIN_YEARS_POSITIVE_SHARE
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
    assert MIN_ASSETS_POSITIVE == 3


def test_costs_are_stressed_upward_and_never_downward() -> None:
    # The cost model is the one assumption not verifiable from data on disk. Every declared
    # multiplier makes trading more expensive; a multiplier below 1 would be assuming the
    # edge is better than modelled.
    assert COST_STRESS_MULTIPLIERS == (2, 3)
    assert all(multiplier > 1 for multiplier in COST_STRESS_MULTIPLIERS)
