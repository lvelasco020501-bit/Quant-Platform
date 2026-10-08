"""M39's declaration must hold its own rules: inherited thresholds, no new parameters, 1D only.

The milestone's whole claim is that a candidate is judged through Risk V2 from its first run and
that the signal basis is a diagnostic. These tests pin both: every gate is an inherited constant
except the one M39 owns, the compatibility ratio can fail on its own, and a ratio against a
losing signal basis is undefined rather than a pass.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantplatform.core.enums import Timeframe
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN, SCREEN_MIN_TRADES
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_ASSETS_POSITIVE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
    MIN_YEARS_POSITIVE_SHARE,
)
from quantplatform.research.m30 import ASSETS_M30, MAX_SINGLE_ASSET_SHARE
from quantplatform.research.m33 import ENTRY_LOOKBACK, EXIT_LOOKBACK, LONG_WINDOW, SHORT_WINDOW
from quantplatform.research.m39 import (
    ASSETS_M39,
    MIN_COMPATIBILITY_RATIO,
    PREDICTED_WORST_COMPATIBILITY,
    TIMEFRAME,
    VARIANTS_M39,
    Family,
    Gate,
    Measured,
    Variant,
    compatibility_ratio,
    survives,
)
from quantplatform.strategies.research import build_research_registry


def _measured(**over: object) -> Measured:
    """Return a variant that clears every gate, overridden per test."""
    fields: dict[str, object] = {
        "key": "PB1",
        "annual": Decimal("0.25"),
        "max_drawdown": Decimal("0.30"),
        "calmar_ratio": Decimal("0.83"),
        "profit_factor": Decimal("1.40"),
        "trades": 120,
        "turnover": Decimal(50),
        "fees": Decimal(3000),
        "forced_exits": 20,
        "re_entries": 5,
        "held_share_of_wanted": Decimal("0.85"),
        "out_of_sample_return": Decimal("0.30"),
        "annual_at_double_cost": Decimal("0.18"),
        "annual_at_triple_cost": Decimal("0.11"),
        "assets_positive": 5,
        "top_asset_share": Decimal("0.30"),
        "single_year_share": Decimal("0.35"),
        "years_positive_share": Decimal("0.70"),
        "signal_annual": Decimal("0.40"),
    }
    return Measured.model_validate({**fields, **over})


class TestTheDeclarationSaysWhatItClaims:
    """Scope, universe and parameter provenance."""

    def test_it_runs_on_one_day_bars_only(self) -> None:
        assert TIMEFRAME is Timeframe.D1

    def test_the_universe_is_m30s_six_taken_by_reference(self) -> None:
        assert ASSETS_M39 is ASSETS_M30
        assert len(ASSETS_M39) == 6

    def test_there_are_three_families_with_two_variants_each(self) -> None:
        assert len(VARIANTS_M39) == 6
        for family in Family:
            assert len([v for v in VARIANTS_M39 if v.family is family]) == 2

    def test_exactly_one_variant_per_family_is_the_doubled_neighbour(self) -> None:
        for family in Family:
            doubled = [v for v in VARIANTS_M39 if v.family is family and v.doubled]
            assert len(doubled) == 1

    def test_every_window_comes_from_m33s_one_day_scale(self) -> None:
        # The only 1D convention this project has. Doubling is M22's neighbour convention.
        allowed = {
            str(SHORT_WINDOW),
            str(SHORT_WINDOW * 2),
            str(LONG_WINDOW),
            str(ENTRY_LOOKBACK),
            str(ENTRY_LOOKBACK * 2),
            str(EXIT_LOOKBACK),
            str(EXIT_LOOKBACK * 2),
        }
        for variant in VARIANTS_M39:
            for _, value in variant.params:
                assert value in allowed, (variant.key, value)

    def test_every_variant_has_a_distinct_key(self) -> None:
        keys = [v.key for v in VARIANTS_M39]
        assert len(keys) == len(set(keys))

    def test_every_variant_names_a_strategy_the_registry_can_build(self) -> None:
        registry = build_research_registry()
        for variant in VARIANTS_M39:
            assert registry.create(variant.strategy_id, dict(variant.params)) is not None

    def test_the_same_parameters_run_on_every_market(self) -> None:
        # Nothing in a variant names an asset, which is what makes per-asset tuning
        # unrepresentable rather than merely discouraged.
        for variant in VARIANTS_M39:
            names = {name for name, _ in variant.params}
            assert not names & {"symbol", "market", "asset"}

    def test_the_control_is_declared_before_any_run(self) -> None:
        assert PREDICTED_WORST_COMPATIBILITY is Family.CONFIRMED


class TestThresholdsAreInherited:
    """M39 owns exactly one number."""

    def test_every_other_threshold_comes_from_an_earlier_milestone(self) -> None:
        assert SCREEN_MIN_TRADES == 30
        assert Decimal("0.35") == SCREEN_MAX_DRAWDOWN
        assert Decimal("0.50") == MIN_CALMAR
        assert Decimal("1.00") == MIN_NEIGHBOUR_PROFIT_FACTOR
        assert COST_STRESS_MULTIPLIERS == (2, 3)
        assert MIN_ASSETS_POSITIVE == 3
        assert Decimal("0.60") == MAX_SINGLE_ASSET_SHARE
        assert Decimal("0.50") == MAX_SINGLE_YEAR_SHARE
        assert Decimal("0.60") == MIN_YEARS_POSITIVE_SHARE

    def test_the_compatibility_floor_sits_between_what_failed_and_what_did_not(self) -> None:
        # M36 kept 31% of its signal CAGR and was rejected; M37's alternative kept 67% and was
        # not. Half lies between them, and was fixed before any M39 candidate ran.
        assert Decimal("0.50") == MIN_COMPATIBILITY_RATIO
        assert Decimal("0.31") < MIN_COMPATIBILITY_RATIO < Decimal("0.67")


class TestTheSignalBasisIsADiagnostic:
    """It is measured and reported, but it may not carry a pass on its own."""

    def test_the_ratio_is_risk_managed_over_signal(self) -> None:
        assert compatibility_ratio(
            _measured(annual=Decimal("0.20"), signal_annual=Decimal("0.40"))
        ) == Decimal("0.5")

    def test_a_ratio_against_a_losing_signal_basis_is_undefined(self) -> None:
        # Losing less than the unmanaged form is not keeping an edge.
        assert compatibility_ratio(_measured(signal_annual=Decimal("-0.10"))) is None
        assert compatibility_ratio(_measured(signal_annual=Decimal(0))) is None

    def test_an_undefined_ratio_does_not_fail_the_compatibility_gate(self) -> None:
        # Such a candidate is judged on its absolute gates alone, not failed by default.
        ok, failed = survives(_measured(signal_annual=Decimal("-0.10")))
        assert Gate.RISK_DESTROYS_EDGE not in failed
        assert ok

    def test_a_strong_signal_basis_cannot_rescue_a_weak_risk_managed_result(self) -> None:
        # The lesson of M30 through M38, as an assertion.
        ok, failed = survives(_measured(annual=Decimal("-0.05"), signal_annual=Decimal("0.80")))
        assert not ok
        assert Gate.NEGATIVE in failed

    def test_keeping_too_little_of_the_signal_edge_fails_on_its_own(self) -> None:
        ok, failed = survives(_measured(annual=Decimal("0.10"), signal_annual=Decimal("0.40")))
        assert not ok
        assert failed == (Gate.RISK_DESTROYS_EDGE,)


class TestEachGateCanFailOnItsOwn:
    """A gate nothing can trip is not a gate."""

    def test_a_sound_candidate_passes(self) -> None:
        ok, failed = survives(_measured())
        assert ok
        assert failed == ()

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("trades", 29, Gate.NO_TRADES),
            ("annual", Decimal("-0.01"), Gate.NEGATIVE),
            ("max_drawdown", Decimal("0.36"), Gate.DRAWDOWN),
            ("calmar_ratio", Decimal("0.49"), Gate.LOW_CALMAR),
            ("profit_factor", Decimal("0.99"), Gate.WEAK_PROFIT_FACTOR),
            ("out_of_sample_return", Decimal("-0.01"), Gate.OUT_OF_SAMPLE_NEGATIVE),
            ("annual_at_double_cost", Decimal("-0.01"), Gate.COST_FRAGILE),
            ("annual_at_triple_cost", Decimal("-0.01"), Gate.COST_FRAGILE),
            ("assets_positive", 2, Gate.TOO_FEW_ASSETS_POSITIVE),
            ("top_asset_share", Decimal("0.61"), Gate.ASSET_CONCENTRATION),
            ("single_year_share", Decimal("0.51"), Gate.SINGLE_YEAR_CONCENTRATION),
            ("years_positive_share", Decimal("0.59"), Gate.YEARS_INCONSISTENT),
        ],
    )
    def test_one_measure_fails_one_gate(self, field: str, value: object, expected: Gate) -> None:
        override: dict[str, object] = {field: value}
        ok, failed = survives(_measured(**override))
        assert not ok
        assert expected in failed

    def test_a_missing_calmar_cannot_pass_by_default(self) -> None:
        ok, failed = survives(_measured(calmar_ratio=None))
        assert not ok
        assert Gate.LOW_CALMAR in failed


class TestTheDeclarationIsFrozen:
    """A declaration that could be edited after the fact would not be one."""

    def test_a_variant_cannot_be_mutated(self) -> None:
        with pytest.raises(ValidationError):
            VARIANTS_M39[0].key = "changed"  # type: ignore[misc]

    def test_a_variant_rejects_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            Variant.model_validate(
                {
                    "key": "X",
                    "family": Family.PULLBACK,
                    "strategy_id": "trend_pullback",
                    "params": (),
                    "surprise": 1,
                }
            )

    def test_a_measurement_rejects_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            Measured.model_validate({"key": "X", "surprise": 1})
