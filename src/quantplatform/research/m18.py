"""M18 — does policy G hold up where the breaker actually fires?

M17 recommended G (halt at 10% drawdown, reopen after 30 days, measure the next drawdown from
the reopening) but on thin evidence: at real costs the breaker engages in exactly one of six
markets, so the whole choice rested on XRP and a single episode in July 2020. Two questions
were left open and both are about what happens when a halt is *not* rare:

* **Flapping** — does the market reopen and immediately halt again, as policy E did twenty
  times under stress?
* **Ratchet** — each restart lets the account fall another 10% from a lower level. Chained,
  that turns a 10% limit into an uncontrolled loss. M17 never observed it; it also never had
  more than one halt in any market.

**Two ways to make the breaker fire, both plausible and both reproducible.**

1. **A cost ladder.** S2 is fees x3, slippage x5 and a 10 bps spread; S3 is fees x5, slippage
   x10 and 25 bps. Punishing, but these are real conditions for a thin altcoin on a bad day,
   and they are built with the harness's own cost scenarios. S0 (real costs) and S1 (M16's
   fees x2 + slippage x3 + 5 bps) already exist from M17 and are cited, not re-run.
2. **A tighter breaker.** The same policies with the drawdown limit at **5%** — the tightest
   the risk configuration itself accepts — at real costs. This distorts no market at all: it
   simply makes the breaker engage often enough to watch it reopen, again and again. It is a
   probe of the mechanism, **never a candidate configuration**.

What is deliberately *not* done: no invented price path, no synthetic crash, no adverse fill
sequence. The harness has no such machinery, and a market built to break a policy would prove
only that it could be built.

**What G has to show, fixed before the runs.** Each is a gate, and all of them are about
behaviour rather than profit — return is not an input to any of them.

======================  ====================================================================
 gate                    G passes when
======================  ====================================================================
 protects                worst drawdown in any scenario stays within twice its own limit
 recovers                at least half its halts are followed by a trade before the next
 does not flap           at most a quarter of its halts start within a week of a reopening
 does not ratchet        no chain of more than two consecutive lower restarts, and no chain
                         losing more than twice the limit from where it began
 stays continuous        trade count moves less than a quarter across the fee settings
======================  ====================================================================

A policy failing any gate fails the milestone, whatever it earned.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.m17 import POLICIES
from quantplatform.research.recovery import RecoveryPolicy
from quantplatform.research.stress import StressScenario
from quantplatform.risk.config import RiskConfiguration

__all__ = [
    "FLAPPING_WINDOW",
    "MAX_FLAPPING_SHARE",
    "MAX_RATCHET_CHAIN",
    "MIN_RECOVERY_SHARE",
    "PROBE_DRAWDOWN",
    "PROBE_FEE_MULTIPLIERS",
    "SCENARIOS",
    "STUDIED",
    "CostScenario",
    "Gate",
    "flapping_share",
    "judge_policy",
    "ratchet_chains",
    "scenario_for",
    "with_threshold",
]

STUDIED: Final[tuple[str, ...]] = ("C", "E", "G")
"""The permanent latch, the cooldown that carries its reference, and the cooldown that moves
it. A is production and F is the blind quarterly rule; both are cited from M17, not re-run."""


class CostScenario(DomainModel):
    """One set of cost assumptions, applied to a definition without touching anything else."""

    label: Text
    fee_multiplier: Decimal
    slippage_multiplier: Decimal
    spread_bps: Decimal


SCENARIOS: Final[tuple[CostScenario, ...]] = (
    CostScenario(
        label="S2: fees x3, slippage x5, spread 10bps",
        fee_multiplier=Decimal(3),
        slippage_multiplier=Decimal(5),
        spread_bps=Decimal(10),
    ),
    CostScenario(
        label="S3: fees x5, slippage x10, spread 25bps",
        fee_multiplier=Decimal(5),
        slippage_multiplier=Decimal(10),
        spread_bps=Decimal(25),
    ),
)

PROBE_DRAWDOWN: Final[Decimal] = Decimal("0.05")
"""The tightest total-drawdown limit the risk configuration accepts; it refuses anything at or
below the 5% daily drawdown limit, so this is the floor rather than a number chosen to suit."""

PROBE_FEE_MULTIPLIERS: Final[tuple[Decimal, ...]] = (Decimal("1.00"), Decimal("1.25"))

MIN_RECOVERY_SHARE: Final[Decimal] = Decimal("0.50")
FLAPPING_WINDOW: Final[timedelta] = timedelta(days=7)
MAX_FLAPPING_SHARE: Final[Decimal] = Decimal("0.25")
MAX_RATCHET_CHAIN: Final[int] = 2
MAX_DRAWDOWN_MULTIPLE: Final[Decimal] = Decimal(2)
MAX_SWING: Final[Decimal] = Decimal("0.25")
PAIR: Final[int] = 2
"""Two halts: the fewest that can be compared, and so the fewest that can flap."""


def scenario_for(definition: ExperimentDefinition, scenario: CostScenario) -> StressScenario:
    """Return the harness scenario that applies these costs to ``definition``."""
    policy = definition.risk.execution_policy
    costs = policy.model_dump()
    costs["fee"]["basis_points"] = policy.fee.basis_points * scenario.fee_multiplier
    costs["slippage"]["basis_points"] = policy.slippage.basis_points * scenario.slippage_multiplier
    risk = RiskConfiguration.model_validate(
        {**definition.risk.model_dump(), "execution_policy": costs}
    )
    backtest = BacktestConfig.model_validate(
        {**definition.backtest.model_dump(), "assumed_spread_basis_points": scenario.spread_bps}
    )
    return StressScenario(risk=risk, backtest=backtest)


def with_threshold(policy: RecoveryPolicy, drawdown_pct: Decimal) -> RecoveryPolicy:
    """Return the same recovery rule at a different drawdown limit, for the probe."""
    return policy.model_copy(update={"drawdown_pct": drawdown_pct})


def studied_policies() -> tuple[RecoveryPolicy, ...]:
    """Return the three policies this milestone re-runs, in declaration order."""
    return tuple(p for p in POLICIES if p.key in STUDIED)


def ratchet_chains(episodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every run of consecutive halts whose restart reference stepped down.

    One halt that lowers the reference is the policy working. Several in a row, each starting
    from a lower level than the last, is the reference walking downwards — the failure mode a
    moving high-water mark can have. Each chain reports how far the reference fell from where
    the chain began, which is the loss the limit was supposed to bound.
    """
    chains: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    previous: Decimal | None = None
    for episode in episodes:
        after = episode.get("reference_after")
        if after is None:
            continue
        reference = Decimal(str(after))
        if previous is not None and reference < previous:
            current.append(episode)
        else:
            if len(current) >= PAIR:
                chains.append(_chain(current))
            current = [episode]
        previous = reference
    if len(current) >= PAIR:
        chains.append(_chain(current))
    return chains


def _chain(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    first = Decimal(str(episodes[0]["reference_before"]))
    last = Decimal(str(episodes[-1]["reference_after"]))
    return {
        "halts": len(episodes),
        "from": episodes[0]["began"],
        "to": episodes[-1]["ended"],
        "decline": (first - last) / first if first else None,
    }


def flapping_share(episodes: Sequence[dict[str, Any]]) -> Decimal | None:
    """Return the share of halts that began within a week of the previous reopening."""
    starts = [e for e in episodes if e.get("began")]
    if len(starts) < PAIR:
        return None
    quick = 0
    for earlier, later in pairwise(starts):
        ended = earlier.get("ended")
        if ended is None:
            continue
        gap = _moment(later["began"]) - _moment(ended)
        if gap <= FLAPPING_WINDOW:
            quick += 1
    return Decimal(quick) / Decimal(len(starts) - 1)


def _moment(value: object) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class Gate(DomainModel):
    """One requirement, and whether the evidence met it."""

    name: Text
    passed: bool
    detail: Text


def judge_policy(
    *,
    threshold: Decimal,
    worst_drawdown: Decimal | None,
    halts: int,
    effective: int,
    flapping: Decimal | None,
    longest_chain: int,
    worst_chain_decline: Decimal | None,
    swing: Decimal | None,
) -> tuple[bool, list[Gate]]:
    """Apply this milestone's gates, in the order they are declared. Return never enters."""
    limit = threshold * MAX_DRAWDOWN_MULTIPLE
    recovery = Decimal(effective) / Decimal(halts) if halts else None
    gates = [
        Gate(
            name="protects",
            passed=worst_drawdown is None or worst_drawdown <= limit,
            detail=(
                "never halted"
                if worst_drawdown is None
                else f"worst drawdown {worst_drawdown:.2%} against a {limit:.0%} allowance"
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
                else f"{flapping:.0%} of halts began within a week of a reopening"
            ),
        ),
        Gate(
            name="does not ratchet",
            passed=(
                longest_chain <= MAX_RATCHET_CHAIN
                and (worst_chain_decline is None or worst_chain_decline <= limit)
            ),
            detail=(
                f"longest chain of lower restarts: {longest_chain}"
                + (
                    ""
                    if worst_chain_decline is None
                    else f", worst chain gave up {worst_chain_decline:.2%}"
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
