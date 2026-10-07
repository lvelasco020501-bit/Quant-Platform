"""M38's declaration must freeze M37's candidate, not restate or soften it.

Two things these tests exist to prevent. The candidate drifting from the one M37 froze -- M38
names it by key and must build the very configuration ``m37_definitions`` already builds, with
no number of its own. And a gate quietly becoming reachable: every threshold is inherited from
an earlier milestone, and each gate must be able to fail on its own.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantplatform.core.enums import Timeframe
from quantplatform.research import m38
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
)
from quantplatform.research.m30 import MAX_SINGLE_ASSET_SHARE
from quantplatform.research.m34 import MAX_PER_SLEEVE_SHARE
from quantplatform.research.m36 import BREADTHS
from quantplatform.research.m37 import TURNOVER_REDUCTION_FOR_PRIMARY, Mechanism
from quantplatform.research.m37_alternatives import ALTERNATIVES
from quantplatform.research.m37_definitions import alternative_risk, baseline_risk
from quantplatform.research.m38 import (
    CANDIDATE_KEY,
    MIN_TURNOVER_REDUCTION,
    REMOVED_BY_V3,
    SAFETY_INVARIANTS,
    Basis,
    BreadthResult,
    Gate,
    SafetyInvariant,
    invariants_for,
    passes,
    pool_symbols,
    turnover_reduced,
)
from quantplatform.risk.config import RiskConfiguration

TIMEFRAME = Timeframe.H4


def _candidate() -> RiskConfiguration:
    spec = next(a for a in ALTERNATIVES if a.key == CANDIDATE_KEY)
    return alternative_risk(spec, TIMEFRAME)


def _result(**over: object) -> BreadthResult:
    """Return a result that passes every gate, overridden per test."""
    fields: dict[str, object] = {
        "basis": Basis.RISK_V3,
        "breadth": 6,
        "annual": Decimal("0.30"),
        "max_drawdown": Decimal("0.30"),
        "calmar_ratio": Decimal("1.00"),
        "profit_factor": Decimal("1.40"),
        "turnover": Decimal(200),
        "fees": Decimal(20000),
        "stops": 100,
        "re_entries": 20,
        "held_share_of_wanted": Decimal("0.90"),
        "out_of_sample_return": Decimal("0.50"),
        "annual_at_double_cost": Decimal("0.20"),
        "annual_at_triple_cost": Decimal("0.10"),
        "single_year_share": Decimal("0.40"),
        "top_asset_share": Decimal("0.30"),
        "top_sleeve_share": Decimal("0.50"),
        "years_positive_share": Decimal("0.70"),
    }
    return BreadthResult.model_validate({**fields, **over})


def _deployed() -> BreadthResult:
    return _result(basis=Basis.RISK_V2, turnover=Decimal(400), calmar_ratio=Decimal("0.65"))


class TestTheCandidateIsM37sAndNotARestatement:
    """M38 must measure the configuration M37 froze, with no second copy of its numbers."""

    def test_the_candidate_is_named_by_key_rather_than_rebuilt(self) -> None:
        assert CANDIDATE_KEY == "ALT2"
        assert any(a.key == CANDIDATE_KEY for a in ALTERNATIVES)

    def test_m38_declares_no_stop_distance_of_its_own(self) -> None:
        # The distance must come from M37's rule, so M38 must hold no value that could drift
        # from it. Checked over the module's own namespace rather than its text: the docstring
        # says "1200 is not adjusted", and a check that banned the characters would forbid the
        # sentence promising not to touch the number.
        distance = _candidate().initial_stop_distance_bps
        assert distance is not None
        values = [
            getattr(m38, name)
            for name in dir(m38)
            if not name.startswith("_") and isinstance(getattr(m38, name), Decimal)
        ]
        assert distance not in values

    def test_the_removals_match_m37s_declaration_exactly(self) -> None:
        spec = next(a for a in ALTERNATIVES if a.key == CANDIDATE_KEY)
        assert frozenset(spec.removes) == REMOVED_BY_V3

    def test_the_candidate_keeps_the_stop_and_the_sizing(self) -> None:
        assert Mechanism.INITIAL_STOP not in REMOVED_BY_V3
        assert Mechanism.SIZING not in REMOVED_BY_V3

    def test_the_universe_is_the_full_point_in_time_pool(self) -> None:
        assert len(list(pool_symbols())) == 30

    def test_both_breadths_m36_judged_are_judged_again(self) -> None:
        assert BREADTHS == (6, 12)


class TestThresholdsAreInherited:
    """M38 invents one number, and even that one is M37's."""

    def test_every_performance_threshold_comes_from_an_earlier_milestone(self) -> None:
        assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN
        assert Decimal("0.50") == MIN_CALMAR
        assert COST_STRESS_MULTIPLIERS == (2, 3)
        assert Decimal("0.60") == MAX_SINGLE_ASSET_SHARE
        assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
        assert Decimal("1.00") == MIN_NEIGHBOUR_PROFIT_FACTOR
        assert MAX_PER_SLEEVE_SHARE == MAX_SINGLE_ASSET_SHARE

    def test_the_turnover_bar_is_the_one_m37_already_used(self) -> None:
        assert MIN_TURNOVER_REDUCTION == TURNOVER_REDUCTION_FOR_PRIMARY


class TestSafetyInvariantsReadTheConfigurationNotTheSample:
    """Whether a stop exists is a fact about the configuration, not about what a run reached."""

    def test_the_candidate_holds_every_invariant(self) -> None:
        assert invariants_for(_candidate(), baseline_risk(TIMEFRAME)) == SAFETY_INVARIANTS

    def test_the_deployed_configuration_lacks_only_the_ratchet_invariant(self) -> None:
        # Risk V2 carries a trailing stop, which never retreats and is a ratchet by
        # construction. The candidate is therefore strictly safer here and no weaker elsewhere.
        deployed = baseline_risk(TIMEFRAME)
        held = invariants_for(deployed, deployed)
        assert SAFETY_INVARIANTS - held == {SafetyInvariant.NO_RATCHET}

    def test_a_configuration_without_a_stop_loses_two_invariants(self) -> None:
        base = baseline_risk(TIMEFRAME)
        naked = RiskConfiguration.model_validate(
            {**base.model_dump(), "initial_stop_distance_bps": None, "risk_budget": None}
        )
        held = invariants_for(naked, base)
        assert SafetyInvariant.STOP_EXISTS not in held
        assert SafetyInvariant.SIZED_BY_RISK not in held

    def test_a_stop_at_the_budget_boundary_fails_the_window_invariant(self) -> None:
        # M37 established that a stop configured at max_stop_distance_bps realises outside it
        # once rounded to the tick, after which the engine refuses every entry. The invariant is
        # strict so such a configuration cannot be called safe.
        base = baseline_risk(TIMEFRAME)
        assert base.risk_budget is not None
        boundary = RiskConfiguration.model_validate(
            {
                **base.model_dump(),
                "initial_stop_distance_bps": base.risk_budget.max_stop_distance_bps,
            }
        )
        assert SafetyInvariant.STOP_WITHIN_BUDGET_WINDOW not in invariants_for(boundary, base)

    def test_a_latched_configuration_fails_the_latch_invariant(self) -> None:
        base = baseline_risk(TIMEFRAME)
        latched = RiskConfiguration.model_validate(
            {
                **base.model_dump(),
                "latch_total_drawdown": True,
                "max_total_drawdown_pct": Decimal("0.20"),
            }
        )
        assert SafetyInvariant.NO_LATCH not in invariants_for(latched, base)

    def test_weakened_breakers_fail_the_breaker_invariant(self) -> None:
        base = baseline_risk(TIMEFRAME)
        loosened = RiskConfiguration.model_validate(
            {**base.model_dump(), "max_daily_loss_pct": None}
        )
        assert SafetyInvariant.BREAKERS_PRESENT not in invariants_for(loosened, base)


class TestEachGateCanFailOnItsOwn:
    """A gate nothing can trip is not a gate."""

    def test_a_sound_candidate_passes(self) -> None:
        ok, failed = passes(_result(), _deployed(), invariants_held=SAFETY_INVARIANTS)
        assert ok
        assert failed == ()

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("max_drawdown", Decimal("0.40"), Gate.DRAWDOWN),
            ("calmar_ratio", Decimal("0.40"), Gate.LOW_CALMAR),
            ("out_of_sample_return", Decimal("-0.1"), Gate.OUT_OF_SAMPLE_NEGATIVE),
            ("annual_at_double_cost", Decimal("-0.01"), Gate.COST_FRAGILE),
            ("annual_at_triple_cost", Decimal("-0.01"), Gate.COST_FRAGILE),
            ("turnover", Decimal(390), Gate.TURNOVER_NOT_REDUCED),
            ("top_asset_share", Decimal("0.70"), Gate.ASSET_CONCENTRATION),
            ("top_sleeve_share", Decimal("0.90"), Gate.SLEEVE_CONCENTRATION),
            ("single_year_share", Decimal("0.60"), Gate.SINGLE_YEAR_CONCENTRATION),
            ("profit_factor", Decimal("0.90"), Gate.WEAK_PROFIT_FACTOR),
        ],
    )
    def test_one_measure_fails_one_gate(self, field: str, value: Decimal, expected: Gate) -> None:
        override: dict[str, object] = {field: value}
        ok, failed = passes(_result(**override), _deployed(), invariants_held=SAFETY_INVARIANTS)
        assert not ok
        assert expected in failed

    def test_a_missing_invariant_fails_the_safety_gate(self) -> None:
        ok, failed = passes(
            _result(),
            _deployed(),
            invariants_held=SAFETY_INVARIANTS - {SafetyInvariant.NO_RATCHET},
        )
        assert not ok
        assert Gate.SAFETY_REGRESSION in failed

    def test_a_missing_calmar_cannot_pass_by_default(self) -> None:
        ok, failed = passes(
            _result(calmar_ratio=None), _deployed(), invariants_held=SAFETY_INVARIANTS
        )
        assert not ok
        assert Gate.LOW_CALMAR in failed

    def test_turnover_is_judged_against_the_deployed_basis_not_a_constant(self) -> None:
        candidate = _result(turnover=Decimal(200))
        assert turnover_reduced(candidate, _deployed())
        assert not turnover_reduced(candidate, _result(basis=Basis.RISK_V2, turnover=Decimal(210)))

    def test_a_deployed_basis_that_never_traded_cannot_certify_a_reduction(self) -> None:
        assert not turnover_reduced(_result(), _result(basis=Basis.RISK_V2, turnover=Decimal(0)))


class TestTheDeclarationIsFrozen:
    """A declaration that could be edited after the fact would not be one."""

    def test_a_result_cannot_be_mutated(self) -> None:
        with pytest.raises(ValidationError):
            _result().breadth = 12  # type: ignore[misc]

    def test_a_result_rejects_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            BreadthResult.model_validate({"basis": Basis.RISK_V3, "breadth": 6, "surprise": 1})

    def test_the_three_bases_are_distinct(self) -> None:
        assert len({Basis.SIGNAL, Basis.RISK_V2, Basis.RISK_V3}) == 3
