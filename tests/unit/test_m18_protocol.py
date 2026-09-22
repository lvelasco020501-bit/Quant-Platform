"""M18's protocol: the scenarios that make the breaker fire, and the gates G has to pass.

The two measures that did not exist before this milestone are the interesting ones. Flapping
is a policy reopening and halting straight back — E did it twenty times under stress. Ratchet
is the reference walking downwards, each restart allowing another fall from a lower level,
which is the one way a moving high-water mark can turn a 10% limit into an unbounded loss.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quantplatform.orchestration.research import load_definition
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.m16 import ASSETS, STRATEGY
from quantplatform.research.m17 import POLICIES, study_definition
from quantplatform.research.m18 import (
    MAX_RATCHET_CHAIN,
    PROBE_DRAWDOWN,
    SCENARIOS,
    STUDIED,
    flapping_share,
    judge_policy,
    ratchet_chains,
    scenario_for,
    studied_policies,
    with_threshold,
)
from quantplatform.research.recovery import risk_configuration_for_recovery
from quantplatform.research.stress import derive_stress_definition

DEPLOYED = Path(__file__).resolve().parents[1] / "fixtures" / "deployed_risk_v2_definition.json"
ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def base() -> ExperimentDefinition:
    return load_definition(DEPLOYED)


def _episode(
    day: int, *, before: str, after: str | None, released: int | None = None
) -> dict[str, Any]:
    return {
        "began": ANCHOR + timedelta(days=day),
        "ended": None if released is None else ANCHOR + timedelta(days=released),
        "equity_at_halt": Decimal(before),
        "equity_at_release": None if after is None else Decimal(after),
        "reference_before": Decimal(before),
        "reference_after": None if after is None else Decimal(after),
    }


# --- The scenarios --------------------------------------------------------------------------


def test_the_ladder_is_two_plausible_cost_scenarios() -> None:
    assert [s.label for s in SCENARIOS] == [
        "S2: fees x3, slippage x5, spread 10bps",
        "S3: fees x5, slippage x10, spread 25bps",
    ]


def test_a_scenario_multiplies_the_costs_and_changes_nothing_else(
    base: ExperimentDefinition,
) -> None:
    definition = study_definition(ASSETS[0], STRATEGY, base=base, policy=POLICIES[1])
    harsh = derive_stress_definition(definition, scenario_for(definition, SCENARIOS[1]))
    original = definition.risk.execution_policy
    assert harsh.risk.execution_policy.fee.basis_points == original.fee.basis_points * 5
    assert harsh.risk.execution_policy.slippage.basis_points == original.slippage.basis_points * 10
    assert harsh.backtest.assumed_spread_basis_points == Decimal(25)
    assert harsh.strategy == definition.strategy, "the strategy never changes"
    assert harsh.dataset == definition.dataset, "the market never changes"
    assert harsh.risk.max_total_drawdown_pct == definition.risk.max_total_drawdown_pct


def test_the_probe_tightens_only_the_drawdown_limit() -> None:
    assert Decimal("0.05") == PROBE_DRAWDOWN
    for policy in studied_policies():
        probed = with_threshold(policy, PROBE_DRAWDOWN)
        assert probed.drawdown_pct == PROBE_DRAWDOWN
        assert probed.recovery is policy.recovery
        assert probed.cooldown == policy.cooldown


def test_the_probe_threshold_is_one_the_risk_configuration_accepts(
    base: ExperimentDefinition,
) -> None:
    probed = with_threshold(next(p for p in studied_policies() if p.key == "G"), PROBE_DRAWDOWN)
    risk = risk_configuration_for_recovery(probed, base.risk)
    assert risk.max_total_drawdown_pct == PROBE_DRAWDOWN


def test_only_the_three_policies_that_can_still_change_are_re_run() -> None:
    assert STUDIED == ("C", "E", "G")
    assert [p.key for p in studied_policies()] == ["C", "E", "G"]


# --- Ratchet ---------------------------------------------------------------------------------


def test_one_halt_that_lowers_the_reference_is_not_a_ratchet() -> None:
    assert ratchet_chains([_episode(0, before="10000", after="9000", released=30)]) == []


def test_consecutive_lower_restarts_are_a_chain_and_their_decline_is_measured() -> None:
    episodes = [
        _episode(0, before="10000", after="9000", released=30),
        _episode(60, before="9000", after="8100", released=90),
        _episode(120, before="8100", after="7290", released=150),
    ]
    chains = ratchet_chains(episodes)
    assert len(chains) == 1
    assert chains[0]["halts"] == 3
    # The limit was 10% a time; chained, the account gave up 27% from where the chain began.
    assert chains[0]["decline"] == (Decimal("10000") - Decimal("7290")) / Decimal("10000")


def test_a_recovery_between_halts_breaks_the_chain() -> None:
    episodes = [
        _episode(0, before="10000", after="9000", released=30),
        _episode(60, before="11000", after="9900", released=90),
    ]
    assert ratchet_chains(episodes) == []


def test_a_halt_that_never_ended_cannot_be_part_of_a_chain() -> None:
    assert ratchet_chains([_episode(0, before="10000", after=None)]) == []


# --- Flapping --------------------------------------------------------------------------------


def test_reopening_and_halting_again_within_a_week_is_flapping() -> None:
    episodes = [
        _episode(0, before="10000", after="9000", released=30),
        _episode(33, before="9000", after="8100", released=60),
    ]
    assert flapping_share(episodes) == 1


def test_halts_a_season_apart_are_not_flapping() -> None:
    episodes = [
        _episode(0, before="10000", after="9000", released=30),
        _episode(200, before="9000", after="8100", released=230),
    ]
    assert flapping_share(episodes) == 0


def test_a_single_halt_cannot_flap() -> None:
    assert flapping_share([_episode(0, before="10000", after="9000", released=30)]) is None


# --- The gates -------------------------------------------------------------------------------


def _judge(**overrides: object) -> tuple[bool, list[Any]]:
    arguments: dict[str, Any] = {
        "threshold": Decimal("0.10"),
        "worst_drawdown": Decimal("0.12"),
        "halts": 4,
        "effective": 4,
        "flapping": Decimal("0.00"),
        "longest_chain": 1,
        "worst_chain_decline": None,
        "swing": Decimal("0.05"),
    }
    return judge_policy(**{**arguments, **overrides})


def test_a_policy_meeting_every_gate_passes() -> None:
    passed, gates = _judge()
    assert passed
    assert all(gate.passed for gate in gates)


def test_a_drawdown_beyond_twice_the_limit_fails_the_protection_gate() -> None:
    passed, gates = _judge(worst_drawdown=Decimal("0.25"))
    assert not passed
    assert not next(g for g in gates if g.name == "protects").passed


def test_halts_that_do_not_lead_to_trades_fail_the_recovery_gate() -> None:
    passed, gates = _judge(halts=20, effective=1)
    assert not passed
    assert not next(g for g in gates if g.name == "recovers").passed


def test_reopening_straight_back_into_a_halt_fails_the_flapping_gate() -> None:
    passed, gates = _judge(flapping=Decimal("0.80"))
    assert not passed
    assert not next(g for g in gates if g.name == "does not flap").passed


def test_a_long_chain_of_lower_restarts_fails_the_ratchet_gate() -> None:
    passed, gates = _judge(longest_chain=MAX_RATCHET_CHAIN + 1)
    assert not passed
    assert not next(g for g in gates if g.name == "does not ratchet").passed


def test_a_short_chain_that_gives_up_too_much_also_fails_the_ratchet_gate() -> None:
    passed, gates = _judge(longest_chain=2, worst_chain_decline=Decimal("0.30"))
    assert not passed
    assert not next(g for g in gates if g.name == "does not ratchet").passed


def test_a_trade_count_that_moves_with_a_small_cost_change_fails_continuity() -> None:
    passed, gates = _judge(swing=Decimal("0.60"))
    assert not passed
    assert not next(g for g in gates if g.name == "stays continuous").passed


def test_a_policy_that_never_halted_is_not_failed_for_it() -> None:
    passed, gates = _judge(worst_drawdown=None, halts=0, effective=0, flapping=None)
    assert passed
    assert "never halted" in next(g for g in gates if g.name == "recovers").detail
