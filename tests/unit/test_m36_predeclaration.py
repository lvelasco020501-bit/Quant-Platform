"""M36's declaration, and the two things it is honest about.

``test_phase_one_reads_only_the_two_operational_breadths`` pins the narrowing the user made, and
``test_sizing_and_latching_are_declared_as_not_exercised`` pins the two Risk V2 layers phase 2
cannot reach. Both are recorded as data rather than prose so a test can hold them: a milestone
that quietly forgot which layers it exercised would be claiming more than it measured.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research import m34
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import CANDIDATES_M29, MAX_SINGLE_YEAR_SHARE, MIN_CALMAR
from quantplatform.research.m30 import MAX_SINGLE_ASSET_SHARE
from quantplatform.research.m32 import UNIVERSE_SIZE
from quantplatform.research.m35 import Basis
from quantplatform.research.m36 import (
    BREADTHS,
    CANDIDATE,
    EXERCISED,
    NOT_EXERCISED,
    OPERATIONAL_BREADTH_FLOOR,
    TIMEFRAME,
    BreadthPoint,
    Divergence,
    Final36,
    FinalRobustness,
    RiskV2Layer,
    phase_one_passes,
    survives,
)

PASSING: dict[str, object] = {
    "basis": Basis.CERTIFIED,
    "breadths_passed": True,
    "annual": Decimal("0.24"),
    "max_drawdown": Decimal("0.30"),
    "calmar_ratio": Decimal("0.80"),
    "single_year_share": Decimal("0.42"),
    "top_asset_share": Decimal("0.28"),
    "top_sleeve_share": Decimal("0.56"),
    "annual_at_double_cost": Decimal("0.17"),
    "annual_at_triple_cost": Decimal("0.11"),
    "out_of_sample_return": Decimal("0.55"),
    "signal_basis_passed": True,
}
"""A certified-basis result clearing every condition."""


def _survives(**overrides: object) -> tuple[bool, tuple[FinalRobustness, ...]]:
    return survives(Final36(**{**PASSING, **overrides}))  # type: ignore[arg-type]


def _point(breadth: int, *, drawdown: str = "0.30", calmar: str = "0.90") -> BreadthPoint:
    return BreadthPoint(
        breadth=breadth,
        annual=Decimal("0.29"),
        max_drawdown=Decimal(drawdown),
        calmar_ratio=Decimal(calmar),
        annual_at_double_cost=Decimal("0.23"),
        annual_at_triple_cost=Decimal("0.18"),
        out_of_sample_return=Decimal("0.77"),
        single_year_share=Decimal("0.41"),
        top_asset_share=Decimal("0.26"),
        top_sleeve_share=Decimal("0.55"),
    )


# --- Nothing is searched --------------------------------------------------------------------------


def test_the_candidate_is_m34_s_combined_portfolio_unchanged() -> None:
    assert CANDIDATE is m34.Portfolio.COMBINED
    assert m34.PORTFOLIOS[CANDIDATE] == ("B2", "G1")


def test_the_sleeves_still_reach_through_to_m29_s_objects() -> None:
    for probe in m34.SLEEVES:
        assert probe is next(o for o in CANDIDATES_M29 if o.key == probe.key)


def test_the_timeframe_cannot_drift_from_m34_s() -> None:
    assert TIMEFRAME is m34.TIMEFRAME is Timeframe.H4


def test_every_threshold_is_the_one_its_own_milestone_set() -> None:
    assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN
    assert Decimal("0.50") == MIN_CALMAR
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
    assert MAX_SINGLE_ASSET_SHARE == m34.MAX_PER_SLEEVE_SHARE == Decimal("0.60")


# --- What phase 1 was narrowed to, and why --------------------------------------------------------


def test_phase_one_reads_only_the_two_operational_breadths() -> None:
    # The narrowing the user made, recorded so it cannot be quietly widened or forgotten.
    assert BREADTHS == (6, 12)
    assert OPERATIONAL_BREADTH_FLOOR == UNIVERSE_SIZE == 6
    assert 3 not in BREADTHS


def test_phase_one_needs_both_breadths_and_refuses_a_partial_run() -> None:
    assert phase_one_passes((_point(6), _point(12))) is True
    assert phase_one_passes((_point(6),)) is False
    assert phase_one_passes(()) is False


def test_a_breadth_is_judged_on_every_phase_one_condition() -> None:
    assert _point(6).passes is True
    assert _point(6, drawdown="0.36").passes is False
    assert _point(6, calmar="0.49").passes is False


def test_a_breadth_failing_costs_or_out_of_sample_fails_phase_one() -> None:
    thin = _point(12).model_copy(update={"annual_at_triple_cost": Decimal("-0.01")})
    negative = _point(12).model_copy(update={"out_of_sample_return": Decimal("-0.02")})

    assert thin.passes is False
    assert negative.passes is False


# --- Which Risk V2 layers phase 2 actually reaches ------------------------------------------------


def test_sizing_and_latching_are_declared_as_not_exercised() -> None:
    # The honest half of the milestone. A per-pair engine run cannot see the portfolio's equity,
    # so its sizing is not the portfolio's; and the reference policy releases the latch.
    assert {RiskV2Layer.SIZING, RiskV2Layer.LATCHING} == NOT_EXERCISED


def test_the_layers_that_decide_positions_are_declared_as_exercised() -> None:
    assert {
        RiskV2Layer.STOPS,
        RiskV2Layer.POSITION_STATE,
        RiskV2Layer.RE_ENTRIES,
        RiskV2Layer.ORDER_REJECTION,
        RiskV2Layer.BREAKERS,
    } == EXERCISED


def test_every_risk_layer_is_accounted_for_one_way_or_the_other() -> None:
    # No layer may be left unmentioned: silence about one would be a claim nobody checked.
    assert set(RiskV2Layer) == EXERCISED | NOT_EXERCISED
    assert not EXERCISED & NOT_EXERCISED


# --- The final gate -------------------------------------------------------------------------------


def test_a_certified_result_clearing_everything_passes() -> None:
    assert _survives() == (True, ())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("breadths_passed", False, FinalRobustness.BREADTH_FAILED),
        ("annual", Decimal("-0.01"), FinalRobustness.NEGATIVE_AFTER_COSTS),
        ("max_drawdown", Decimal("0.36"), FinalRobustness.DRAWDOWN),
        ("calmar_ratio", Decimal("0.49"), FinalRobustness.LOW_CALMAR),
        ("single_year_share", Decimal("0.51"), FinalRobustness.SINGLE_YEAR),
        ("top_asset_share", Decimal("0.61"), FinalRobustness.SINGLE_ASSET),
        ("top_sleeve_share", Decimal("0.61"), FinalRobustness.SINGLE_SLEEVE),
        ("annual_at_triple_cost", Decimal("-0.01"), FinalRobustness.COST_FRAGILE),
        ("out_of_sample_return", Decimal("-0.01"), FinalRobustness.OUT_OF_SAMPLE_NEGATIVE),
    ],
)
def test_each_declared_condition_can_fail_on_its_own(
    field: str, value: object, expected: FinalRobustness
) -> None:
    passed, reasons = _survives(**{field: value})

    assert passed is False
    assert expected in reasons


def test_a_failure_on_the_certified_basis_alone_is_named_as_simulator_dependence() -> None:
    # The whole reason for running the certified engine: to be able to say that a result which
    # held on signals and broke under Risk V2 was a property of the simulator.
    passed, reasons = _survives(max_drawdown=Decimal("0.44"), signal_basis_passed=True)

    assert passed is False
    assert FinalRobustness.SIMULATOR_DEPENDENT in reasons


def test_a_candidate_failing_on_both_bases_fails_on_its_own_merits() -> None:
    # Not simulator dependence: it was never good on either basis, and calling it that would
    # blame the instrument for the result.
    passed, reasons = _survives(max_drawdown=Decimal("0.44"), signal_basis_passed=False)

    assert passed is False
    assert FinalRobustness.DRAWDOWN in reasons
    assert FinalRobustness.SIMULATOR_DEPENDENT not in reasons


def test_simulator_dependence_is_never_charged_to_the_signal_basis_itself() -> None:
    # A basis is not asked to validate itself.
    passed, reasons = _survives(
        basis=Basis.SIGNALS, max_drawdown=Decimal("0.44"), signal_basis_passed=True
    )

    assert passed is False
    assert FinalRobustness.SIMULATOR_DEPENDENT not in reasons


# --- Divergences are measured, not bounded --------------------------------------------------------


def test_a_divergence_reports_the_ratio_between_the_two_bases() -> None:
    turnover = Divergence(measure="turnover", on_signals=Decimal(271), on_certified=Decimal(813))

    assert turnover.relative == Decimal(3)


def test_a_divergence_with_nothing_to_compare_reports_no_ratio() -> None:
    assert Divergence(measure="x", on_signals=None, on_certified=Decimal(1)).relative is None
    assert Divergence(measure="x", on_signals=Decimal(0), on_certified=Decimal(1)).relative is None
