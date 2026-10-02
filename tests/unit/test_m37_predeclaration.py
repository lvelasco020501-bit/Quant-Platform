"""M37's pre-declaration must say what it claims to say, and the structural facts must hold.

The three facts M37 rests on were read out of the risk layer rather than assumed, so they are
asserted here against the real configuration: the loss-streak breaker was already off, the
initial stop cannot be ablated alone, and re-entry has no switch at all. If any of them stops
being true, the ablation's shape is wrong and these tests say so before a result is published.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantplatform.core.enums import Timeframe
from quantplatform.research.latch_policy import risk_configuration_for
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m22 import _base
from quantplatform.research.m37 import (
    ABLATIONS,
    ALREADY_INACTIVE,
    ASSETS_M37,
    CAGR_GAP_RECOVERY_FOR_PRIMARY,
    COUPLED_TO_SIZING,
    EDGE_CONSERVATION_FOR_ALTERNATIVE,
    MAX_PRIMARY_MECHANISMS,
    NOT_ABLATABLE,
    TURNOVER_REDUCTION_FOR_PRIMARY,
    UNIVERSE_M37,
    Ablation,
    Alternative,
    Mechanism,
    MechanismRow,
    Rejection,
    is_primary,
    primary_causes,
    survives,
)
from quantplatform.risk.config import RiskConfiguration


@pytest.fixture
def baseline() -> RiskConfiguration:
    """Return the exact Risk V2 configuration M36 ran at 4H."""
    return risk_configuration_for(REFERENCE, risk_for_timeframe(_base().risk, Timeframe.H4))


def _row(key: str, **over: object) -> MechanismRow:
    """Return a row with neutral defaults, overridden per test."""
    fields: dict[str, object] = {
        "key": key,
        "annual": Decimal("0.09"),
        "max_drawdown": Decimal("0.46"),
        "calmar_ratio": Decimal("0.19"),
        "turnover": Decimal(1000),
        "fees": Decimal(30000),
        "re_entries": 10,
        "stops": 50,
        "out_of_sample_return": Decimal("0.3"),
        "annual_at_double_cost": Decimal("0.01"),
        "annual_at_triple_cost": Decimal("0.01"),
    }
    return MechanismRow.model_validate({**fields, **over})


class TestStructuralFacts:
    """The three facts the ablation's shape depends on, asserted against the real config."""

    def test_the_loss_streak_breaker_was_already_off_in_the_m36_configuration(
        self, baseline: RiskConfiguration
    ) -> None:
        assert baseline.max_consecutive_losses is None
        assert Mechanism.LOSS_STREAK_BREAKER in ALREADY_INACTIVE

    def test_disabling_the_loss_streak_breaker_reproduces_the_baseline_exactly(
        self, baseline: RiskConfiguration
    ) -> None:
        disabled = RiskConfiguration.model_validate(
            {**baseline.model_dump(), "max_consecutive_losses": None}
        )
        assert disabled == baseline

    def test_the_configuration_refuses_to_remove_the_initial_stop_on_its_own(
        self, baseline: RiskConfiguration
    ) -> None:
        with pytest.raises(ValidationError, match="initial_stop_distance_bps"):
            RiskConfiguration.model_validate(
                {**baseline.model_dump(), "initial_stop_distance_bps": None}
            )
        assert Mechanism.INITIAL_STOP in COUPLED_TO_SIZING

    def test_removing_the_stop_together_with_sizing_is_accepted(
        self, baseline: RiskConfiguration
    ) -> None:
        joint = RiskConfiguration.model_validate(
            {**baseline.model_dump(), "initial_stop_distance_bps": None, "risk_budget": None}
        )
        assert not joint.risk_v2_active

    def test_the_risk_layer_carries_no_cooldown_or_re_entry_control(self) -> None:
        # The only field mentioning an entry at all is the flag that refuses a naked one. There
        # is no cooldown, no re-entry delay and no post-stop state, which is why re-entry can be
        # measured but not switched off.
        names = set(RiskConfiguration.model_fields)
        assert {n for n in names if "cooldown" in n} == set()
        assert {n for n in names if "entry" in n} == {"require_stop_on_entry"}
        assert Mechanism.RE_ENTRY in NOT_ABLATABLE

    def test_every_other_mechanism_is_actually_switched_on_in_the_baseline(
        self, baseline: RiskConfiguration
    ) -> None:
        assert baseline.risk_budget is not None
        assert baseline.initial_stop_distance_bps is not None
        assert baseline.break_even_activation_bps is not None
        assert baseline.trailing_activation_bps is not None
        assert baseline.trailing_distance_bps is not None
        assert baseline.take_profit_distance_bps is not None
        assert baseline.max_holding_bars is not None
        assert baseline.max_daily_loss_pct is not None


class TestVariantSet:
    """The variant set must cover the mechanisms and change exactly one thing at a time."""

    def test_the_baseline_disables_nothing(self) -> None:
        assert ABLATIONS[0].key == "BASE"
        assert ABLATIONS[0].disables == ()

    def test_exactly_one_variant_is_a_control(self) -> None:
        assert [a.key for a in ABLATIONS if a.is_control] == ["H"]

    def test_every_variant_has_a_distinct_key(self) -> None:
        keys = [a.key for a in ABLATIONS]
        assert len(keys) == len(set(keys))

    def test_only_the_coupled_variant_disables_more_than_one_mechanism(self) -> None:
        multi = [a.key for a in ABLATIONS if len(a.disables) > 1]
        assert multi == ["B"]

    def test_the_coupled_variant_disables_the_stop_and_sizing_together(self) -> None:
        joint = next(a for a in ABLATIONS if a.key == "B")
        assert set(joint.disables) == {Mechanism.INITIAL_STOP, Mechanism.SIZING}

    def test_every_ablatable_mechanism_appears_in_some_variant(self) -> None:
        covered = {m for a in ABLATIONS for m in a.disables}
        assert covered == set(Mechanism) - NOT_ABLATABLE

    def test_re_entry_is_disabled_by_no_variant_because_it_has_no_switch(self) -> None:
        assert all(Mechanism.RE_ENTRY not in a.disables for a in ABLATIONS)

    def test_the_universe_is_m16s_six_and_equals_the_breadth(self) -> None:
        assert ASSETS_M37 == ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT")
        assert UNIVERSE_M37 == len(ASSETS_M37) == 6


class TestPrimaryCauseRule:
    """The attribution rule, fixed before any ablation ran."""

    def test_both_thresholds_are_a_quarter(self) -> None:
        assert Decimal("0.25") == TURNOVER_REDUCTION_FOR_PRIMARY
        assert Decimal("0.25") == CAGR_GAP_RECOVERY_FOR_PRIMARY

    def test_an_alternative_is_held_to_a_higher_bar_than_a_diagnosis(self) -> None:
        assert EDGE_CONSERVATION_FOR_ALTERNATIVE > CAGR_GAP_RECOVERY_FOR_PRIMARY

    def test_a_variant_needs_both_conditions_not_either(self) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        signals = Decimal("0.29")
        churn_only = _row("X", annual=Decimal("0.09"), turnover=Decimal(700))
        return_only = _row("Y", annual=Decimal("0.20"), turnover=Decimal(990))
        both = _row("Z", annual=Decimal("0.20"), turnover=Decimal(700))
        assert not is_primary(churn_only, base=base, signals_annual=signals)
        assert not is_primary(return_only, base=base, signals_annual=signals)
        assert is_primary(both, base=base, signals_annual=signals)

    def test_a_variant_with_no_measurable_return_cannot_qualify_by_default(self) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        missing = _row("X", annual=None, turnover=Decimal(100))
        assert not is_primary(missing, base=base, signals_annual=Decimal("0.29"))

    def test_nothing_is_primary_when_the_baseline_already_matches_the_signals(self) -> None:
        base = _row("BASE", annual=Decimal("0.29"), turnover=Decimal(1000))
        candidate = _row("X", annual=Decimal("0.40"), turnover=Decimal(100))
        assert not is_primary(candidate, base=base, signals_annual=Decimal("0.29"))

    def test_at_most_two_mechanisms_are_carried_forward(self) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        rows = (
            base,
            _row("C", annual=Decimal("0.26"), turnover=Decimal(500)),
            _row("D", annual=Decimal("0.24"), turnover=Decimal(500)),
            _row("E", annual=Decimal("0.28"), turnover=Decimal(500)),
        )
        chosen = primary_causes(rows, base=base, signals_annual=Decimal("0.29"))
        assert len(chosen) <= MAX_PRIMARY_MECHANISMS
        assert chosen == ("E", "C")

    def test_the_baseline_and_the_control_are_never_named_as_causes(self) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        rows = (
            base,
            _row("H", annual=Decimal("0.28"), turnover=Decimal(100)),
            _row("F", annual=Decimal("0.27"), turnover=Decimal(100)),
        )
        assert primary_causes(rows, base=base, signals_annual=Decimal("0.29")) == ("F",)


class TestAlternativeCriteria:
    """Phase 2's criteria, each one able to fail on its own."""

    @staticmethod
    def _alt(**over: object) -> Alternative:
        fields: dict[str, object] = {
            "key": "ALT",
            "label": "an alternative",
            "keeps_catastrophic_stop": True,
            "introduces_ratchet": False,
            "row": _row(
                "ALT",
                annual=Decimal("0.25"),
                max_drawdown=Decimal("0.30"),
                calmar_ratio=Decimal("0.83"),
                turnover=Decimal(400),
            ),
        }
        return Alternative.model_validate({**fields, **over})

    def test_a_sound_alternative_survives(self) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        passed, reasons = survives(self._alt(), base=base, signals_annual=Decimal("0.29"))
        assert passed
        assert reasons == ()

    @pytest.mark.parametrize(
        ("override", "expected"),
        [
            ({"keeps_catastrophic_stop": False}, Rejection.NO_LARGE_LOSS_PROTECTION),
            ({"introduces_ratchet": True}, Rejection.INTRODUCES_RATCHET),
        ],
    )
    def test_a_design_property_fails_on_its_own(
        self, override: dict[str, object], expected: Rejection
    ) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        passed, reasons = survives(self._alt(**override), base=base, signals_annual=Decimal("0.29"))
        assert not passed
        assert expected in reasons

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("turnover", Decimal(990), Rejection.TURNOVER_NOT_REDUCED),
            ("out_of_sample_return", Decimal("-0.1"), Rejection.OUT_OF_SAMPLE_NEGATIVE),
            ("annual_at_double_cost", Decimal("-0.01"), Rejection.COST_FRAGILE),
            ("annual_at_triple_cost", Decimal("-0.01"), Rejection.COST_FRAGILE),
            ("max_drawdown", Decimal("0.40"), Rejection.DRAWDOWN),
            ("calmar_ratio", Decimal("0.10"), Rejection.MORE_FRAGILE),
            ("annual", Decimal("0.12"), Rejection.EDGE_NOT_CONSERVED),
        ],
    )
    def test_a_measured_criterion_fails_on_its_own(
        self, field: str, value: Decimal, expected: Rejection
    ) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        sound: dict[str, object] = {
            "annual": Decimal("0.25"),
            "max_drawdown": Decimal("0.30"),
            "calmar_ratio": Decimal("0.83"),
            "turnover": Decimal(400),
        }
        row = _row("ALT", **{**sound, field: value})
        passed, reasons = survives(self._alt(row=row), base=base, signals_annual=Decimal("0.29"))
        assert not passed
        assert expected in reasons

    def test_an_alternative_conserving_exactly_half_the_gap_is_accepted(self) -> None:
        base = _row("BASE", annual=Decimal("0.09"), turnover=Decimal(1000))
        half = Decimal("0.09") + (Decimal("0.29") - Decimal("0.09")) / 2
        row = _row(
            "ALT",
            annual=half,
            max_drawdown=Decimal("0.30"),
            calmar_ratio=Decimal("0.83"),
            turnover=Decimal(400),
        )
        passed, reasons = survives(self._alt(row=row), base=base, signals_annual=Decimal("0.29"))
        assert passed, reasons


class TestDeclaredFrozen:
    """The pre-declaration's records must be frozen, like every domain model in this project."""

    def test_an_ablation_cannot_be_mutated(self) -> None:
        variant = ABLATIONS[1]
        with pytest.raises(ValidationError):
            variant.key = "changed"  # type: ignore[misc]

    def test_a_variant_rejects_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            Ablation.model_validate({"key": "X", "label": "x", "disables": (), "surprise": True})
