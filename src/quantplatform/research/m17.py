"""M17 — how a market halted by the drawdown breaker gets to trade again.

M16 measured `regime_trend` at 4h across six markets and found a defect in the *risk policy*
rather than the strategy. Under the permanent drawdown latch (policy C), XRP's breaker tripped
once, on 2020-07-29, and that market never traded again: 27 trades, 74% of its history blocked,
minus 3%. The identical run with fees doubled never tripped, traded 146 times, and returned
+42%. A ten-year outcome decided by which side of a threshold the equity passed one afternoon
is not risk control; it is an accident with a long memory.

This milestone changes **only the recovery rule** — when a halted market reopens. The strategy,
its parameters, the timeframe, the datasets, the fees and the slippage are M16's, unchanged.

**The five policies, fixed before any result was looked at.**

======  ======================================================================================
 key     rule
======  ======================================================================================
 A       production Risk V2: five consecutive losses, 20% drawdown, both permanent
 C       M16's research policy: 10% drawdown, permanent. The defect under study
 E       10% drawdown, halt ends after a fixed cooldown, reference peak carried over
 F       10% drawdown, halt ends at the next calendar quarter, reference resets to its opening
 G       10% drawdown, halt ends after the same cooldown, reference resets to the reopening
======  ======================================================================================

**Why these constants, and not others.** The 10% threshold is C's, untouched, so the comparison
isolates the recovery rule instead of mixing in a different limit. The 30-day cooldown comes
from the strategy: its regime filter looks back 72 bars, which at 4h is twelve days, so thirty
days is about two and a half of its own windows — long enough for the regime that caused the
drawdown to have changed, short enough that no market sits out a year. The quarter is the
shortest conventional business period a drawdown limit is assessed over. **No constant here was
chosen by looking at a return, and none may be.**

**How continuity is measured.** The defect is discontinuity, so the test is a *small* cost
change, not a stress: each market is run at fees x1.00, x1.10 and x1.25 with slippage
unchanged. A policy is continuous when a quarter more in fees does not change the number of
trades much; C on XRP moved from 27 to 146 (an 81% swing), which is what a good policy must
not do.

**How the winner is chosen — declared in advance, and never by return.**

1. **Damage** — the worst drawdown across the six markets may not exceed C's worst by more
   than five percentage points.
2. **Predictability** — no market blocked more than 25% of its history, and no single halt
   longer than 180 days.
3. **Continuity** — of what remains, the smallest worst-market trade swing across the fee grid.

Ties break on the lower median blocked time. Return is not an input at any step: a rule that
earns more by risking more would win on return and lose on every question that matters here.

**Amendment, added after XRP's stress runs and before any six-market result existed.** The
three gates above miss the failure they were written to catch. Under the harshest cost
scenario, policy E reopened XRP twenty separate times and still finished with exactly C's
result — 22 trades, minus 6.01% — because a rule that carries the old reference reopens the
market straight back into the same refusal. It flaps instead of recovering, and every gate
above passes it: the halts are short, the blocked share is small, the drawdown is C's.

So a fourth measure joins them, between predictability and continuity: **operability**, the
share of a policy's halts after which the market actually traded again before the next halt.
A reopening that cannot trade is not a recovery, and a policy whose reopenings are mostly
nominal is not eligible however good its other numbers look. It is measured from the stored
trades, not from the halt count, and like everything else here it says nothing about return.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.folds import WindowSpec
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m16 import DATA_END, TIMEFRAME, Asset, symbol_rules_for
from quantplatform.research.recovery import (
    Recovery,
    RecoveryPolicy,
    risk_configuration_for_recovery,
)
from quantplatform.research.sprint import SprintCandidate
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "COOLDOWN",
    "DATASET_SOURCE",
    "DRAWDOWN",
    "FEE_MULTIPLIERS",
    "MAX_BLOCKED_SHARE",
    "MAX_HALT",
    "MIN_EFFECTIVE_REOPENINGS",
    "POLICIES",
    "REFERENCES",
    "PolicyRow",
    "continuity_swing",
    "recommend",
    "study_definition",
]

DATASET_SOURCE: Final[str] = "binance_vision_m16"
"""M16's datasets, unchanged and byte for byte: this milestone re-uses them, it does not rebuild."""

DRAWDOWN: Final[Decimal] = Decimal("0.10")
COOLDOWN: Final[timedelta] = timedelta(days=30)
FEE_MULTIPLIERS: Final[tuple[Decimal, ...]] = (Decimal("1.00"), Decimal("1.10"), Decimal("1.25"))

POLICIES: Final[tuple[RecoveryPolicy, ...]] = (
    RecoveryPolicy(
        key="A",
        label="production Risk V2: streak and drawdown, both permanent",
        drawdown_pct=Decimal("0.20"),
        recovery=Recovery.PERMANENT,
        streak_limit=5,
    ),
    RecoveryPolicy(
        key="C",
        label="drawdown 10%, permanent (M16's policy, the defect)",
        drawdown_pct=DRAWDOWN,
        recovery=Recovery.PERMANENT,
    ),
    RecoveryPolicy(
        key="E",
        label="drawdown 10%, cooldown 30 days, reference carried over",
        drawdown_pct=DRAWDOWN,
        recovery=Recovery.COOLDOWN,
        cooldown=COOLDOWN,
    ),
    RecoveryPolicy(
        key="F",
        label="drawdown 10%, reset each calendar quarter",
        drawdown_pct=DRAWDOWN,
        recovery=Recovery.PERIOD,
    ),
    RecoveryPolicy(
        key="G",
        label="drawdown 10%, cooldown 30 days, new high-water mark",
        drawdown_pct=DRAWDOWN,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=COOLDOWN,
    ),
)

REFERENCES: Final[frozenset[str]] = frozenset({"A", "C", "F"})
"""Policies reported for comparison but never recommended.

A is production and C is the defect under study. F joins them on instruction given before
the six-market results were read, and the results bear the reason out: a quarterly reference
cannot see an all-time drawdown, so F never halted XRP at all despite its 10.14% fall. That
makes F score perfectly on continuity and on time blocked — the degenerate optimum of these
measures is a breaker that never fires, which is not what any of them was meant to reward.
"""

MIN_EFFECTIVE_REOPENINGS: Final[Decimal] = Decimal("0.50")
"""At least half of a policy's halts must be followed by a trade before the next halt."""

MAX_BLOCKED_SHARE: Final[Decimal] = Decimal("0.25")
MAX_HALT: Final[timedelta] = timedelta(days=180)
DAMAGE_ALLOWANCE: Final[Decimal] = Decimal("0.05")
"""How much worse than C's worst drawdown a recovery rule may be: five percentage points."""


def fee_multiplied(definition: ExperimentDefinition, multiplier: Decimal) -> ExperimentDefinition:
    """Return the same definition with its commission multiplied, and nothing else changed."""
    policy = definition.risk.execution_policy
    fee = policy.fee.model_copy(update={"basis_points": policy.fee.basis_points * multiplier})
    risk = definition.risk.model_copy(
        update={"execution_policy": policy.model_copy(update={"fee": fee})}
    )
    copy = definition.model_copy(
        update={"risk": risk, "name": f"{definition.name}-fee{multiplier.normalize()}"}
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def study_definition(
    asset: Asset,
    candidate: SprintCandidate,
    *,
    base: ExperimentDefinition,
    policy: RecoveryPolicy,
) -> ExperimentDefinition:
    """Return one market's definition under one recovery rule, over its whole history."""
    version = build_research_registry().metadata_for(candidate.strategy_id).version
    span = WindowSpec(start=asset.start, end=DATA_END)
    dataset = base.dataset.model_copy(
        update={
            "symbol": asset.symbol,
            "symbol_rules": symbol_rules_for(asset),
            "timeframe": TIMEFRAME,
            "start": span.start,
            "end": span.end,
            "source": DATASET_SOURCE,
        }
    )
    copy = base.model_copy(
        update={
            "name": f"m17-{asset.raw}-{candidate.strategy_id}-{policy.key}",
            "strategy": StrategySpec(
                strategy_id=candidate.strategy_id,
                strategy_version=version,
                params=candidate.params,
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": TIMEFRAME}),
            "risk": risk_configuration_for_recovery(
                policy, risk_for_timeframe(base.risk, TIMEFRAME)
            ),
            "role": ExperimentRole.IN_SAMPLE,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def continuity_swing(trade_counts: list[int]) -> Decimal | None:
    """Return how much the trade count moved across the fee grid, as a share of the largest.

    Zero is a policy that traded the same number of times however the fees were nudged; one
    is a policy that stopped trading entirely. ``None`` when nothing traded at any setting.
    """
    if not trade_counts or max(trade_counts) == 0:
        return None
    return Decimal(max(trade_counts) - min(trade_counts)) / Decimal(max(trade_counts))


class PolicyRow:
    """One policy's behaviour across every market, as the choosing rule reads it."""

    def __init__(self, key: str, rows: list[dict[str, Any]]) -> None:
        """Summarise one policy from its per-market rows."""
        self.key = key
        self.rows = rows
        self.worst_drawdown = max((Decimal(str(r["max_drawdown"])) for r in rows), default=None)
        self.worst_blocked = max((Decimal(str(r["blocked"])) for r in rows), default=None)
        self.longest_halt = max((r["longest_halt_days"] for r in rows), default=None)
        self.worst_swing = max((r["swing"] for r in rows if r["swing"] is not None), default=None)
        reopenings = [r.get("reopenings", 0) for r in rows]
        self.halts = sum(r.get("halts", 0) for r in rows)
        self.effective_reopenings = (
            Decimal(sum(r.get("effective", 0) for r in rows)) / Decimal(sum(reopenings))
            if sum(reopenings)
            else None
        )
        """Share of halts after which the market traded again; ``None`` when it never halted."""

        blocked = sorted(Decimal(str(r["blocked"])) for r in rows)
        middle = len(blocked) // 2
        self.median_blocked = blocked[middle] if blocked else None


def recommend(
    policies: list[PolicyRow], *, baseline_drawdown: Decimal
) -> tuple[str | None, list[str]]:
    """Return the recommended policy and why each one was or was not eligible.

    Applies the rule declared in this module's docstring, in order, and never looks at a
    return. A policy that fails a gate is reported with the gate it failed.
    """
    notes: list[str] = []
    eligible: list[PolicyRow] = []
    for policy in policies:
        if policy.key in REFERENCES:
            notes.append(f"{policy.key}: reference, not a candidate")
            continue
        failures = []
        if policy.worst_drawdown is not None and (
            policy.worst_drawdown > baseline_drawdown + DAMAGE_ALLOWANCE
        ):
            failures.append(
                f"worst drawdown {policy.worst_drawdown:.2%} exceeds "
                f"{baseline_drawdown + DAMAGE_ALLOWANCE:.2%}"
            )
        if policy.worst_blocked is not None and policy.worst_blocked > MAX_BLOCKED_SHARE:
            failures.append(f"blocked {policy.worst_blocked:.2%} of a market's history")
        if policy.longest_halt is not None and policy.longest_halt > MAX_HALT.days:
            failures.append(f"a halt lasted {policy.longest_halt} days")
        if (
            policy.effective_reopenings is not None
            and policy.effective_reopenings < MIN_EFFECTIVE_REOPENINGS
        ):
            failures.append(
                f"only {policy.effective_reopenings:.0%} of its halts were followed by a trade: "
                "it reopens without being able to trade"
            )
        if failures:
            notes.append(f"{policy.key}: not eligible — {'; '.join(failures)}")
            continue
        eligible.append(policy)
        notes.append(
            f"{policy.key}: eligible — worst drawdown "
            f"{policy.worst_drawdown:.2%}, worst blocked {policy.worst_blocked:.2%}, "
            f"worst trade swing {policy.worst_swing:.2%}"
            if policy.worst_swing is not None
            else f"{policy.key}: eligible"
        )
    if not eligible:
        return None, notes
    best = min(
        eligible,
        key=lambda p: (
            p.worst_swing if p.worst_swing is not None else Decimal(1),
            p.median_blocked if p.median_blocked is not None else Decimal(1),
        ),
    )
    return best.key, notes
