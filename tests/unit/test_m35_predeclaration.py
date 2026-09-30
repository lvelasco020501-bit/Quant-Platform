"""M35's declaration, and the two conditions that make it a validation rather than a re-run.

``test_the_gate_is_m34_s_plus_the_two_things_m35_exists_for`` is the one that matters: this
milestone must hold M34's candidate to M34's own thresholds, and add only the two checks M34
could not make -- stability across breadth at constant exposure, and survival of Risk V2 actually
moving the positions. A threshold that drifted between the two milestones would make the
validation meaningless.
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
from quantplatform.research.m35 import (
    BREADTHS,
    CANDIDATE,
    REFERENCE_BREADTH,
    TIMEFRAME,
    Basis,
    Phase1,
    Validated,
    ValidationRobustness,
    phase_one_passes,
    survives,
)

PASSING: dict[str, object] = {
    "basis": Basis.SIGNALS,
    "annual": Decimal("0.29"),
    "max_drawdown": Decimal("0.32"),
    "calmar_ratio": Decimal("0.92"),
    "single_year_share": Decimal("0.41"),
    "top_asset_share": Decimal("0.26"),
    "top_sleeve_share": Decimal("0.55"),
    "annual_at_double_cost": Decimal("0.24"),
    "annual_at_triple_cost": Decimal("0.18"),
    "out_of_sample_return": Decimal("0.77"),
    "breadths_stable": True,
    "certified_basis_holds": True,
}
"""M34's portfolio C as measured, with both M35 conditions satisfied."""


def _survives(**overrides: object) -> tuple[bool, tuple[ValidationRobustness, ...]]:
    return survives(Validated(**{**PASSING, **overrides}))  # type: ignore[arg-type]


def _phase1(breadth: int, *, drawdown: str, calmar: str) -> Phase1:
    return Phase1(
        breadth=breadth,
        annual=Decimal("0.29"),
        max_drawdown=Decimal(drawdown),
        calmar_ratio=Decimal(calmar),
        mean_deployed=Decimal("0.217"),
        positions_when_invested=Decimal(4),
        bars_forced_to_cash=0,
    )


# --- Nothing is searched --------------------------------------------------------------------------


def test_the_candidate_is_m34_s_combined_portfolio_unchanged() -> None:
    assert CANDIDATE is m34.Portfolio.COMBINED
    assert m34.PORTFOLIOS[CANDIDATE] == m34.SLEEVE_KEYS == ("B2", "G1")


def test_the_sleeves_still_reach_through_to_m29_s_objects() -> None:
    for probe in m34.SLEEVES:
        assert probe is next(o for o in CANDIDATES_M29 if o.key == probe.key)


def test_the_declared_breadths_are_the_three_that_were_asked_for() -> None:
    assert BREADTHS == (3, 6, 12)
    assert REFERENCE_BREADTH == UNIVERSE_SIZE == 6


def test_the_timeframe_cannot_drift_from_m34_s() -> None:
    assert TIMEFRAME is m34.TIMEFRAME is Timeframe.H4


def test_both_bases_are_declared_and_named() -> None:
    assert set(Basis) == {Basis.SIGNALS, Basis.CERTIFIED}


# --- The gate -------------------------------------------------------------------------------------


def test_the_candidate_as_m34_measured_it_clears_everything() -> None:
    assert _survives() == (True, ())


def test_the_gate_is_m34_s_plus_the_two_things_m35_exists_for() -> None:
    # Every threshold is M34's, reused rather than restated, so the validation holds the
    # candidate to the bar it originally cleared.
    assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN
    assert Decimal("0.50") == MIN_CALMAR
    assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
    assert MAX_SINGLE_ASSET_SHARE == m34.MAX_PER_SLEEVE_SHARE == Decimal("0.60")
    # Compared by value: the two enums are different classes, so their members are never the
    # same objects however identical their meanings.
    carried = {r.value for r in ValidationRobustness} - {
        ValidationRobustness.BREADTH_DEPENDENT.value,
        ValidationRobustness.RISK_V2_DESTROYS_IT.value,
    }
    assert carried == {r.value for r in m34.PortfolioRobustness} - {
        m34.PortfolioRobustness.FRAGILE_TO_BREADTH.value
    }


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("annual", Decimal("-0.01"), ValidationRobustness.NEGATIVE_AFTER_COSTS),
        ("max_drawdown", Decimal("0.36"), ValidationRobustness.DRAWDOWN),
        ("calmar_ratio", Decimal("0.49"), ValidationRobustness.LOW_CALMAR),
        ("single_year_share", Decimal("0.51"), ValidationRobustness.SINGLE_YEAR),
        ("top_asset_share", Decimal("0.61"), ValidationRobustness.SINGLE_ASSET),
        ("top_sleeve_share", Decimal("0.61"), ValidationRobustness.SINGLE_SLEEVE),
        ("annual_at_triple_cost", Decimal("-0.01"), ValidationRobustness.COST_FRAGILE),
        ("out_of_sample_return", Decimal("-0.01"), ValidationRobustness.OUT_OF_SAMPLE_NEGATIVE),
        ("breadths_stable", False, ValidationRobustness.BREADTH_DEPENDENT),
        ("certified_basis_holds", False, ValidationRobustness.RISK_V2_DESTROYS_IT),
    ],
)
def test_each_declared_condition_can_fail_on_its_own(
    field: str, value: object, expected: ValidationRobustness
) -> None:
    passed, reasons = _survives(**{field: value})

    assert passed is False
    assert expected in reasons


def test_a_portfolio_that_only_works_at_one_breadth_is_refused() -> None:
    passed, reasons = _survives(breadths_stable=False)

    assert passed is False
    assert ValidationRobustness.BREADTH_DEPENDENT in reasons


def test_a_portfolio_that_risk_v2_destroys_is_refused() -> None:
    # The condition M34 could not evaluate at all, and the reason M35 exists.
    passed, reasons = _survives(certified_basis_holds=False)

    assert passed is False
    assert ValidationRobustness.RISK_V2_DESTROYS_IT in reasons


# --- Phase 1's own stop rule ----------------------------------------------------------------------


def test_phase_one_needs_every_declared_breadth_to_hold() -> None:
    steady = tuple(_phase1(breadth, drawdown="0.30", calmar="0.90") for breadth in BREADTHS)
    assert phase_one_passes(steady) is True


def test_phase_one_fails_when_any_breadth_blows_the_drawdown_cap() -> None:
    mixed = (
        _phase1(3, drawdown="0.43", calmar="0.62"),
        _phase1(6, drawdown="0.32", calmar="0.92"),
        _phase1(12, drawdown="0.26", calmar="1.38"),
    )

    assert mixed[0].stable is False
    assert mixed[1].stable is True
    assert phase_one_passes(mixed) is False


def test_phase_one_fails_when_a_breadth_is_missing() -> None:
    # A probe that only ran two of three points has not answered the question.
    assert phase_one_passes((_phase1(6, drawdown="0.30", calmar="0.90"),)) is False


def test_a_breadth_is_judged_on_conditions_that_were_already_declared() -> None:
    # Drawdown cap and Calmar floor, applied to the neighbour. No new number for the probe.
    assert _phase1(3, drawdown="0.35", calmar="0.50").stable is True
    assert _phase1(3, drawdown="0.3501", calmar="0.50").stable is False
    assert _phase1(3, drawdown="0.35", calmar="0.4999").stable is False


def test_forced_cash_bars_are_carried_rather_than_hidden() -> None:
    # The one way the constant-exposure construction cannot hold: a breadth with no eligible
    # signal on a bar the reference was invested in. Reported, because it biases that breadth
    # favourably and a reader needs to know.
    entry = Phase1(
        breadth=3,
        annual=Decimal("0.26"),
        max_drawdown=Decimal("0.43"),
        calmar_ratio=Decimal("0.62"),
        mean_deployed=Decimal("0.205"),
        positions_when_invested=Decimal("2.6"),
        bars_forced_to_cash=2229,
    )

    assert entry.bars_forced_to_cash == 2229
    assert entry.stable is False
