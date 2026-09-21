"""M17's protocol: five recovery rules, one strategy, six markets, nothing else changed.

The milestone exists to remove a discontinuity, so the tests that matter most are the ones
holding every policy to the *same* strategy, timeframe, threshold and costs — and the one
that pins the choosing rule to safety and continuity rather than to return.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.orchestration.research import load_definition
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.m16 import ASSETS, STRATEGY
from quantplatform.research.m17 import (
    COOLDOWN,
    DRAWDOWN,
    FEE_MULTIPLIERS,
    MAX_BLOCKED_SHARE,
    POLICIES,
    REFERENCES,
    PolicyRow,
    continuity_swing,
    fee_multiplied,
    recommend,
    study_definition,
)
from quantplatform.research.recovery import Recovery

DEPLOYED = Path(__file__).resolve().parents[1] / "fixtures" / "deployed_risk_v2_definition.json"


@pytest.fixture
def base() -> ExperimentDefinition:
    return load_definition(DEPLOYED)


def _row(
    blocked: str = "0.05",
    drawdown: str = "0.10",
    halt_days: int = 30,
    swing: str | None = "0.10",
    reopenings: int = 0,
    effective: int = 0,
) -> dict[str, object]:
    return {
        "blocked": Decimal(blocked),
        "max_drawdown": Decimal(drawdown),
        "longest_halt_days": halt_days,
        "swing": None if swing is None else Decimal(swing),
        "halts": reopenings,
        "reopenings": reopenings,
        "effective": effective,
    }


# --- The policies ---------------------------------------------------------------------------


def test_the_five_policies_are_the_two_references_and_three_recovery_rules() -> None:
    assert [p.key for p in POLICIES] == ["A", "C", "E", "F", "G"]
    by_key = {p.key: p for p in POLICIES}
    assert by_key["A"].streak_limit == 5, "A is production Risk V2, untouched"
    assert by_key["A"].drawdown_pct == Decimal("0.20")
    assert by_key["C"].recovery is Recovery.PERMANENT
    assert by_key["E"].recovery is Recovery.COOLDOWN
    assert by_key["F"].recovery is Recovery.PERIOD
    assert by_key["G"].recovery is Recovery.COOLDOWN_AND_RESTART


def test_every_recovery_rule_shares_one_threshold_and_one_cooldown() -> None:
    # The comparison is about the recovery rule. A policy with a different threshold would
    # answer a different question, and a per-policy cooldown would be a tuning knob.
    candidates = [p for p in POLICIES if p.key in {"C", "E", "F", "G"}]
    assert {p.drawdown_pct for p in candidates} == {DRAWDOWN}
    assert {p.cooldown for p in candidates if p.cooldown is not None} == {COOLDOWN}
    assert timedelta(days=30) == COOLDOWN


def test_only_the_recovery_rule_changes_between_policies(base: ExperimentDefinition) -> None:
    built = {
        p.key: study_definition(ASSETS[0], STRATEGY, base=base, policy=p)
        for p in POLICIES
        if p.key != "A"
    }
    assert len({d.strategy.model_dump_json() for d in built.values()}) == 1
    assert len({d.dataset.model_dump_json() for d in built.values()}) == 1
    assert len({d.risk.execution_policy.model_dump_json() for d in built.values()}) == 1
    # C, E, F and G arm the identical breaker; what differs is the wrapper that governs it.
    assert len({d.risk.model_dump_json() for d in built.values()}) == 1


def test_the_strategy_and_timeframe_are_m16s(base: ExperimentDefinition) -> None:
    definition = study_definition(ASSETS[0], STRATEGY, base=base, policy=POLICIES[1])
    assert definition.strategy.strategy_id == "regime_trend"
    assert definition.strategy.params == STRATEGY.params
    assert definition.dataset.timeframe is Timeframe.H4
    assert definition.risk.initial_stop_distance_bps == 600, "M15's 4h conversion, unchanged"


def test_each_policy_gets_its_own_identity(base: ExperimentDefinition) -> None:
    ids = {
        p.key: study_definition(ASSETS[0], STRATEGY, base=base, policy=p).experiment_id
        for p in POLICIES
    }
    assert len(set(ids.values())) == len(POLICIES), "policies must not share an experiment id"


# --- The continuity probe -------------------------------------------------------------------


def test_the_fee_grid_is_small_nudges_not_a_stress() -> None:
    assert (Decimal("1.00"), Decimal("1.10"), Decimal("1.25")) == FEE_MULTIPLIERS


def test_multiplying_the_fee_changes_the_fee_and_nothing_else(base: ExperimentDefinition) -> None:
    definition = study_definition(ASSETS[0], STRATEGY, base=base, policy=POLICIES[1])
    dearer = fee_multiplied(definition, Decimal("1.25"))
    original = definition.risk.execution_policy
    assert dearer.risk.execution_policy.fee.basis_points == original.fee.basis_points * Decimal(
        "1.25"
    )
    assert dearer.risk.execution_policy.slippage == original.slippage
    assert dearer.strategy == definition.strategy
    assert dearer.dataset == definition.dataset
    assert dearer.experiment_id != definition.experiment_id


def test_the_swing_is_the_share_of_trades_a_small_fee_change_moves() -> None:
    # M16's XRP under the permanent latch: 27 trades at base cost, 146 with fees doubled.
    assert continuity_swing([27, 146, 146]) == Decimal(146 - 27) / Decimal(146)
    assert continuity_swing([100, 100, 100]) == 0
    assert continuity_swing([0, 0, 0]) is None
    assert continuity_swing([]) is None


# --- Choosing, without looking at a return ---------------------------------------------------


def test_a_policy_that_blocks_a_market_too_long_is_not_eligible() -> None:
    blocked = PolicyRow("E", [_row(blocked=str(MAX_BLOCKED_SHARE + Decimal("0.01")))])
    chosen, notes = recommend([blocked], baseline_drawdown=Decimal("0.10"))
    assert chosen is None
    assert any("blocked" in note for note in notes)


def test_a_policy_whose_halt_runs_for_years_is_not_eligible() -> None:
    stuck = PolicyRow("F", [_row(halt_days=400)])
    chosen, _ = recommend([stuck], baseline_drawdown=Decimal("0.10"))
    assert chosen is None


def test_a_policy_that_deepens_the_drawdown_too_far_is_not_eligible() -> None:
    risky = PolicyRow("G", [_row(drawdown="0.20")])
    chosen, notes = recommend([risky], baseline_drawdown=Decimal("0.10"))
    assert chosen is None
    assert any("drawdown" in note for note in notes)


def test_a_policy_that_reopens_without_being_able_to_trade_is_not_eligible() -> None:
    # Policy E under XRP's harshest stress: twenty halts ended, none of them followed by a
    # trade, and the run finished on exactly the permanent latch's result. Every other gate
    # passes it — short halts, little time blocked, the same drawdown — so this is the gate
    # that has to catch it.
    nominal = PolicyRow("E", [_row(reopenings=20, effective=0)])
    chosen, notes = recommend([nominal], baseline_drawdown=Decimal("0.10"))
    assert chosen is None
    assert any("without being able to trade" in note for note in notes)


def test_a_policy_whose_reopenings_actually_trade_is_eligible() -> None:
    real = PolicyRow("G", [_row(reopenings=4, effective=4)])
    chosen, _ = recommend([real], baseline_drawdown=Decimal("0.10"))
    assert chosen == "G"


def test_a_policy_that_never_halted_is_not_judged_on_reopenings() -> None:
    quiet = PolicyRow("G", [_row(reopenings=0, effective=0)])
    assert quiet.effective_reopenings is None
    chosen, _ = recommend([quiet], baseline_drawdown=Decimal("0.10"))
    assert chosen == "G"


def test_among_eligible_policies_the_most_continuous_one_wins() -> None:
    jumpy = PolicyRow("E", [_row(swing="0.60")])
    steady = PolicyRow("G", [_row(swing="0.05")])
    chosen, _ = recommend([jumpy, steady], baseline_drawdown=Decimal("0.10"))
    assert chosen == "G"


def test_a_tie_on_continuity_breaks_on_time_blocked_never_on_return() -> None:
    busy = PolicyRow("E", [_row(blocked="0.20", swing="0.10")])
    free = PolicyRow("G", [_row(blocked="0.02", swing="0.10")])
    chosen, _ = recommend([busy, free], baseline_drawdown=Decimal("0.10"))
    assert chosen == "G"


def test_the_references_are_never_candidates() -> None:
    # F joins A and C as a reference: a quarterly reference cannot see an all-time drawdown,
    # so F never halted XRP despite its 10.14% fall — and a breaker that never fires scores
    # perfectly on continuity and on time blocked, which is the degenerate optimum of both.
    assert {"A", "C", "F"} == REFERENCES
    chosen, notes = recommend(
        [PolicyRow(key, [_row(swing="0.00")]) for key in ("A", "C", "F")],
        baseline_drawdown=Decimal("0.10"),
    )
    assert chosen is None
    assert all("reference" in note for note in notes)


def test_a_reference_cannot_win_even_with_perfect_numbers() -> None:
    perfect = PolicyRow("F", [_row(swing="0.00", blocked="0.00")])
    worse = PolicyRow("G", [_row(swing="0.30", blocked="0.10", reopenings=2, effective=2)])
    chosen, _ = recommend([perfect, worse], baseline_drawdown=Decimal("0.10"))
    assert chosen == "G"
