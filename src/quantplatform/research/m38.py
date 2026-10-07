"""M38 — does M37's ALT2 structure survive outside the six markets it was found on?

**Pre-declaration, committed before a single M38 result existed.** Research only. Nothing here
is deployed. Production Risk, production execution, the paper sessions, the VPS and both
sleeves' parameters are untouched, and no strategy parameter is restated: the sleeves are
reached by reference through M36 and M34 to M29's own probe objects.

M37 found that the protective stop is what destroys this edge, and that one structural
alternative -- the strategy's own exit as the primary exit, with a static hard safety stop --
clears every pre-declared criterion. It found that on **six** markets at breadth six, a
portfolio that never selects because it always holds all six. M37 said in as many words that
its levels are not comparable with M36's and that the drawdown it measured sat 1.16 points from
the cap. M38 is the test of whether any of it holds on the full universe.

**The candidate is frozen, not explored.** One configuration, fixed below, carried over from
M37 without a number moved. No other stop distance is measured, 1200 is not adjusted, and there
is no ALT3. If the candidate fails a gate the line closes; a gate is not lowered to rescue it.

**Three bases, and two of them already exist.** Risk V2 on this universe is M36's own
sixty-pair extraction -- the same configuration object, verified by equality rather than
assumed -- so it is read rather than re-run. The signal basis is M35's cached timelines. Only
the V3 candidate is extracted, which is also what makes the comparison cheap enough to run at
full breadth.

**What M38 cannot settle.** It is still a backtest on one timeframe with sizing that a per-pair
engine run cannot make portfolio-aware, which is the architectural limit M34 declared and M36
restated. A pass here licenses a formal Risk V3 implementation in research and then an isolated
paper *design* -- not a deployment, and not a revision of M36's NO-GO on the combined
portfolio.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
)
from quantplatform.research.m30 import (
    MAX_SINGLE_ASSET_SHARE,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
)
from quantplatform.research.m32 import LIQUIDITY_WINDOW, UNIVERSE_SIZE
from quantplatform.research.m34 import MAX_PER_SLEEVE_SHARE, SLEEVES, TIMEFRAME, pool_symbols
from quantplatform.research.m36 import BREADTHS
from quantplatform.research.m37 import Mechanism
from quantplatform.risk.config import RiskConfiguration

__all__ = [
    "BREADTHS",
    "CANDIDATE_KEY",
    "COST_STRESS_MULTIPLIERS",
    "LIQUIDITY_WINDOW",
    "MAX_SINGLE_ASSET_SHARE",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_CALMAR",
    "MIN_TURNOVER_REDUCTION",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "REMOVED_BY_V3",
    "SAFETY_INVARIANTS",
    "SCREEN_MAX_DRAWDOWN",
    "SLEEVES",
    "TIMEFRAME",
    "UNIVERSE_SIZE",
    "Basis",
    "BreadthResult",
    "Gate",
    "SafetyInvariant",
    "invariants_for",
    "passes",
    "pool_symbols",
    "turnover_reduced",
]


CANDIDATE_KEY: Final[str] = "ALT2"
"""The M37 alternative being validated, named by its own key so the two cannot drift apart.

M38 measures the configuration ``m37_alternatives`` already declares and
``m37_definitions.alternative_risk`` already builds. It does not restate the stop distance, the
removals or the sizing rule: a second copy of a frozen candidate is a second thing to keep in
step, and the point of freezing it was that there is only one."""

REMOVED_BY_V3: Final[frozenset[Mechanism]] = frozenset(
    {
        Mechanism.BREAK_EVEN,
        Mechanism.TRAILING_STOP,
        Mechanism.TAKE_PROFIT,
        Mechanism.TIME_STOP,
    }
)
"""What the candidate deletes, asserted against the M37 declaration rather than retyped as
policy. The initial stop and risk-based sizing are both kept, and so are the breakers."""


class Basis(StrEnum):
    """The three things being compared. Only the last one is extracted by M38."""

    SIGNAL = "signal"
    """The strategy timelines with no risk layer at all: the ceiling, not a candidate."""

    RISK_V2 = "risk_v2"
    """The deployed configuration, read from M36's own sixty-pair extraction."""

    RISK_V3 = "risk_v3"
    """M37's frozen ALT2 candidate, extracted by M38 on the full universe."""


MIN_TURNOVER_REDUCTION: Final[Decimal] = Decimal("0.25")
"""How far below Risk V2's turnover the candidate must sit to count as "clearly less".

The same quarter M37 used to call a mechanism primary, kept rather than re-chosen so the two
milestones' notions of "clearly less churn" are the same notion. On M37's six markets the
candidate cut turnover 51.3%, so this is not a bar set where the answer was already known to
sit -- it is the bar that was already in use."""


class SafetyInvariant(StrEnum):
    """Properties of the candidate that must hold for it to be a risk configuration at all.

    Checked against the built configuration rather than against the sample, because every one of
    them is a fact about how the mechanism is written. A sample that happened never to reach a
    stop would not make the stop absent, and a run that happened not to trip a breaker would not
    mean the breaker was gone.
    """

    STOP_EXISTS = "stop_exists"
    """Some price level still closes a position before a loss becomes unbounded."""

    STOP_REQUIRED_ON_ENTRY = "stop_required_on_entry"
    """No entry may be approved without protection."""

    SIZED_BY_RISK = "sized_by_risk"
    """Position size comes from the risk budget, not from a fixed notional fraction.

    M37's variant A showed what the fallback does: ``entry_fraction`` is 0.95, so a position
    becomes 95% of the account and the engine held something on 10% of the bars the strategy
    wanted one."""

    BREAKERS_PRESENT = "breakers_present"
    """The drawdown, daily-drawdown and daily-loss breakers are configured as deployed."""

    NO_LATCH = "no_latch"
    """No breaker latches, so no state exists that price action alone cannot leave."""

    NO_RATCHET = "no_ratchet"
    """No stop that only ever tightens. The trailing stop is one by construction and is gone."""

    STOP_WITHIN_BUDGET_WINDOW = "stop_within_budget_window"
    """The stop sits strictly inside the budget's distance window.

    Strictly, not merely within: M37 established that a stop configured *at*
    ``max_stop_distance_bps`` realises outside it once rounded to the venue tick, and the engine
    then refuses every entry. An invariant that allowed the boundary would permit a
    configuration that cannot trade."""


SAFETY_INVARIANTS: Final[frozenset[SafetyInvariant]] = frozenset(SafetyInvariant)
"""Every invariant must hold. There is no subset that counts as passing."""


class Gate(StrEnum):
    """Why the candidate does not pass. Declared before any full-universe number existed."""

    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"
    COST_FRAGILE = "cost_fragile"
    TURNOVER_NOT_REDUCED = "turnover_not_reduced"
    ASSET_CONCENTRATION = "asset_concentration"
    SLEEVE_CONCENTRATION = "sleeve_concentration"
    SINGLE_YEAR_CONCENTRATION = "single_year_concentration"
    WEAK_PROFIT_FACTOR = "weak_profit_factor"
    SAFETY_REGRESSION = "safety_regression"


class BreadthResult(DomainModel):
    """One breadth measured on one basis, with no judgement applied."""

    basis: Basis
    breadth: int
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    profit_factor: Decimal | None
    turnover: Decimal
    fees: Decimal
    stops: int
    re_entries: int
    held_share_of_wanted: Decimal | None
    out_of_sample_return: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    single_year_share: Decimal | None
    top_asset_share: Decimal | None
    top_sleeve_share: Decimal | None
    years_positive_share: Decimal | None
    """Share of calendar years that closed above where they opened."""


def turnover_reduced(candidate: BreadthResult, deployed: BreadthResult) -> bool:
    """Return whether the candidate churns clearly less than the deployed configuration."""
    if deployed.turnover <= 0:
        return False
    return (deployed.turnover - candidate.turnover) / deployed.turnover >= MIN_TURNOVER_REDUCTION


def passes(
    candidate: BreadthResult,
    deployed: BreadthResult,
    *,
    invariants_held: frozenset[SafetyInvariant],
) -> tuple[bool, tuple[Gate, ...]]:
    """Return whether the candidate clears every declared gate at this breadth, and what failed.

    Thresholds are inherited rather than chosen: the drawdown cap is M22's, the Calmar floor and
    the cost multipliers are M29's, the concentration limits are M30's and M34's. M38 adds one
    number of its own, the turnover reduction, and that one is M37's.
    """
    failed: list[Gate] = []
    if candidate.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failed.append(Gate.DRAWDOWN)
    if candidate.calmar_ratio is None or candidate.calmar_ratio < MIN_CALMAR:
        failed.append(Gate.LOW_CALMAR)
    if candidate.out_of_sample_return is None or candidate.out_of_sample_return <= 0:
        failed.append(Gate.OUT_OF_SAMPLE_NEGATIVE)
    if (
        candidate.annual_at_double_cost is None
        or candidate.annual_at_double_cost <= 0
        or candidate.annual_at_triple_cost is None
        or candidate.annual_at_triple_cost <= 0
    ):
        failed.append(Gate.COST_FRAGILE)
    if not turnover_reduced(candidate, deployed):
        failed.append(Gate.TURNOVER_NOT_REDUCED)
    if candidate.top_asset_share is not None and candidate.top_asset_share > MAX_SINGLE_ASSET_SHARE:
        failed.append(Gate.ASSET_CONCENTRATION)
    if candidate.top_sleeve_share is not None and candidate.top_sleeve_share > MAX_PER_SLEEVE_SHARE:
        failed.append(Gate.SLEEVE_CONCENTRATION)
    if (
        candidate.single_year_share is not None
        and candidate.single_year_share > MAX_SINGLE_YEAR_SHARE
    ):
        failed.append(Gate.SINGLE_YEAR_CONCENTRATION)
    if candidate.profit_factor is None or candidate.profit_factor < MIN_NEIGHBOUR_PROFIT_FACTOR:
        failed.append(Gate.WEAK_PROFIT_FACTOR)
    if invariants_held != SAFETY_INVARIANTS:
        failed.append(Gate.SAFETY_REGRESSION)
    return not failed, tuple(failed)


def invariants_for(
    candidate: RiskConfiguration, deployed: RiskConfiguration
) -> frozenset[SafetyInvariant]:
    """Return which safety invariants the candidate configuration actually holds.

    Read from the two configurations, never from a run. Every invariant here is a property of
    how the mechanism is written, so a sample is the wrong place to look: a run that never
    reached a stop would report the stop absent, and a run that never tripped a breaker would
    report the breaker gone. ``deployed`` supplies what "as deployed" means for the breakers, so
    the comparison cannot drift from the thing it claims to preserve.
    """
    held: set[SafetyInvariant] = set()
    if candidate.initial_stop_distance_bps is not None:
        held.add(SafetyInvariant.STOP_EXISTS)
    if candidate.stop_required:
        held.add(SafetyInvariant.STOP_REQUIRED_ON_ENTRY)
    if candidate.risk_v2_active:
        held.add(SafetyInvariant.SIZED_BY_RISK)
    if (
        candidate.max_total_drawdown_pct == deployed.max_total_drawdown_pct
        and candidate.max_daily_drawdown_pct == deployed.max_daily_drawdown_pct
        and candidate.max_daily_loss_pct == deployed.max_daily_loss_pct
    ):
        held.add(SafetyInvariant.BREAKERS_PRESENT)
    if not candidate.latch_total_drawdown:
        held.add(SafetyInvariant.NO_LATCH)
    if candidate.trailing_activation_bps is None and candidate.trailing_distance_bps is None:
        held.add(SafetyInvariant.NO_RATCHET)
    budget = candidate.risk_budget
    distance = candidate.initial_stop_distance_bps
    if (
        budget is not None
        and distance is not None
        and budget.min_stop_distance_bps < distance < budget.max_stop_distance_bps
    ):
        held.add(SafetyInvariant.STOP_WITHIN_BUDGET_WINDOW)
    return frozenset(held)
