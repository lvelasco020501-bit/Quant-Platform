"""The phase-2 alternatives must be declarations, not searches.

The one thing these tests exist to prevent is a stop distance becoming a tuned number. Every
distance must come from a declared rule -- the configuration's own bound, or this project's
doubling convention -- and the function that supplies them has no branch that returns a literal.
ALT1's infeasibility is asserted too, so the record of a failed attempt cannot quietly vanish.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantplatform.core.enums import Timeframe
from quantplatform.research.m37 import Mechanism
from quantplatform.research.m37_alternatives import (
    ALTERNATIVES,
    INFEASIBLE,
    AlternativeSpec,
    StopRule,
    stop_distance_for,
)
from quantplatform.research.m37_definitions import alternative_risk, baseline_risk

TIMEFRAME = Timeframe.H4


def _spec(key: str) -> AlternativeSpec:
    return next(spec for spec in ALTERNATIVES if spec.key == key)


class TestDistancesComeFromRulesNotChoices:
    """Each branch derives its answer; none returns a number someone picked."""

    def test_the_budget_rule_returns_the_budget_maximum(self) -> None:
        assert stop_distance_for(
            StopRule.BUDGET_MAXIMUM, budget_maximum=Decimal(2000), current=Decimal(600)
        ) == Decimal(2000)

    def test_the_doubling_rule_returns_twice_the_current_stop(self) -> None:
        assert stop_distance_for(
            StopRule.DOUBLED, budget_maximum=Decimal(2000), current=Decimal(600)
        ) == Decimal(1200)
        assert stop_distance_for(
            StopRule.DOUBLED, budget_maximum=Decimal(2000), current=Decimal(300)
        ) == Decimal(600)

    def test_a_distance_no_wider_than_the_stop_it_replaces_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not wider"):
            stop_distance_for(
                StopRule.BUDGET_MAXIMUM, budget_maximum=Decimal(600), current=Decimal(600)
            )

    def test_a_doubled_distance_beyond_the_budget_is_refused_not_clamped(self) -> None:
        # Clamping would silently produce a stop the declaration did not describe.
        with pytest.raises(ValueError, match="beyond the budget"):
            stop_distance_for(StopRule.DOUBLED, budget_maximum=Decimal(1500), current=Decimal(900))

    def test_every_alternative_names_a_rule_rather_than_a_value(self) -> None:
        for spec in ALTERNATIVES:
            assert isinstance(spec.stop_rule, StopRule)

    def test_the_doubled_distance_applied_is_twice_the_deployed_stop(self) -> None:
        base = baseline_risk(TIMEFRAME)
        assert base.initial_stop_distance_bps is not None
        applied = alternative_risk(_spec("ALT2"), TIMEFRAME)
        assert applied.initial_stop_distance_bps == base.initial_stop_distance_bps * 2


class TestAlt1sFailureStaysOnTheRecord:
    """An attempt that could not be built is part of the evidence, not an embarrassment."""

    def test_alt1_is_recorded_as_infeasible_with_a_reason(self) -> None:
        assert "ALT1" in INFEASIBLE
        assert "max_stop_distance_bps" in INFEASIBLE["ALT1"]

    def test_alt1_is_still_declared_rather_than_deleted(self) -> None:
        assert _spec("ALT1").stop_rule is StopRule.BUDGET_MAXIMUM

    def test_alt2_is_not_recorded_as_infeasible(self) -> None:
        assert "ALT2" not in INFEASIBLE

    def test_both_alternatives_are_structurally_identical_apart_from_the_distance(self) -> None:
        one, two = _spec("ALT1"), _spec("ALT2")
        assert one.removes == two.removes
        assert one.keeps_catastrophic_stop == two.keeps_catastrophic_stop
        assert one.introduces_ratchet == two.introduces_ratchet
        assert one.stop_rule is not two.stop_rule


class TestTheAlternativeKeepsWhatItClaims:
    """Design properties are declared, and the built configuration must match them."""

    def test_it_keeps_a_stop_and_says_so(self) -> None:
        spec = _spec("ALT2")
        assert spec.keeps_catastrophic_stop
        assert alternative_risk(spec, TIMEFRAME).initial_stop_distance_bps is not None

    def test_it_keeps_risk_based_sizing(self) -> None:
        applied = alternative_risk(_spec("ALT2"), TIMEFRAME)
        assert applied.risk_v2_active
        assert applied.stop_required

    def test_it_removes_every_stop_modification_and_no_stop(self) -> None:
        spec = _spec("ALT2")
        assert set(spec.removes) == {
            Mechanism.BREAK_EVEN,
            Mechanism.TRAILING_STOP,
            Mechanism.TAKE_PROFIT,
            Mechanism.TIME_STOP,
        }
        assert Mechanism.INITIAL_STOP not in spec.removes
        assert Mechanism.SIZING not in spec.removes

    def test_it_claims_no_ratchet_and_the_configuration_has_none(self) -> None:
        spec = _spec("ALT2")
        assert not spec.introduces_ratchet
        applied = alternative_risk(spec, TIMEFRAME)
        # Trailing never retreats and is a ratchet by construction; it is gone. The drawdown
        # latch is the other, and it was off at baseline and stays off.
        assert applied.trailing_activation_bps is None
        assert applied.trailing_distance_bps is None
        assert not applied.latch_total_drawdown

    def test_it_changes_exactly_the_fields_its_declaration_implies(self) -> None:
        base = baseline_risk(TIMEFRAME).model_dump()
        applied = alternative_risk(_spec("ALT2"), TIMEFRAME).model_dump()
        assert {name for name in base if base[name] != applied[name]} == {
            "initial_stop_distance_bps",
            "break_even_activation_bps",
            "trailing_activation_bps",
            "trailing_distance_bps",
            "take_profit_distance_bps",
            "max_holding_bars",
        }

    def test_the_breakers_are_left_exactly_as_deployed(self) -> None:
        base = baseline_risk(TIMEFRAME)
        applied = alternative_risk(_spec("ALT2"), TIMEFRAME)
        assert applied.max_total_drawdown_pct == base.max_total_drawdown_pct
        assert applied.max_daily_drawdown_pct == base.max_daily_drawdown_pct
        assert applied.max_daily_loss_pct == base.max_daily_loss_pct

    def test_the_wider_stop_funds_a_proportionally_smaller_position(self) -> None:
        # Declared in advance as the alternative's cost, not discovered afterwards.
        base = baseline_risk(TIMEFRAME)
        applied = alternative_risk(_spec("ALT2"), TIMEFRAME)
        assert base.risk_budget is not None
        assert base.initial_stop_distance_bps is not None
        assert applied.initial_stop_distance_bps is not None
        before = base.risk_budget.risk_per_trade_pct / (base.initial_stop_distance_bps / 10000)
        after = base.risk_budget.risk_per_trade_pct / (applied.initial_stop_distance_bps / 10000)
        # To within Decimal rounding: 1/0.12 and (1/0.06)/2 differ in the 28th digit at the
        # working precision, which is an artifact of the division and not of the relationship.
        assert abs(after - before / 2) < Decimal("1e-20")


class TestTheSpecIsFrozen:
    """A declaration that could be edited after the fact would not be one."""

    def test_an_alternative_cannot_be_mutated(self) -> None:
        with pytest.raises(ValidationError):
            ALTERNATIVES[0].key = "changed"  # type: ignore[misc]

    def test_an_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AlternativeSpec.model_validate(
                {
                    "key": "X",
                    "label": "x",
                    "removes": (),
                    "stop_rule": StopRule.DOUBLED,
                    "keeps_catastrophic_stop": True,
                    "introduces_ratchet": False,
                    "surprise": 1,
                }
            )
