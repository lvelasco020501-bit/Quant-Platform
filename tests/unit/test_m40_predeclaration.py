"""M40's causal test must refuse a pure exposure effect, which is the obvious way to be fooled.

Removing a constraint raises return almost by construction: the constraint exists to remove
exposure. So the tests below spend most of their effort on the case that looks like success --
annual return up, everything else flat or worse -- and pin that it fails. The rest check that
the two studied variants and the single ablated field come from earlier milestones by reference
rather than being restated here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantplatform.core.enums import Timeframe
from quantplatform.research import m40
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m37 import TURNOVER_REDUCTION_FOR_PRIMARY, Mechanism
from quantplatform.research.m37_definitions import OVERRIDES
from quantplatform.research.m39 import VARIANTS_M39
from quantplatform.research.m40 import (
    ABLATED,
    CLEAR_IMPROVEMENT,
    MATERIAL_DRAWDOWN_INCREASE,
    STUDIED,
    STUDIED_KEYS,
    TIMEFRAME,
    Arm,
    CausalCheck,
    Reason,
    Side,
    causal,
    establishes_cause,
)


def _side(arm: str, **over: object) -> Side:
    """Return one arm whose figures make the ablation look causal, overridden per test."""
    better = arm == Arm.NO_TIME_STOP
    fields: dict[str, object] = {
        "arm": arm,
        "key": "MC1",
        "annual": Decimal("0.40") if better else Decimal("0.20"),
        "max_drawdown": Decimal("0.36"),
        "calmar_ratio": Decimal("1.11") if better else Decimal("0.56"),
        "profit_factor": Decimal("1.40") if better else Decimal("1.20"),
        "trades": 600 if better else 1000,
        "forced_exits": 300 if better else 700,
        "time_stop_exits": 0 if better else 371,
        "turnover": Decimal(200) if better else Decimal(350),
        "fees": Decimal(15000) if better else Decimal(25000),
        "held_share_of_wanted": Decimal("0.90") if better else Decimal("0.70"),
        "out_of_sample_return": Decimal("0.40") if better else Decimal("0.30"),
        "annual_at_double_cost": Decimal("0.30") if better else Decimal("0.18"),
        "annual_at_triple_cost": Decimal("0.22") if better else Decimal("0.11"),
        "years_positive_share": Decimal("0.60") if better else Decimal("0.40"),
        "single_year_share": Decimal("0.40") if better else Decimal("0.45"),
    }
    return Side.model_validate({**fields, **over})


def _check(**ablated_over: object) -> CausalCheck:
    """Return a causal check that passes, with the ablated arm overridden per test."""
    return CausalCheck(
        key="MC1",
        full=_side(Arm.FULL),
        ablated=_side(Arm.NO_TIME_STOP, **ablated_over),
    )


class TestItRefusesAPureExposureEffect:
    """The failure mode this milestone is most likely to hit."""

    def test_annual_return_rising_alone_does_not_establish_cause(self) -> None:
        # Return up, every risk-adjusted and robustness figure unchanged: exactly what simply
        # removing a constraint produces.
        flat = _check(
            calmar_ratio=Decimal("0.56"),
            out_of_sample_return=Decimal("0.30"),
            annual_at_double_cost=Decimal("0.18"),
            annual_at_triple_cost=Decimal("0.11"),
            held_share_of_wanted=Decimal("0.70"),
            forced_exits=700,
        )
        ok, failed = causal(flat)
        assert not ok
        assert Reason.CALMAR_NOT_CLEARLY_BETTER in failed
        assert Reason.OUT_OF_SAMPLE_NOT_BETTER in failed
        assert Reason.STRESS_NOT_BETTER in failed
        assert Reason.PREMATURE_CLOSES_NOT_REDUCED in failed

    def test_return_bought_with_drawdown_does_not_establish_cause(self) -> None:
        # Twice the return and twice the drawdown is not the time stop being the problem.
        ok, failed = causal(_check(max_drawdown=Decimal("0.72"), calmar_ratio=Decimal("0.56")))
        assert not ok
        assert Reason.DRAWDOWN_MATERIALLY_WORSE in failed
        assert Reason.CALMAR_NOT_CLEARLY_BETTER in failed

    def test_a_sound_ablation_does_establish_cause(self) -> None:
        ok, failed = causal(_check())
        assert ok
        assert failed == ()


class TestEachConditionFailsOnItsOwn:
    """A condition nothing can trip is not a condition."""

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            # A quarter better is the bar; just under it is not "clearly".
            ("annual", Decimal("0.24"), Reason.CAGR_NOT_CLEARLY_BETTER),
            ("calmar_ratio", Decimal("0.69"), Reason.CALMAR_NOT_CLEARLY_BETTER),
            ("max_drawdown", Decimal("0.42"), Reason.DRAWDOWN_MATERIALLY_WORSE),
            ("out_of_sample_return", Decimal("0.30"), Reason.OUT_OF_SAMPLE_NOT_BETTER),
            ("annual_at_double_cost", Decimal("0.18"), Reason.STRESS_NOT_BETTER),
            ("annual_at_triple_cost", Decimal("0.11"), Reason.STRESS_NOT_BETTER),
            ("forced_exits", 700, Reason.PREMATURE_CLOSES_NOT_REDUCED),
            ("held_share_of_wanted", Decimal("0.70"), Reason.PREMATURE_CLOSES_NOT_REDUCED),
        ],
    )
    def test_one_measure_fails_one_condition(
        self, field: str, value: object, expected: Reason
    ) -> None:
        override: dict[str, object] = {field: value}
        ok, failed = causal(_check(**override))
        assert not ok
        assert expected in failed

    def test_exactly_a_quarter_better_clears_the_bar(self) -> None:
        # The threshold is inclusive, so the boundary is a pass rather than a coin toss.
        ok, _ = causal(_check(annual=Decimal("0.25"), calmar_ratio=Decimal("0.70")))
        assert ok

    def test_exactly_five_points_of_extra_drawdown_is_tolerated(self) -> None:
        ok, failed = causal(_check(max_drawdown=Decimal("0.41")))
        assert Reason.DRAWDOWN_MATERIALLY_WORSE not in failed
        assert ok

    def test_a_drawdown_that_improves_is_never_a_failure(self) -> None:
        ok, failed = causal(_check(max_drawdown=Decimal("0.20")))
        assert Reason.DRAWDOWN_MATERIALLY_WORSE not in failed
        assert ok

    def test_a_missing_figure_cannot_pass_by_default(self) -> None:
        for field, expected in (
            ("annual", Reason.CAGR_NOT_CLEARLY_BETTER),
            ("calmar_ratio", Reason.CALMAR_NOT_CLEARLY_BETTER),
            ("out_of_sample_return", Reason.OUT_OF_SAMPLE_NOT_BETTER),
            ("annual_at_double_cost", Reason.STRESS_NOT_BETTER),
            ("held_share_of_wanted", Reason.PREMATURE_CLOSES_NOT_REDUCED),
        ):
            override: dict[str, object] = {field: None}
            ok, failed = causal(_check(**override))
            assert not ok
            assert expected in failed, field

    def test_a_full_arm_that_lost_money_cannot_show_a_relative_gain(self) -> None:
        # A ratio against a negative baseline is not a gain, so it cannot establish cause.
        losing = CausalCheck(
            key="MC1",
            full=_side(Arm.FULL, annual=Decimal("-0.10"), calmar_ratio=Decimal("-0.30")),
            ablated=_side(Arm.NO_TIME_STOP),
        )
        assert losing.relative_cagr_gain is None
        assert losing.relative_calmar_gain is None
        ok, failed = causal(losing)
        assert not ok
        assert Reason.CAGR_NOT_CLEARLY_BETTER in failed


class TestCauseMustHoldAcrossBothVariants:
    """One rule improving is an interaction with that rule, not a general finding."""

    def test_both_studied_variants_must_pass(self) -> None:
        good = _check()
        bad = _check(calmar_ratio=Decimal("0.56"))
        assert establishes_cause((good, good))
        assert not establishes_cause((good, bad))

    def test_a_single_variant_is_not_enough_even_when_it_passes(self) -> None:
        assert not establishes_cause((_check(),))

    def test_no_checks_establishes_nothing(self) -> None:
        assert not establishes_cause(())


class TestScopeAndProvenance:
    """What M40 studies, and where its numbers come from."""

    def test_it_studies_m39s_two_most_compatible_variants(self) -> None:
        assert STUDIED_KEYS == ("MC1", "RT1")
        assert {v.key for v in STUDIED} == set(STUDIED_KEYS)

    def test_the_variants_come_from_m39_by_reference(self) -> None:
        # Identity, not equality: a copy could drift from M39's declaration.
        for variant in STUDIED:
            assert any(variant is declared for declared in VARIANTS_M39)

    def test_exactly_one_mechanism_is_ablated_and_it_is_m37s(self) -> None:
        assert ABLATED is Mechanism.TIME_STOP
        assert OVERRIDES[ABLATED] == {"max_holding_bars": None}
        assert len(OVERRIDES[ABLATED]) == 1

    def test_no_replacement_duration_is_declared_anywhere_in_m40(self) -> None:
        # Phase 1 must establish causality before any limit is proposed. A module that already
        # held 14, 21 or 30 would have begun choosing one.
        values = {
            getattr(m40, name)
            for name in dir(m40)
            if not name.startswith("_") and isinstance(getattr(m40, name), int | Decimal)
        }
        assert not values & {14, 21, 30, Decimal(14), Decimal(21), Decimal(30)}

    def test_the_clear_improvement_bar_is_inherited_not_chosen(self) -> None:
        assert CLEAR_IMPROVEMENT is TURNOVER_REDUCTION_FOR_PRIMARY
        assert Decimal("0.25") == CLEAR_IMPROVEMENT

    def test_the_drawdown_tolerance_is_in_points_and_below_the_cap(self) -> None:
        assert Decimal("0.05") == MATERIAL_DRAWDOWN_INCREASE
        assert MATERIAL_DRAWDOWN_INCREASE < SCREEN_MAX_DRAWDOWN

    def test_it_runs_at_one_day_only(self) -> None:
        assert TIMEFRAME is Timeframe.D1


class TestTheDeclarationIsFrozen:
    """A declaration that could be edited after the fact would not be one."""

    def test_a_side_cannot_be_mutated(self) -> None:
        with pytest.raises(ValidationError):
            _side(Arm.FULL).annual = Decimal("1")  # type: ignore[misc]

    def test_a_side_rejects_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            Side.model_validate({"arm": Arm.FULL, "key": "MC1", "surprise": 1})
