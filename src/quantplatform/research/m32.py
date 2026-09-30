"""M32 — validating the rotation edge against survivorship and against the production engine.

**Pre-declaration. Written and committed before a single result was seen.**

M31 produced two configurations that clear every declared gate: CS2 at 25% fixed exposure on 1d
and RF1 at 25% on 4h, each with all four of its neighbours passing too. Both rest on a universe
of six assets that are the large caps *of 2026*, and both were measured by a simulator written
for M30 that has none of the production engine's certification. Those are the two things that
could still make the whole line worthless, and this milestone attacks them in that order.

**What the existing work already got right, so the remaining problem is named precisely.** The
simulator never fabricated history: :func:`~quantplatform.research.rotation.align` builds a union
grid and an asset becomes rankable only once it has a bar *and* enough of its own history for a
score, so SOL is absent from every 2019 ranking and nothing is forward-filled. The bias that
remains is not fabricated history, it is **selection**: the six were chosen knowing which assets
would still matter in 2026. The markets that were top-twenty in 2018 and then faded, and the two
that were top-ten and went to zero, are simply missing.

So Phase 1 does not re-cut the same six. It adds the faded and the dead, and lets a
point-in-time liquidity rule decide what was tradeable on each date.

**Why an inclusive pool is a conservative test and not a friendly one.** Momentum rotation buys
whatever is strongest. LUNA was among the strongest assets in the market in the weeks before it
went to zero in May 2022, and FTT was strong until November 2022. A rule that ranks by trailing
return is exactly the rule that would have rotated into both. Adding them can only make the
candidate's job harder, which is what makes their inclusion evidence rather than decoration.

**The gate is not restated here.** :func:`~quantplatform.research.m31.survives` is imported and
reused, so M32 cannot hold a different threshold than the milestone whose result it is checking.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from quantplatform.core.enums import Timeframe
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.m30 import ONE_WAY_COST_BASIS_POINTS, OOS_START, RotationRule
from quantplatform.research.m31 import (
    ASSETS_M30,
    COST_STRESS_MULTIPLIERS,
    FROZEN,
    Controlled,
    ControlRobustness,
    survives,
)
from quantplatform.research.m32_pool import ERAS, POOL, Era, PoolMember

__all__ = [
    "ASSETS_M30",
    "CANDIDATES_M32",
    "COST_STRESS_MULTIPLIERS",
    "ERAS",
    "FIXED_EXPOSURE",
    "LIQUIDITY_WINDOW",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "POOL",
    "UNIVERSE_SIZE",
    "Candidate",
    "ControlRobustness",
    "Controlled",
    "Era",
    "Phase2Claim",
    "PoolMember",
    "survives",
]


# --- The two frozen candidates -------------------------------------------------------------------


class Candidate(DomainModel):
    """One M31 result being re-examined, named by the signal and timeframe it was measured at."""

    key: Text
    signal: Text
    timeframe: Timeframe
    recorded_cagr: Text
    """What M31 measured, carried as text so it reads in a report and can never be arithmetic."""
    recorded_drawdown: Text
    recorded_calmar: Text


CANDIDATES_M32: Final[tuple[Candidate, ...]] = (
    Candidate(
        key="CS2-fixed25%-1d",
        signal="CS2",
        timeframe=Timeframe.D1,
        recorded_cagr="19.53%",
        recorded_drawdown="29.34%",
        recorded_calmar="0.67",
    ),
    Candidate(
        key="RF1-fixed25%-4h",
        signal="RF1",
        timeframe=Timeframe.H4,
        recorded_cagr="30.75%",
        recorded_drawdown="22.79%",
        recorded_calmar="1.35",
    ),
)
"""The only two configurations M32 may examine. Their signals come from
:data:`~quantplatform.research.m31.FROZEN`, which is M30's own objects by reference, so nothing
here can alter a lookback, a threshold or a filter even by accident."""

FIXED_EXPOSURE: Final[str] = "fixed 25%"
"""The exposure policy label both candidates carry, unchanged from M31."""


def signal_of(candidate: Candidate) -> RotationRule:
    """Return the frozen rule whose signals this candidate uses."""
    return next(rule for rule in FROZEN if rule.key == candidate.signal)


# --- The point-in-time universe ------------------------------------------------------------------

UNIVERSE_SIZE: Final[int] = len(ASSETS_M30)
"""How many markets may be rankable at once: six, because six is what M30 declared and what
M31's results were measured at. Holding the breadth fixed is what makes the corrected universe a
test of *which* six rather than of how many."""

LIQUIDITY_WINDOW: Final[int] = 72
"""Bars the liquidity measure looks back over. **Inherited**, like every other window in this
line, from ``momentum_roc``'s lookback."""


SIGNAL_DIVERGENCES_ALLOWED: Final[int] = 0
"""How many selection disagreements between the two engines are acceptable: none.

Declared as a number rather than as prose because an enum member's docstring is not readable
at runtime, so a test asserting on one asserts nothing. This is the claim actually under test --
given the same bars, the production chain must choose the same assets at the same instants -- and
it is either true or it is not.
"""

EQUITY_TOLERANCE_DECLARED: Final[bool] = False
"""Whether a numeric tolerance is declared for equity, fees or trade counts: deliberately not.

The production engine sizes from a stop distance and a risk budget, refuses orders, enforces
venue minimums and runs latching breakers; the rotation overlay holds a weight. Those curves will
differ, and promising in advance that they will agree within some band would amount to promising
that Risk V2 does not matter. Each difference is named and attributed to a mechanism instead.
"""


class Phase2Claim(StrEnum):
    """What the production-engine reproduction can and cannot establish.

    Declared before running because the honest answer is not "the numbers will match", and
    saying so afterwards would look like an excuse rather than a prediction.
    """

    SIGNAL_EQUIVALENCE = "signal_equivalence"
    """**Exact, and the claim actually under test.** Given the same bars, the production chain
    must select the same assets at the same instants as the rotation simulator. If it does, the
    selection logic is independent of the simulator that measured it. A single divergence is a
    failure, not a tolerance."""

    ACCOUNTING_DIFFERENCE = "accounting_difference"
    """**Expected, and to be explained rather than bounded.** The production engine sizes from a
    stop distance and a risk budget, refuses orders, enforces venue minimums and runs latching
    breakers. The rotation overlay has no equivalent of any of that: it holds a weight. So equity
    curves, fee totals and trade counts will differ, and declaring a numeric tolerance on CAGR in
    advance would amount to declaring that Risk V2 does not matter. Each difference is named and
    attributed to a mechanism instead."""


# --- Reproducibility of the pool ------------------------------------------------------------------


def pool_symbols() -> tuple[str, ...]:
    """Return every declared market, in declaration order."""
    return tuple(member.raw for member in POOL)


def already_validated() -> tuple[str, ...]:
    """Return the pool members M16 already downloaded and validated."""
    return tuple(member.raw for member in POOL if member.raw in ASSETS_M30)


def to_download() -> tuple[str, ...]:
    """Return the pool members whose archives this milestone has to fetch."""
    return tuple(member.raw for member in POOL if member.raw not in ASSETS_M30)
