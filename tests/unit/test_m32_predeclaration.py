"""M32's pre-declaration is a promise, and these tests are what make it one.

This milestone exists to attack its own predecessor's result, so the tests that matter are the
ones stopping it from going soft on what it is checking.
``test_the_gate_is_literally_the_function_m31_was_judged_by`` compares function identity: M32
cannot hold a different threshold than the milestone whose conclusion it is re-examining.
``test_the_pool_contains_the_two_that_went_to_zero`` guards the single most important property of
the corrected universe -- a survivorship test without LUNA and FTT in it is not one.
"""

from __future__ import annotations

from quantplatform.core.enums import Timeframe
from quantplatform.research import m31
from quantplatform.research.m30 import ONE_WAY_COST_BASIS_POINTS, OOS_START
from quantplatform.research.m31 import FROZEN
from quantplatform.research.m32 import (
    ASSETS_M30,
    CANDIDATES_M32,
    COST_STRESS_MULTIPLIERS,
    EQUITY_TOLERANCE_DECLARED,
    FIXED_EXPOSURE,
    LIQUIDITY_WINDOW,
    POOL,
    SIGNAL_DIVERGENCES_ALLOWED,
    UNIVERSE_SIZE,
    Era,
    Phase2Claim,
    already_validated,
    pool_symbols,
    signal_of,
    survives,
    to_download,
)
from quantplatform.research.sprint import CANDIDATES

# --- The candidates are M31's, by reference ------------------------------------------------------


def test_the_two_candidates_are_the_two_m31_passed() -> None:
    assert [c.key for c in CANDIDATES_M32] == ["CS2-fixed25%-1d", "RF1-fixed25%-4h"]
    assert [c.timeframe for c in CANDIDATES_M32] == [Timeframe.D1, Timeframe.H4]


def test_each_candidate_s_signal_is_m30_s_own_object() -> None:
    # Identity again, through two milestones. If anyone restates CS2's threshold in M32 rather
    # than reaching it through M31's FROZEN, this fails.
    for candidate in CANDIDATES_M32:
        rule = signal_of(candidate)
        assert rule is next(other for other in FROZEN if other.key == candidate.signal)


def test_the_frozen_signals_still_carry_the_parameters_they_were_measured_at() -> None:
    declared = {c.strategy_id: dict(c.params) for c in CANDIDATES}
    for candidate in CANDIDATES_M32:
        rule = signal_of(candidate)
        assert rule.lookback == int(declared["momentum_roc"]["lookback"])


def test_the_exposure_stays_the_twenty_five_percent_that_passed() -> None:
    assert FIXED_EXPOSURE == "fixed 25%"
    assert m31.policy_for(FIXED_EXPOSURE).fixed is not None


def test_nothing_about_the_cost_or_the_window_moves() -> None:
    assert ONE_WAY_COST_BASIS_POINTS == m31.ONE_WAY_COST_BASIS_POINTS
    assert COST_STRESS_MULTIPLIERS == (2, 3)
    assert OOS_START.year == 2024


# --- The gate is not restated --------------------------------------------------------------------


def test_the_gate_is_literally_the_function_m31_was_judged_by() -> None:
    # Not an equivalent gate: the same object. A milestone that re-examines a result must not be
    # able to hold it to a different bar than the one it originally cleared.
    assert survives is m31.survives


def test_the_gate_still_has_exactly_the_seven_declared_conditions() -> None:
    assert len(set(m31.ControlRobustness)) == 6
    assert set(m31.Controlled.model_fields) == {
        "max_drawdown",
        "calmar_ratio",
        "single_year_share",
        "top_asset_share",
        "annual_at_double_cost",
        "annual_at_triple_cost",
        "out_of_sample_return",
    }


# --- The corrected universe ----------------------------------------------------------------------


def test_the_universe_keeps_m30_s_breadth() -> None:
    # Six, so the corrected run is a test of *which* six were investable, not of how many.
    assert UNIVERSE_SIZE == len(ASSETS_M30) == 6


def test_the_liquidity_window_is_inherited_like_every_other_window_here() -> None:
    declared = {c.strategy_id: dict(c.params) for c in CANDIDATES}
    assert int(declared["momentum_roc"]["lookback"]) == LIQUIDITY_WINDOW


def test_the_pool_contains_the_two_that_went_to_zero() -> None:
    # A survivorship test without these is not one. A momentum rule ranks by trailing return,
    # so it is exactly the rule that would have rotated into both shortly before they ended.
    collapsed = {m.raw for m in POOL if m.era is Era.COLLAPSED}
    assert collapsed == {"LUNAUSDT", "FTTUSDT"}


def test_the_pool_keeps_every_incumbent_so_the_comparison_is_like_for_like() -> None:
    assert set(already_validated()) == set(ASSETS_M30)


def test_the_pool_adds_far_more_faded_markets_than_later_ones() -> None:
    # The direction of the correction is the argument for it. Adding the assets that led a
    # cycle and never returned makes the candidate's job harder, which is what makes their
    # inclusion evidence.
    faded = [m for m in POOL if m.era is Era.FADED]
    later = [m for m in POOL if m.era is Era.LATER]
    assert len(faded) > len(later)


def test_every_pool_member_says_why_it_is_there() -> None:
    for member in POOL:
        assert member.note.strip()
        assert member.era in set(Era)


def test_no_market_is_declared_twice() -> None:
    symbols = pool_symbols()
    assert len(set(symbols)) == len(symbols)


def test_the_incumbents_are_not_re_downloaded() -> None:
    # Re-acquiring them would create a second lineage for exactly the assets whose numbers M32
    # has to reproduce.
    assert not set(to_download()) & set(ASSETS_M30)
    assert len(to_download()) + len(already_validated()) == len(POOL)


# --- What phase 2 claims -------------------------------------------------------------------------


def test_phase_two_claims_exact_signal_equivalence_and_nothing_about_equity() -> None:
    # Declared in advance because the honest prediction is that the equity curves will differ:
    # the production engine sizes from a stop and runs breakers, and the rotation overlay holds
    # a weight. Promising a CAGR tolerance would be promising that Risk V2 does not matter.
    assert set(Phase2Claim) == {
        Phase2Claim.SIGNAL_EQUIVALENCE,
        Phase2Claim.ACCOUNTING_DIFFERENCE,
    }
    # Declared as data, not prose: an enum member's __doc__ is the class docstring, so a test
    # asserting on one asserts nothing at all.
    assert SIGNAL_DIVERGENCES_ALLOWED == 0
    assert EQUITY_TOLERANCE_DECLARED is False
