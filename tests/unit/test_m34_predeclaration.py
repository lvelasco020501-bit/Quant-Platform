"""M34's pre-declaration is a promise, and these tests are what make it one.

The load-bearing one is ``test_the_sleeves_are_m29_s_own_objects``: this milestone's whole claim
is that it combines edges the project already has without touching them, and object identity is
the only way to check that rather than assert it. ``test_the_allocation_introduces_no_number_of
_its_own`` is the second — both fractions must fall out of M32's declared breadth, because an
allocation chosen by a person is an allocation that can be chosen again after seeing results.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import CANDIDATES_M29, MAX_SINGLE_YEAR_SHARE, MIN_CALMAR
from quantplatform.research.m30 import MAX_SINGLE_ASSET_SHARE, ONE_WAY_COST_BASIS_POINTS
from quantplatform.research.m32 import LIQUIDITY_WINDOW, UNIVERSE_SIZE
from quantplatform.research.m34 import (
    BENCHMARKS,
    COST_STRESS_MULTIPLIERS,
    MAX_PER_ASSET,
    MAX_PER_SLEEVE_SHARE,
    NEIGHBOUR_UNIVERSE_SIZES,
    PORTFOLIOS,
    SLEEVE_KEYS,
    SLEEVES,
    TIMEFRAME,
    Benchmark,
    Combined,
    Portfolio,
    PortfolioRobustness,
    pool_symbols,
    sleeves_of,
    survives,
    weight_per_signal,
)
from quantplatform.research.portfolio import Allocation

PASSING: dict[str, object] = {
    "annual": Decimal("0.11"),
    "max_drawdown": Decimal("0.19"),
    "calmar_ratio": Decimal("0.58"),
    "single_year_share": Decimal("0.38"),
    "top_asset_share": Decimal("0.44"),
    "top_sleeve_share": Decimal("0.55"),
    "annual_at_double_cost": Decimal("0.07"),
    "annual_at_triple_cost": Decimal("0.04"),
    "out_of_sample_return": Decimal("0.21"),
    "neighbours_positive": True,
    "neighbours_within_drawdown": True,
}
"""A portfolio clearing every declared gate, the baseline each test breaks exactly once."""


def _survives(**overrides: object) -> tuple[bool, tuple[PortfolioRobustness, ...]]:
    return survives(Combined(**{**PASSING, **overrides}))  # type: ignore[arg-type]


# --- Nothing is invented --------------------------------------------------------------------------


def test_the_sleeves_are_m29_s_own_objects() -> None:
    # Identity, not equality. If anyone restates B2's periods or regime_trend's threshold in M34
    # instead of reaching for M29's probe, there would be two copies able to drift and this fails.
    for probe in SLEEVES:
        assert probe is next(other for other in CANDIDATES_M29 if other.key == probe.key)


def test_only_b2_and_regime_trend_are_combined() -> None:
    assert SLEEVE_KEYS == ("B2", "G1")
    assert [p.candidate.strategy_id for p in SLEEVES] == ["breakout_trend", "regime_trend"]


def test_b2_is_the_configuration_running_in_paper() -> None:
    b2 = next(p for p in SLEEVES if p.key == "B2")
    assert dict(b2.candidate.params) == {
        "entry_lookback": "40",
        "exit_lookback": "20",
        "trend_period": "400",
    }


def test_the_allocation_introduces_no_number_of_its_own() -> None:
    # Both fractions fall out of M32's declared breadth. An allocation a person picked is an
    # allocation a person could pick again after seeing the results.
    assert Decimal(1) / Decimal(UNIVERSE_SIZE) == MAX_PER_ASSET
    assert weight_per_signal(sleeves=1) == Decimal(1) / Decimal(UNIVERSE_SIZE)
    assert weight_per_signal(sleeves=2) == Decimal(1) / Decimal(2 * UNIVERSE_SIZE)


def test_a_full_book_is_exactly_the_account_and_never_more() -> None:
    for sleeves in (1, 2, 3):
        assert weight_per_signal(sleeves=sleeves) * UNIVERSE_SIZE * sleeves == Decimal(1)


def test_the_universe_is_m32_s_corrected_pool_and_rule() -> None:
    assert len(pool_symbols()) == 30
    assert UNIVERSE_SIZE == 6
    assert LIQUIDITY_WINDOW == 72


def test_the_timeframe_is_four_hours_and_one_hour_is_excluded() -> None:
    assert TIMEFRAME is Timeframe.H4


def test_costs_are_the_platform_s_own_and_stressed_upward() -> None:
    assert Decimal(15) == ONE_WAY_COST_BASIS_POINTS
    assert COST_STRESS_MULTIPLIERS == (2, 3)


def test_the_sleeve_concentration_bar_reuses_the_asset_one() -> None:
    # "Does one component explain the result" is the same question about a different axis, so it
    # gets the same answer rather than a new number.
    assert MAX_PER_SLEEVE_SHARE == MAX_SINGLE_ASSET_SHARE == Decimal("0.60")


def test_the_breadth_probes_are_half_and_double() -> None:
    assert NEIGHBOUR_UNIVERSE_SIZES == (UNIVERSE_SIZE // 2, UNIVERSE_SIZE * 2)
    assert UNIVERSE_SIZE not in NEIGHBOUR_UNIVERSE_SIZES


# --- The three portfolios -------------------------------------------------------------------------


def test_the_single_sleeve_portfolios_are_the_same_machinery_as_the_combined_one() -> None:
    # A and B exist so that C is a comparison of *combining*, not of two different designs.
    assert PORTFOLIOS[Portfolio.B2_ONLY] == ("B2",)
    assert PORTFOLIOS[Portfolio.REGIME_ONLY] == ("G1",)
    assert PORTFOLIOS[Portfolio.COMBINED] == SLEEVE_KEYS


def test_each_portfolio_resolves_to_frozen_sleeves() -> None:
    for portfolio in Portfolio:
        sleeves = sleeves_of(portfolio)
        assert sleeves
        assert len(sleeves) == len(PORTFOLIOS[portfolio])
        for sleeve in sleeves:
            probe = next(p for p in SLEEVES if p.key == sleeve.key)
            assert sleeve.params == probe.candidate.params


def test_the_four_declared_benchmarks_are_all_reported() -> None:
    assert set(BENCHMARKS) == {
        Benchmark.B2_BTC,
        Benchmark.REGIME_BTC,
        Benchmark.EQUAL_WEIGHT_BASKET,
        Benchmark.BUY_AND_HOLD_BTC,
    }


# --- The gate -------------------------------------------------------------------------------------


def test_a_portfolio_clearing_every_gate_survives() -> None:
    assert _survives() == (True, ())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("annual", Decimal("-0.01"), PortfolioRobustness.NEGATIVE_AFTER_COSTS),
        ("annual", None, PortfolioRobustness.NEGATIVE_AFTER_COSTS),
        ("max_drawdown", Decimal("0.36"), PortfolioRobustness.DRAWDOWN),
        ("calmar_ratio", Decimal("0.49"), PortfolioRobustness.LOW_CALMAR),
        ("single_year_share", Decimal("0.51"), PortfolioRobustness.SINGLE_YEAR),
        ("top_asset_share", Decimal("0.61"), PortfolioRobustness.SINGLE_ASSET),
        ("top_asset_share", None, PortfolioRobustness.SINGLE_ASSET),
        ("top_sleeve_share", Decimal("0.61"), PortfolioRobustness.SINGLE_SLEEVE),
        ("annual_at_double_cost", Decimal(0), PortfolioRobustness.COST_FRAGILE),
        ("annual_at_triple_cost", Decimal("-0.01"), PortfolioRobustness.COST_FRAGILE),
        ("out_of_sample_return", Decimal("-0.01"), PortfolioRobustness.OUT_OF_SAMPLE_NEGATIVE),
        ("neighbours_positive", False, PortfolioRobustness.FRAGILE_TO_BREADTH),
        ("neighbours_within_drawdown", False, PortfolioRobustness.FRAGILE_TO_BREADTH),
    ],
)
def test_each_declared_condition_can_fail_on_its_own(
    field: str, value: object, expected: PortfolioRobustness
) -> None:
    passed, reasons = _survives(**{field: value})

    assert passed is False
    assert expected in reasons


def test_a_single_sleeve_portfolio_is_not_failed_for_owning_its_own_profit() -> None:
    # A one-sleeve portfolio trivially has a sleeve share of one. Failing it for that would make
    # the benchmarks unmeasurable against the thing they are benchmarking.
    passed, reasons = _survives(top_sleeve_share=None)

    assert passed is True
    assert PortfolioRobustness.SINGLE_SLEEVE not in reasons


def test_sensitivity_is_expressed_with_conditions_that_were_already_declared() -> None:
    # Profitable after costs, and inside the drawdown cap: both already in the gate, applied to
    # the neighbours. Inventing a fresh number for the neighbourhood would be the kind of
    # after-the-fact criterion this gate exists to exclude.
    assert Combined.model_fields["neighbours_positive"].annotation is bool
    assert Combined.model_fields["neighbours_within_drawdown"].annotation is bool


def test_the_gate_names_every_reason_not_just_the_first() -> None:
    passed, reasons = _survives(
        annual=Decimal("-0.03"), max_drawdown=Decimal("0.70"), top_sleeve_share=Decimal("0.99")
    )

    assert passed is False
    assert {
        PortfolioRobustness.NEGATIVE_AFTER_COSTS,
        PortfolioRobustness.DRAWDOWN,
        PortfolioRobustness.SINGLE_SLEEVE,
    } <= set(reasons)


def test_every_carried_threshold_is_inherited_unchanged() -> None:
    assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN
    assert Decimal("0.50") == MIN_CALMAR
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
    assert Decimal("0.60") == MAX_SINGLE_ASSET_SHARE


def test_the_declared_allocation_matches_what_the_allocator_implements() -> None:
    # The module declares the fractions; the allocator computes them. They must not drift.
    for sleeves in (1, 2):
        allocation = Allocation(universe_size=UNIVERSE_SIZE, sleeves=sleeves)
        assert allocation.per_signal == weight_per_signal(sleeves=sleeves)
        assert allocation.per_asset_cap == MAX_PER_ASSET
