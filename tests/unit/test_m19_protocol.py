"""M19's protocol: a local reset under a global loss budget, and the gates it has to pass.

The point of the milestone is that the two earlier failures are opposites — C protects by
never trading again, G trades again by letting the reference walk down — and that a budget
measured from the original high-water mark fixes the second without reintroducing the first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from quantplatform.research.m17 import POLICIES
from quantplatform.research.m19 import (
    GLOBAL_CAP,
    POLICIES_M19,
    RESET_ALLOWANCE,
    Observed,
    judge_m19,
    local_overshoot,
)
from quantplatform.research.recovery import Recovery

ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)


def _episode(*, before: str, at: str) -> dict[str, Any]:
    return {
        "began": ANCHOR,
        "ended": ANCHOR + timedelta(days=30),
        "equity_at_halt": Decimal(at),
        "equity_at_release": Decimal(at),
        "reference_before": Decimal(before),
        "reference_after": Decimal(at),
    }


# --- The policies ---------------------------------------------------------------------------


def test_the_three_policies_are_g_with_a_budget_bolted_on() -> None:
    g = next(p for p in POLICIES if p.key == "G")
    assert [p.key for p in POLICIES_M19] == ["H", "I", "J"]
    for policy in POLICIES_M19:
        assert policy.drawdown_pct == g.drawdown_pct, "the local limit is G's"
        assert policy.cooldown == g.cooldown, "the cooldown is G's"
        assert policy.recovery is Recovery.COOLDOWN_AND_RESTART


def test_each_policy_carries_exactly_the_budget_it_is_named_for() -> None:
    by_key = {p.key: p for p in POLICIES_M19}
    assert by_key["H"].global_drawdown_cap == GLOBAL_CAP
    assert by_key["H"].max_resets_per_year is None
    assert by_key["I"].global_drawdown_cap is None
    assert by_key["I"].max_resets_per_year == RESET_ALLOWANCE
    assert by_key["J"].global_drawdown_cap == GLOBAL_CAP
    assert by_key["J"].max_resets_per_year == RESET_ALLOWANCE


def test_the_global_cap_is_productions_own_total_drawdown_limit() -> None:
    # Not a number invented here: policy A, which is deployed Risk V2, latches at 20%.
    production = next(p for p in POLICIES if p.key == "A")
    assert production.drawdown_pct == GLOBAL_CAP


def test_the_cap_is_wider_than_the_local_limit_it_governs() -> None:
    for policy in POLICIES_M19:
        if policy.global_drawdown_cap is not None:
            assert policy.drawdown_pct is not None
            assert policy.global_drawdown_cap > policy.drawdown_pct


# --- How deep a single halt let things run --------------------------------------------------


def test_the_local_overshoot_is_the_deepest_single_halt() -> None:
    episodes = [
        _episode(before="10000", at="8900"),  # 11%
        _episode(before="8900", at="8100"),  # ~9%
    ]
    assert local_overshoot(episodes) == (Decimal("10000") - Decimal("8900")) / Decimal("10000")


def test_no_halts_means_no_overshoot_to_report() -> None:
    assert local_overshoot([]) is None


# --- The gates -------------------------------------------------------------------------------


def _judge(**overrides: object) -> tuple[bool, list[Any]]:
    arguments: dict[str, Any] = {
        "local_limit": Decimal("0.10"),
        "worst_local": Decimal("0.11"),
        "worst_global": Decimal("0.15"),
        "halts": 6,
        "effective": 6,
        "flapping": Decimal("0.00"),
        "worst_chain_decline": Decimal("0.12"),
        "permanent_halts": 1,
        "permanent_without_cause": 0,
        "swing": Decimal("0.05"),
    }
    return judge_m19(Observed.model_validate({**arguments, **overrides}))


def test_a_policy_meeting_every_gate_passes() -> None:
    passed, gates = _judge()
    assert passed
    assert all(gate.passed for gate in gates)


def test_a_drawdown_past_the_global_budget_fails() -> None:
    # G's failure in M18: 19.16% against a 10% local limit. Under a 20% budget that is still
    # inside the cap, so the gate that catches it is the one measured against the budget.
    passed, gates = _judge(worst_global=Decimal("0.25"))
    assert not passed
    assert not next(g for g in gates if g.name == "keeps the global budget").passed


def test_a_halt_that_acted_too_late_fails_the_prompt_gate() -> None:
    passed, gates = _judge(worst_local=Decimal("0.18"))
    assert not passed
    assert not next(g for g in gates if g.name == "protects promptly").passed


def test_halts_that_never_lead_to_trades_fail_the_recovery_gate() -> None:
    passed, gates = _judge(halts=20, effective=2)
    assert not passed
    assert not next(g for g in gates if g.name == "recovers").passed


def test_a_permanent_shutdown_without_spent_budget_fails() -> None:
    # This is the gate that stops M19 from quietly reinventing C: a market may be shut for
    # good only when the budget was actually spent, never on a single local breach.
    passed, gates = _judge(permanent_halts=3, permanent_without_cause=2)
    assert not passed
    assert not next(g for g in gates if g.name == "shuts down only for cause").passed


def test_a_ratchet_chain_inside_the_budget_is_allowed() -> None:
    passed, gates = _judge(worst_chain_decline=GLOBAL_CAP - Decimal("0.01"))
    assert passed
    assert next(g for g in gates if g.name == "ratchet stays inside budget").passed


def test_a_ratchet_chain_past_the_budget_is_not() -> None:
    passed, _ = _judge(worst_chain_decline=GLOBAL_CAP + Decimal("0.01"))
    assert not passed


def test_flapping_and_instability_each_fail_on_their_own() -> None:
    assert not _judge(flapping=Decimal("0.90"))[0]
    assert not _judge(swing=Decimal("0.80"))[0]


def test_a_policy_that_never_halted_is_not_failed_for_it() -> None:
    passed, _ = _judge(
        worst_local=None,
        worst_global=None,
        halts=0,
        effective=0,
        flapping=None,
        worst_chain_decline=None,
        permanent_halts=0,
    )
    assert passed


def test_a_budget_that_never_bound_is_not_evidence_that_it_works() -> None:
    # Added after the runs, and recorded as such: H, I and J reproduced G in all 48 runs
    # because the cap sat above the worst drawdown (19.16% against 20%) and the reset chains
    # spanned years while the allowance counted a rolling year. Passing gates on behaviour
    # that was never exercised is not validation.
    passed, gates = _judge(budget_bound=False)
    assert not passed
    assert not next(g for g in gates if g.name == "budget actually bound").passed


def test_a_budget_that_bound_clears_that_requirement() -> None:
    passed, gates = _judge(budget_bound=True)
    assert passed
    assert next(g for g in gates if g.name == "budget actually bound").passed


def test_a_policy_without_a_budget_is_not_asked_to_have_bound_one() -> None:
    passed, gates = _judge(budget_bound=None)
    assert passed
    assert "no budget" in next(g for g in gates if g.name == "budget actually bound").detail
