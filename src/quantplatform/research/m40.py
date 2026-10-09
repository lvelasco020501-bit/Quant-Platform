"""M40 — is the fixed seven-day limit closing 1D strategies before their edge matures?

**Pre-declaration, committed before a single M40 result existed.** Research only. Nothing is
deployed. Production Risk V2, the VPS, the paper sessions, every strategy parameter, the price
stops, the breakers, the take profit, the trailing stop and the sizing are all untouched: phase
1 changes exactly one field and changes it only in a research configuration.

**Where the question came from.** M39 screened three new families through Risk V2 from their
first measurement and all six variants failed. The useful part was why its own prediction broke:
at 1D, Risk V2 closes *more* positions on its seven-bar time stop than on its price stop -- 371
against 294 for the dual-horizon rule, 437 against 294 for the retest rule. M39 had chosen its
families for where they entered, a property the time stop is indifferent to, so the milestone's
reasoning was wrong and said so. M40 asks the question M39's failure actually raised.

**Two variants, and the reason is M39's own measurement rather than a preference.** MC1 and RT1
were the two with the best risk-to-signal compatibility -- 0.76 and 1.14, where the pullback
family scored negative. Studying the rules that already coexist with Risk V2 isolates the time
stop; studying a rule that has no edge would only re-measure that.

**Phase 1 establishes causality or stops.** It does not look for a better limit. No fourteen-day,
twenty-one-day or thirty-day variant exists in this module, and none may be added before the
ablation has shown the time stop is the cause. A milestone that tries durations first learns
which number flatters the sample.

**What phase 1 cannot settle.** Removing a constraint almost always raises return, because the
constraint exists to remove exposure. So the causal test is deliberately not "did CAGR rise": it
requires the risk-adjusted figure to rise too, the drawdown not to deteriorate materially, the
out-of-sample and stressed results to improve, and the premature closes to actually fall. A pure
exposure effect clears the first of those and fails the rest.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m37 import TURNOVER_REDUCTION_FOR_PRIMARY, Mechanism
from quantplatform.research.m39 import (
    ASSETS_M39,
    COST_STRESS_MULTIPLIERS,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    TIMEFRAME,
    VARIANTS_M39,
    Variant,
)

__all__ = [
    "ABLATED",
    "ASSETS_M39",
    "CLEAR_IMPROVEMENT",
    "COST_STRESS_MULTIPLIERS",
    "MATERIAL_DRAWDOWN_INCREASE",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "SCREEN_MAX_DRAWDOWN",
    "STUDIED",
    "STUDIED_KEYS",
    "TIMEFRAME",
    "Arm",
    "CausalCheck",
    "Reason",
    "Side",
    "causal",
    "establishes_cause",
]


STUDIED_KEYS: Final[tuple[str, ...]] = ("MC1", "RT1")
"""The two variants M39 measured as most compatible with Risk V2, named by their M39 keys.

Not a judgement of M40's: M39 recorded compatibility ratios of 0.76 for MC1 and 1.14 for RT1,
against negative ratios for the pullback family. These two are the only rules in M39 that both
survived contact with Risk V2 and had an edge to lose."""

STUDIED: Final[tuple[Variant, ...]] = tuple(
    variant for variant in VARIANTS_M39 if variant.key in STUDIED_KEYS
)
"""The variants themselves, taken from M39 by reference so no parameter is restated here."""

ABLATED: Final[Mechanism] = Mechanism.TIME_STOP
"""The one mechanism phase 1 removes, named from M37's enumeration rather than redefined.

Its configuration override lives in ``m37_definitions.OVERRIDES`` and is ``max_holding_bars:
None``. One field, and M40 adds no second one."""


class Side(DomainModel):
    """One arm of the comparison, measured with no judgement applied."""

    arm: str
    key: str
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    profit_factor: Decimal | None
    trades: int
    forced_exits: int
    time_stop_exits: int
    turnover: Decimal
    fees: Decimal
    held_share_of_wanted: Decimal | None
    out_of_sample_return: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    years_positive_share: Decimal | None
    single_year_share: Decimal | None


class Arm:
    """The two arms, as plain identifiers so a row cannot be filed under a third."""

    FULL: Final[str] = "risk_v2_full"
    NO_TIME_STOP: Final[str] = "risk_v2_no_time_stop"


CLEAR_IMPROVEMENT: Final[Decimal] = TURNOVER_REDUCTION_FOR_PRIMARY
"""How much better a figure must get to count as "clearly" better: a quarter, relatively.

Taken by reference rather than chosen. This project has already fixed what "clearly" means
twice, in M37's primary-cause rule and again in M38's turnover gate, and both are a quarter.
Using the same number keeps three milestones talking about the same thing."""

MATERIAL_DRAWDOWN_INCREASE: Final[Decimal] = Decimal("0.05")
"""How much worse the drawdown may get before the change counts as breaking it: five points.

The only number M40 adds, and it is set against this project's own history rather than taste.
Drawdown decisions here have turned on this scale: M38's candidate failed its gate by 5.53
points, and M37's passed with 1.16 points of headroom. Five points is therefore the granularity
at which a drawdown change has actually decided a milestone in this codebase. Expressed in
percentage points rather than relatively, because a drawdown is already a ratio and compounding
one ratio onto another obscures what it means."""


class Reason(StrEnum):
    """Why removing the time stop does not establish it as the cause."""

    CAGR_NOT_CLEARLY_BETTER = "cagr_not_clearly_better"
    CALMAR_NOT_CLEARLY_BETTER = "calmar_not_clearly_better"
    DRAWDOWN_MATERIALLY_WORSE = "drawdown_materially_worse"
    OUT_OF_SAMPLE_NOT_BETTER = "out_of_sample_not_better"
    STRESS_NOT_BETTER = "stress_not_better"
    PREMATURE_CLOSES_NOT_REDUCED = "premature_closes_not_reduced"


class CausalCheck(DomainModel):
    """One variant's two arms, judged against the pre-declared causal conditions."""

    key: str
    full: Side
    ablated: Side

    @property
    def relative_cagr_gain(self) -> Decimal | None:
        """Return the ablated arm's annual return as a relative gain over the full arm's."""
        if self.full.annual is None or self.ablated.annual is None or self.full.annual <= 0:
            return None
        return (self.ablated.annual - self.full.annual) / self.full.annual

    @property
    def relative_calmar_gain(self) -> Decimal | None:
        """Return the ablated arm's Calmar as a relative gain over the full arm's."""
        if (
            self.full.calmar_ratio is None
            or self.ablated.calmar_ratio is None
            or self.full.calmar_ratio <= 0
        ):
            return None
        return (self.ablated.calmar_ratio - self.full.calmar_ratio) / self.full.calmar_ratio

    @property
    def drawdown_increase(self) -> Decimal:
        """Return how many points of drawdown the ablation added, negative if it removed some."""
        return self.ablated.max_drawdown - self.full.max_drawdown


def causal(check: CausalCheck) -> tuple[bool, tuple[Reason, ...]]:
    """Return whether removing the time stop alone establishes it as the cause, and what failed.

    Every condition must hold. Removing a constraint raises return almost by construction, since
    the constraint's purpose is to remove exposure, so a rise in annual return alone establishes
    nothing: the risk-adjusted figure has to rise as well, the drawdown must not deteriorate
    materially, the out-of-sample and both stressed results must improve, and the closes the
    time stop was causing must actually fall.
    """
    failed: list[Reason] = []
    cagr_gain = check.relative_cagr_gain
    if cagr_gain is None or cagr_gain < CLEAR_IMPROVEMENT:
        failed.append(Reason.CAGR_NOT_CLEARLY_BETTER)
    calmar_gain = check.relative_calmar_gain
    if calmar_gain is None or calmar_gain < CLEAR_IMPROVEMENT:
        failed.append(Reason.CALMAR_NOT_CLEARLY_BETTER)
    if check.drawdown_increase > MATERIAL_DRAWDOWN_INCREASE:
        failed.append(Reason.DRAWDOWN_MATERIALLY_WORSE)
    if (
        check.ablated.out_of_sample_return is None
        or check.full.out_of_sample_return is None
        or check.ablated.out_of_sample_return <= check.full.out_of_sample_return
    ):
        failed.append(Reason.OUT_OF_SAMPLE_NOT_BETTER)
    pairs = (
        (check.ablated.annual_at_double_cost, check.full.annual_at_double_cost),
        (check.ablated.annual_at_triple_cost, check.full.annual_at_triple_cost),
    )
    if any(after is None or before is None or after <= before for after, before in pairs):
        failed.append(Reason.STRESS_NOT_BETTER)
    # The ablated arm has no time-stop exits by construction, so the honest measure of "fewer
    # premature closes" is that total risk closes fell *and* the account now holds more of what
    # the rule wanted. Either alone could be satisfied without positions maturing.
    held_before = check.full.held_share_of_wanted
    held_after = check.ablated.held_share_of_wanted
    if (
        check.ablated.forced_exits >= check.full.forced_exits
        or held_before is None
        or held_after is None
        or held_after <= held_before
    ):
        failed.append(Reason.PREMATURE_CLOSES_NOT_REDUCED)
    return not failed, tuple(failed)


def establishes_cause(checks: tuple[CausalCheck, ...]) -> bool:
    """Return whether the time stop is established as the cause across both studied variants.

    Both, not either. A mechanism that only damages one of two rules is an interaction with that
    rule, not "the time stop closing 1D strategies before their edge matures" -- and phase 2
    would then be designing a replacement for a problem that has not been shown to be general.
    """
    if len(checks) != len(STUDIED_KEYS):
        return False
    return all(causal(check)[0] for check in checks)
