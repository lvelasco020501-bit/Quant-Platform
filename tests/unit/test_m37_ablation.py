"""The ablation harness must change only what it says it changes, and record it faithfully.

Three things are checked here. The variant mapping must produce configurations that differ from
the baseline in exactly the declared fields. The recording engine must be a pure observer: the
actions it reports have to be the standard engine's own, unaltered. And the re-entry definition
must count risk churning the account rather than the strategy changing its mind.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research.m37 import ABLATIONS, Ablation, Mechanism
from quantplatform.research.m37_definitions import OVERRIDES, baseline_risk, risk_for
from quantplatform.research.m37_probe import AblationRiskEngine, ExitTally, re_entries
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.engine import StandardRiskEngine

TIMEFRAME = Timeframe.H4


def _changed(left: RiskConfiguration, right: RiskConfiguration) -> set[str]:
    """Return the names of the fields on which two configurations differ."""
    a, b = left.model_dump(), right.model_dump()
    return {name for name in a if a[name] != b[name]}


class TestVariantMapping:
    """Each variant must differ from the baseline in exactly its own mechanism's fields."""

    def test_the_baseline_variant_returns_the_baseline_itself(self) -> None:
        base = baseline_risk(TIMEFRAME)
        variant = next(a for a in ABLATIONS if a.key == "BASE")
        assert risk_for(variant, TIMEFRAME) == base

    def test_the_control_variant_is_indistinguishable_from_the_baseline(self) -> None:
        base = baseline_risk(TIMEFRAME)
        control = next(a for a in ABLATIONS if a.is_control)
        assert risk_for(control, TIMEFRAME) == base
        assert _changed(risk_for(control, TIMEFRAME), base) == set()

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("A", {"risk_budget"}),
            ("B", {"risk_budget", "initial_stop_distance_bps"}),
            ("C", {"break_even_activation_bps"}),
            ("D", {"trailing_activation_bps", "trailing_distance_bps"}),
            ("E", {"take_profit_distance_bps"}),
            ("F", {"max_holding_bars"}),
            (
                "G",
                {
                    "max_total_drawdown_pct",
                    "max_daily_drawdown_pct",
                    "max_daily_loss_pct",
                },
            ),
        ],
    )
    def test_a_variant_touches_only_its_own_fields(self, key: str, expected: set[str]) -> None:
        base = baseline_risk(TIMEFRAME)
        variant = next(a for a in ABLATIONS if a.key == key)
        assert _changed(risk_for(variant, TIMEFRAME), base) == expected

    def test_no_variant_moves_a_threshold_to_another_chosen_number(self) -> None:
        # Every override is None, or the bound of a field that has no None. Nothing is retuned.
        for fields in OVERRIDES.values():
            for name, value in fields.items():
                assert value is None or value == Decimal(1), (name, value)

    def test_re_entry_has_no_override_because_it_has_no_field(self) -> None:
        assert Mechanism.RE_ENTRY not in OVERRIDES

    def test_removing_the_stop_alone_is_refused_rather_than_worked_around(self) -> None:
        solo = Ablation(key="X", label="stop only", disables=(Mechanism.INITIAL_STOP,))
        with pytest.raises(ValueError, match="initial_stop_distance_bps"):
            risk_for(solo, TIMEFRAME)

    def test_dropping_sizing_leaves_the_stop_in_place(self) -> None:
        # Otherwise variant A would silently be variant B and the decomposition would be void.
        sizing_only = risk_for(next(a for a in ABLATIONS if a.key == "A"), TIMEFRAME)
        assert sizing_only.initial_stop_distance_bps is not None
        assert not sizing_only.risk_v2_active


class TestRecordingEngineIsAPureObserver:
    """The probe may watch the standard engine; it may never change what it decides."""

    def test_it_overrides_only_the_open_position_evaluation(self) -> None:
        overridden = {
            name
            for name, value in vars(AblationRiskEngine).items()
            if callable(value) and not name.startswith("_") and name != "tally"
        }
        assert overridden == {"evaluate_open_positions"}

    def test_it_subclasses_the_standard_engine_rather_than_reimplementing_it(self) -> None:
        assert issubclass(AblationRiskEngine, StandardRiskEngine)

    def test_a_fresh_tally_counts_nothing(self) -> None:
        tally = ExitTally()
        assert tally.forced_exits == 0
        assert tally.protective_stops == 0
        assert dict(tally.by_code) == {}
        assert dict(tally.by_stop_kind) == {}

    def test_the_tally_reports_protective_stops_from_the_code_breakdown(self) -> None:
        tally = ExitTally()
        tally.by_code.update({"protective_stop": 3, "take_profit": 1})
        assert tally.protective_stops == 3

    def test_the_engine_exposes_its_configuration_unchanged(self) -> None:
        config = baseline_risk(TIMEFRAME)
        assert AblationRiskEngine(config=config).config == config


class TestReEntryDefinition:
    """A re-entry is risk churning, not the strategy changing its mind."""

    @staticmethod
    def _count(engine: str, signal: str) -> int:
        return re_entries(tuple(c == "1" for c in engine), tuple(c == "1" for c in signal))

    def test_a_gap_the_signal_wanted_throughout_is_a_re_entry(self) -> None:
        assert self._count("1100110", "1111110") == 1

    def test_a_gap_where_the_signal_lapsed_is_not_a_re_entry(self) -> None:
        assert self._count("1100110", "1100110") == 0

    def test_a_gap_where_the_signal_lapsed_only_partly_is_not_a_re_entry(self) -> None:
        assert self._count("1100110", "1110110") == 0

    def test_a_single_unbroken_holding_has_no_re_entries(self) -> None:
        assert self._count("1111111", "1111111") == 0

    def test_never_holding_has_no_re_entries(self) -> None:
        assert self._count("0000000", "1111111") == 0

    def test_several_churned_gaps_are_counted_separately(self) -> None:
        assert self._count("10101010", "11111111") == 3

    def test_adjacent_stretches_with_no_gap_cannot_arise_but_count_nothing(self) -> None:
        # One stretch, because the mask has no false bar between the two holdings.
        assert self._count("11111", "11111") == 0
