"""M19 — reopening a halted market without giving up a global limit on the damage.

M17 and M18 left two failures on the table, and they are opposites:

* **C** (permanent latch) protects by never trading again — markets blocked up to 89.86% of
  their history, 0 of 15 halts ever reopened.
* **G** (local reset) reopens properly — 24 of 25 halts were followed by a trade, no flapping,
  and a quarter more in fees moves its trade count by 0.80% — but the reference walks down.
  Three chains of three consecutive lower restarts turned a 10% limit into a 19.16% drawdown,
  and a 5% limit into 11.17%.

The idea this milestone tests is that both are fixable at once: **the local reference may reset
so the market can trade again, but a global loss budget, measured from the original high-water
mark, must never reset silently.**

======  ====================================================================================
 key     rule
======  ====================================================================================
 H       G, plus a hard cap on the loss from the original peak: 20%
 I       G, plus an allowance of two local resets per rolling year, then the latch is final
 J       both — the cap and the allowance, whichever binds first
======  ====================================================================================

**Where the two numbers come from, and what they are not.** Neither was chosen by looking at a
return, and neither may be.

* **The 20% cap is production's own number.** Risk V2 already refuses to lose more than 20%
  in total; policy A latches there permanently. So the cap says exactly this: a local reset may
  let a market trade again, but the account may never lose more than production already allows.
  Taking the figure from the deployed configuration is the one choice that cannot be accused
  of having been tuned here.
* **The allowance of two resets a year is chosen from M18's failure, not from its profits.**
  Every breach of the limit in M18 came from a chain of *three* restarts; two per rolling year
  is the largest allowance that cannot produce a three-chain inside a year. This is fitted to
  the mechanism's observed failure mode, which is a weaker claim than an a-priori constant, and
  it is recorded as such rather than presented as untouched.

**The gates, fixed before the runs.** Return is not an input to any of them.

=============================  =====================================================________
 gate                           a policy passes when
=============================  =========================================================____
 protects promptly              no single halt let the loss from its own reference run past
                                one and a half times the local limit
 keeps the global budget        no run's drawdown from the original peak passed the 20% cap
 recovers                       at least half its halts were followed by a trade
 does not flap                  at most a quarter of its halts began within a week of a
                                reopening
 ratchet stays inside budget    no chain of lower restarts gave up more than the global cap
 shuts down only for cause      any market left permanently halted had actually spent the
                                budget, rather than being latched on a single local breach
 stays continuous               the trade count moved less than a quarter across fee settings
=============================  =============================================================

If none of H, I or J passes every gate, the answer is NO-GO and nothing goes near production.

**An evidential requirement, added after the runs and disclosed as such.** The gates above ask
how a budget *behaves*; they cannot ask whether it behaved at all. On this evidence every
budget stayed inert — the worst drawdown reached 19.16% against a 20% cap, and the reset
chains were spread over three and four years, so an allowance counted per rolling year never
came close to binding. H, I and J therefore reproduce G bit for bit in all 48 runs, and the
gates pass them on G's behaviour. A mechanism that never acted has not been validated by the
runs in which it did not act, so :func:`judge_m19` now also requires that the budget bound at
least once. This changes no policy's behaviour and no ranking; it changes only whether the
result may be called evidence, and it was added after seeing that the answer was "no".
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.m17 import POLICIES
from quantplatform.research.m18 import (
    FLAPPING_WINDOW,
    MAX_FLAPPING_SHARE,
    MAX_SWING,
    MIN_RECOVERY_SHARE,
)
from quantplatform.research.recovery import Recovery, RecoveryPolicy

__all__ = [
    "GLOBAL_CAP",
    "LOCAL_OVERSHOOT",
    "POLICIES_M19",
    "RESET_ALLOWANCE",
    "Gate",
    "Observed",
    "judge_m19",
    "local_overshoot",
]

GLOBAL_CAP: Final[Decimal] = Decimal("0.20")
"""Production Risk V2's own total-drawdown limit, reused here as the budget no reset may clear."""

RESET_ALLOWANCE: Final[int] = 2
"""Local resets allowed per rolling year. Chosen from M18's three-restart chains, not a return."""

LOCAL_OVERSHOOT: Final[Decimal] = Decimal("1.5")
"""How far past its own local limit a single halt may let the loss run before it is late."""

_G: Final[RecoveryPolicy] = next(p for p in POLICIES if p.key == "G")

POLICIES_M19: Final[tuple[RecoveryPolicy, ...]] = (
    RecoveryPolicy(
        key="H",
        label="local reset under a 20% global cap",
        drawdown_pct=_G.drawdown_pct,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=_G.cooldown,
        global_drawdown_cap=GLOBAL_CAP,
    ),
    RecoveryPolicy(
        key="I",
        label="local reset, two resets per rolling year",
        drawdown_pct=_G.drawdown_pct,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=_G.cooldown,
        max_resets_per_year=RESET_ALLOWANCE,
    ),
    RecoveryPolicy(
        key="J",
        label="local reset under both the cap and the allowance",
        drawdown_pct=_G.drawdown_pct,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=_G.cooldown,
        global_drawdown_cap=GLOBAL_CAP,
        max_resets_per_year=RESET_ALLOWANCE,
    ),
)
"""H, I and J are G with a budget bolted on; the local limit and cooldown are G's, untouched,
so any difference in the results is the budget and nothing else."""


def local_overshoot(episodes: Sequence[dict[str, Any]]) -> Decimal | None:
    """Return the deepest loss any single halt allowed, as a share of its own reference.

    This is the local counterpart of the global budget: it says whether the breaker acted
    when it should have, independently of how far the account fell in total across halts.
    """
    worst: Decimal | None = None
    for episode in episodes:
        reference = episode.get("reference_before")
        equity = episode.get("equity_at_halt")
        if reference is None or equity is None:
            continue
        before, at = Decimal(str(reference)), Decimal(str(equity))
        if before <= 0:
            continue
        fell = (before - at) / before
        worst = fell if worst is None else max(worst, fell)
    return worst


class Gate(DomainModel):
    """One requirement, and whether the evidence met it."""

    name: Text
    passed: bool
    detail: Text


class Observed(DomainModel):
    """Everything the gates are allowed to look at. Note what is absent: any return."""

    local_limit: Decimal
    worst_local: Decimal | None = None
    """Deepest loss a single halt allowed, measured from that halt's own reference."""

    worst_global: Decimal | None = None
    """Deepest loss from the original high-water mark, across every run."""

    halts: int = 0
    effective: int = 0
    flapping: Decimal | None = None
    worst_chain_decline: Decimal | None = None
    permanent_halts: int = 0
    permanent_without_cause: int = 0
    """Markets left halted for good although the budget had not been spent."""

    budget_bound: bool | None = None
    """Whether the budget ever actually bound. ``None`` for a policy that has no budget."""

    swing: Decimal | None = None


def judge_m19(observed: Observed) -> tuple[bool, list[Gate]]:
    """Apply M19's gates in their declared order. Return never enters the decision."""
    local_limit = observed.local_limit
    worst_local = observed.worst_local
    worst_global = observed.worst_global
    halts, effective = observed.halts, observed.effective
    flapping = observed.flapping
    worst_chain_decline = observed.worst_chain_decline
    permanent_halts = observed.permanent_halts
    permanent_without_cause = observed.permanent_without_cause
    swing = observed.swing
    allowance = local_limit * LOCAL_OVERSHOOT
    recovery = Decimal(effective) / Decimal(halts) if halts else None
    gates = [
        Gate(
            name="protects promptly",
            passed=worst_local is None or worst_local <= allowance,
            detail=(
                "never halted"
                if worst_local is None
                else f"deepest single halt {worst_local:.2%} against {allowance:.1%} allowed"
            ),
        ),
        Gate(
            name="keeps the global budget",
            passed=worst_global is None or worst_global <= GLOBAL_CAP,
            detail=(
                "no drawdown recorded"
                if worst_global is None
                else f"worst drawdown from the original peak {worst_global:.2%} "
                f"against a {GLOBAL_CAP:.0%} budget"
            ),
        ),
        Gate(
            name="recovers",
            passed=recovery is None or recovery >= MIN_RECOVERY_SHARE,
            detail=(
                "never halted"
                if recovery is None
                else f"{effective}/{halts} halts were followed by a trade"
            ),
        ),
        Gate(
            name="does not flap",
            passed=flapping is None or flapping <= MAX_FLAPPING_SHARE,
            detail=(
                "fewer than two halts"
                if flapping is None
                else f"{flapping:.0%} of halts began within "
                f"{FLAPPING_WINDOW.days} days of a reopening"
            ),
        ),
        Gate(
            name="ratchet stays inside budget",
            passed=worst_chain_decline is None or worst_chain_decline <= GLOBAL_CAP,
            detail=(
                "no chain of lower restarts"
                if worst_chain_decline is None
                else f"worst chain gave up {worst_chain_decline:.2%}"
            ),
        ),
        Gate(
            name="shuts down only for cause",
            passed=permanent_without_cause == 0,
            detail=(
                f"{permanent_halts} permanent halts, "
                f"{permanent_without_cause} of them without the budget being spent"
            ),
        ),
        Gate(
            name="budget actually bound",
            passed=observed.budget_bound is not False,
            detail=(
                "policy carries no budget"
                if observed.budget_bound is None
                else (
                    "the budget bound at least once"
                    if observed.budget_bound
                    else "the budget never bound: these runs reproduce G exactly, so they say "
                    "nothing about whether a budget works"
                )
            ),
        ),
        Gate(
            name="stays continuous",
            passed=swing is None or swing <= MAX_SWING,
            detail="not measured" if swing is None else f"trade count moved {swing:.2%}",
        ),
    ]
    return all(gate.passed for gate in gates), gates
