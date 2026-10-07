"""The phase-2 alternative must be a declaration, not a search.

The one thing these tests exist to prevent is the stop distance becoming a tuned number. It may
only ever be the deployed risk budget's own declared maximum, and the function that supplies it
is written so it cannot return anything else -- which is what is asserted here, alongside the
alternative keeping the protection it claims to keep.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantplatform.core.enums import Timeframe
from quantplatform.research.m37 import Mechanism
from quantplatform.research.m37_alternatives import (
    ALTERNATIVES,
    CATASTROPHIC_STOP_SOURCE,
    AlternativeSpec,
    stop_distance_for,
)
from quantplatform.research.m37_definitions import alternative_risk, baseline_risk

TIMEFRAME = Timeframe.H4


class TestTheStopDistanceIsNotChosen:
    """It comes from the configuration under study, and the code enforces that."""

    def test_it_returns_the_budget_maximum_and_nothing_else(self) -> None:
        assert stop_distance_for(Decimal(2000), Decimal(600)) == Decimal(2000)
        assert stop_distance_for(Decimal(1500), Decimal(600)) == Decimal(1500)

    def test_a_maximum_no_wider_than_the_current_stop_is_refused(self) -> None:
        # A catastrophe brake no further out than the trade-managing stop it replaces would be
        # the same mechanism under a new name.
        with pytest.raises(ValueError, match="not wider"):
            stop_distance_for(Decimal(600), Decimal(600))
        with pytest.raises(ValueError, match="not wider"):
            stop_distance_for(Decimal(400), Decimal(600))

    def test_the_provenance_travels_with_the_milestone(self) -> None:
        assert CATASTROPHIC_STOP_SOURCE == "risk_budget.max_stop_distance_bps"

    def test_the_applied_distance_equals_the_deployed_budget_maximum(self) -> None:
        base = baseline_risk(TIMEFRAME)
        assert base.risk_budget is not None
        applied = alternative_risk(ALTERNATIVES[0], TIMEFRAME)
        assert applied.initial_stop_distance_bps == base.risk_budget.max_stop_distance_bps


class TestTheAlternativeKeepsWhatItClaims:
    """Design properties are declared, and the built configuration must match them."""

    def test_there_is_exactly_one_alternative(self) -> None:
        # Not a family, and no second distance to fall back on.
        assert len(ALTERNATIVES) == 1

    def test_it_keeps_a_stop_and_says_so(self) -> None:
        spec = ALTERNATIVES[0]
        assert spec.keeps_catastrophic_stop
        assert alternative_risk(spec, TIMEFRAME).initial_stop_distance_bps is not None

    def test_it_keeps_risk_based_sizing(self) -> None:
        # Phase 1 showed that dropping the budget is what crippled variant A.
        applied = alternative_risk(ALTERNATIVES[0], TIMEFRAME)
        assert applied.risk_v2_active
        assert applied.stop_required

    def test_it_removes_every_stop_modification_and_no_stop(self) -> None:
        spec = ALTERNATIVES[0]
        assert set(spec.removes) == {
            Mechanism.BREAK_EVEN,
            Mechanism.TRAILING_STOP,
            Mechanism.TAKE_PROFIT,
            Mechanism.TIME_STOP,
        }
        assert Mechanism.INITIAL_STOP not in spec.removes
        assert Mechanism.SIZING not in spec.removes

    def test_it_claims_no_ratchet_and_the_configuration_has_none(self) -> None:
        spec = ALTERNATIVES[0]
        assert not spec.introduces_ratchet
        applied = alternative_risk(spec, TIMEFRAME)
        # A trailing stop never retreats, which is a ratchet by construction; it is gone. The
        # drawdown latch is the other, and it was off at baseline and stays off.
        assert applied.trailing_activation_bps is None
        assert applied.trailing_distance_bps is None
        assert not applied.latch_total_drawdown

    def test_it_changes_exactly_the_fields_its_declaration_implies(self) -> None:
        base = baseline_risk(TIMEFRAME).model_dump()
        applied = alternative_risk(ALTERNATIVES[0], TIMEFRAME).model_dump()
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
        applied = alternative_risk(ALTERNATIVES[0], TIMEFRAME)
        assert applied.max_total_drawdown_pct == base.max_total_drawdown_pct
        assert applied.max_daily_drawdown_pct == base.max_daily_drawdown_pct
        assert applied.max_daily_loss_pct == base.max_daily_loss_pct

    def test_the_wider_stop_funds_a_proportionally_smaller_position(self) -> None:
        # Declared in advance as the alternative's cost, not discovered afterwards.
        base = baseline_risk(TIMEFRAME)
        applied = alternative_risk(ALTERNATIVES[0], TIMEFRAME)
        assert base.risk_budget is not None
        assert base.initial_stop_distance_bps is not None
        assert applied.initial_stop_distance_bps is not None
        before = base.risk_budget.risk_per_trade_pct / (base.initial_stop_distance_bps / 10000)
        after = base.risk_budget.risk_per_trade_pct / (applied.initial_stop_distance_bps / 10000)
        assert after < before
        assert after == Decimal("0.05")


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
                    "widen_stop_to_budget_maximum": True,
                    "keeps_catastrophic_stop": True,
                    "introduces_ratchet": False,
                    "surprise": 1,
                }
            )
