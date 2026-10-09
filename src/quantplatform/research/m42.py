"""M42 — can allocating by risk, rather than selecting signals, bring the drawdown under control?

**Pre-declaration, committed before a single M42 number existed.** Research only. Nothing here
is deployed. Production Risk, production execution, the paper sessions, the VPS and both
sleeves' parameters are untouched. No strategy is added, no indicator is added, no stop is
moved, Risk V2 is not modified, and there is no leverage and no short.

M41 closed the research programme down to one hypothesis and said why: drawdown is what has
killed every line since M29, and the only thing that has ever reduced it without charging a
toll was diversification rather than selection -- M34 reached 31.87% by combining two sleeves
whose correlation was 0.08, M35 reached 25.50% at Calmar 1.38 purely by widening to twelve
markets. Both of those changed *what* was held. M42 changes *how much of each*, which is the
one axis the eight families tested since M13 never touched.

**Phase 1 is arithmetic over evidence already on disk.** M38's sixty-pair Risk V2 position
masks, the same point-in-time universe, the same signals, the same costs, the same windows,
both declared breadths. No ``BacktestEngine`` run. That is the whole reason M41 ranked this
hypothesis first: it can be refuted before any compute is spent.

**The gate is close to self-excluding, and that is said here rather than discovered later.**
Risk V2 on the full universe earns 7.53% a year at breadth six, as M38 measured it. Conserving
half of it leaves 3.77%, and a Calmar floor of 0.50 then demands a drawdown at or under 7.5% --
a fifth of the 35% cap that the drawdown gate alone would allow. Taken together the gates therefore
require inverse-vol to *raise* the return while *lowering* the drawdown, not merely to trade one
for the other. That is a hard bar for a pure re-weighting, and it is written down before the
measurement so that a NO-GO cannot later be dismissed as a badly chosen gate -- and so that a
PASS cannot be manufactured by lowering one. **No threshold here moves after results exist.**
Every one of them is inherited: the drawdown cap is M22's, the Calmar floor and the cost
multipliers are M29's, the concentration limits are M30's and M34's, the conservation fraction
is M37's, the volatility window is M32's.

**What a pass would and would not license.** Phase 2, and nothing else: reproducing the same
allocation through the certified portfolio path to show the result is not an accounting
artefact. Even both phases together license a separate implementation and paper *design*, not a
deployment, and they do not reopen M36's NO-GO on the combined portfolio.
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
from quantplatform.research.m32 import LIQUIDITY_WINDOW
from quantplatform.research.m34 import MAX_PER_SLEEVE_SHARE, SLEEVES, TIMEFRAME, pool_symbols
from quantplatform.research.m36 import BREADTHS
from quantplatform.research.m37 import EDGE_CONSERVATION_FOR_ALTERNATIVE

__all__ = [
    "BREADTHS",
    "COST_STRESS_MULTIPLIERS",
    "EXPOSURE_MATCH_TOLERANCE",
    "LIQUIDITY_WINDOW",
    "MAX_PER_SLEEVE_SHARE",
    "MAX_SINGLE_ASSET_SHARE",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_CAGR_CONSERVATION",
    "MIN_CALMAR",
    "MIN_NEIGHBOUR_PROFIT_FACTOR",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "SCREEN_MAX_DRAWDOWN",
    "SLEEVES",
    "TIMEFRAME",
    "VOLATILITY_FEATURE",
    "VOLATILITY_WINDOW",
    "Arm",
    "ArmResult",
    "Gate",
    "cagr_conserved",
    "passes",
    "pool_symbols",
]


# --- What is being compared -----------------------------------------------------------------------


class Arm(StrEnum):
    """The two allocations. Same masks, same universe, same costs; only the weights differ."""

    EQUAL_WEIGHT = "equal_weight"
    """The allocation every milestone since M34 has used: one active signal holds
    ``1 / (universe size x sleeves)``, capped at ``1 / universe size`` per market. This is the
    baseline, and it is not re-measured from a document -- it is recomputed here so both arms
    come off one measurement path."""

    INVERSE_VOL = "inverse_vol"
    """The candidate: the same funded holdings, weighted in proportion to the reciprocal of
    their market's realised volatility, rescaled to deploy exactly what the baseline deploys in
    that same bar."""


# --- The volatility, taken from the project rather than chosen ------------------------------------


VOLATILITY_WINDOW: Final[int] = LIQUIDITY_WINDOW
"""Bars the volatility estimate looks back over: M32's liquidity window, by reference.

Not a number chosen for M42. It is the window the point-in-time universe rule already ranks
over, itself inherited from ``momentum_roc``'s lookback, and taking it means M42 introduces no
new window at all. Choosing it also very nearly eliminates the insufficient-history case:
``eligible_universe`` already refuses a market with fewer than this many bars of its own
history, so a funded market is almost always one that can be measured. Almost, not quite --
``rvol_n`` reads n+1 bars because n returns need n+1 closes, so a market is eligible for exactly
one bar before its volatility exists. That bar is handled by the declared rule below and
counted, not waved away."""

VOLATILITY_FEATURE: Final[str] = f"rvol_{VOLATILITY_WINDOW}"
"""The volatility definition: the production indicator ``rvol_<n>``, the population standard
deviation of the last n one-bar close-to-close returns.

Computed by ``IndicatorFeatures`` -- the same pipeline the strategies and M31's
volatility-normalised score read -- and not reimplemented here. A second implementation of a
number this project already computes would be a second thing to keep in step, and M31 already
established what this family of windows must also satisfy: the window has to be contiguous in
calendar time, not merely in index, or a halt like FTT's 311 days turns a 72-bar return into a
383-day one."""


class Unmeasurable(StrEnum):
    """Why a funded holding may receive no inverse-vol weight. Declared, not improvised.

    In every one of these cases the holding is **not funded** at that slot and its share is
    redistributed to the rest of that bar's holdings by the same inverse-vol rule, so aggregate
    exposure is preserved. The project's standing convention for a quantity it cannot compute is
    silence rather than a guess -- a strategy omits a feature whose window is short and returns
    no signal rather than inventing one -- and an unknown risk is the last thing that should be
    sized as though it were known.
    """

    SHORT_HISTORY = "short_history"
    """Fewer than ``VOLATILITY_WINDOW + 1`` bars of this market's own history at that slot."""

    GAPPED_WINDOW = "gapped_window"
    """The window is not contiguous in calendar time: a listing, halt or relisting seam."""

    ZERO_VOLATILITY = "zero_volatility"
    """A flat window. The reciprocal does not exist, and the project's declared response to a
    zero denominator is silence rather than an error."""


EXPOSURE_MATCH_TOLERANCE: Final[Decimal] = Decimal("1e-25")
"""How far a bar's inverse-vol exposure may sit from the baseline's and still count as equal.

A construction tolerance, not a gate: the weights are quotients carried at the project's
forty-digit working precision, so the sum of the parts need not land on the whole to the last
digit. It is twenty-five orders of magnitude below a basis point, which is to say it admits
rounding and nothing else.

**The per-market cap cannot force a shortfall, and that is provable rather than hopeful.** The
baseline deploys ``active signals / (universe size x sleeves)``, and the caps available at that
bar total ``wanted markets / universe size``. Since a market can be wanted by at most every
sleeve, the first is never larger than the second, so water-filling always has somewhere left to
put the budget. A deficit can therefore only come from the ``Unmeasurable`` rule emptying a bar
completely, which the code puts in cash and counts. The practical consequence is what makes the
experiment clean: both arms hold the same capital in the same bars, so a drawdown difference is
a re-weighting effect and cannot be a de-risking effect wearing its clothes."""


# --- The gate -------------------------------------------------------------------------------------


MIN_CAGR_CONSERVATION: Final[Decimal] = EDGE_CONSERVATION_FOR_ALTERNATIVE
"""The fraction of the baseline's annual return the re-weighting must keep: M37's half.

By reference, because it is already this project's notion of "enough of the edge survived to
continue" -- M37 set it at half, against a quarter for merely identifying a cause, on the
grounds that an alternative proposing to replace part of a working system is held to a higher
bar than a diagnosis. Inverse-vol is exactly such a proposal, so it inherits the number rather
than being handed one picked for it."""


class Gate(StrEnum):
    """Why the inverse-vol arm does not pass. Declared before any M42 result existed."""

    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"
    COST_FRAGILE = "cost_fragile"
    ASSET_CONCENTRATION = "asset_concentration"
    SLEEVE_CONCENTRATION = "sleeve_concentration"
    SINGLE_YEAR_CONCENTRATION = "single_year_concentration"
    WEAK_PROFIT_FACTOR = "weak_profit_factor"
    CAGR_NOT_CONSERVED = "cagr_not_conserved"
    EXPOSURE_NOT_MATCHED = "exposure_not_matched"
    """The arms did not hold the same capital bar by bar, so the comparison is not a comparison
    of two allocations. A failure of construction rather than of performance, and it is a gate
    and not an assertion because a silent fallback to cash would otherwise read as a virtue."""


class ArmResult(DomainModel):
    """One arm measured at one breadth, with no judgement applied."""

    arm: Arm
    breadth: int
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    profit_factor: Decimal | None
    turnover: Decimal
    fees: Decimal
    exposure: Decimal | None
    out_of_sample_return: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    single_year_share: Decimal | None
    years_positive_share: Decimal | None
    top_asset_share: Decimal | None
    top_sleeve_share: Decimal | None
    unfunded_bars: int = 0
    """Bars where no funded holding had a usable volatility, so the bar stayed in cash."""
    capital_deficit: Decimal = Decimal(0)
    """Total exposure the arm failed to deploy relative to the baseline, summed over bars.
    Non-zero only through ``Unmeasurable``; never negative, because nothing levers up."""


def cagr_conserved(candidate: ArmResult, baseline: ArmResult) -> bool:
    """Return whether the candidate keeps enough of the baseline's annual return.

    A baseline that did not earn anything has no edge to conserve, and a fraction of a
    non-positive number is not a bar -- it is a free pass that gets easier the worse the
    baseline did. Such a comparison is refused rather than scored, the same way M39's
    compatibility ratio refuses a losing signal basis.
    """
    if baseline.annual is None or baseline.annual <= 0:
        return False
    if candidate.annual is None:
        return False
    return candidate.annual >= baseline.annual * MIN_CAGR_CONSERVATION


def passes(candidate: ArmResult, baseline: ArmResult) -> tuple[bool, tuple[Gate, ...]]:
    """Return whether the inverse-vol arm clears every declared gate, and what failed.

    Both breadths must pass independently; this judges one of them. Nothing here is softened if
    it fails: M41's closing condition was that a failure ends the research programme rather than
    opening a ninth family.
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
    if not cagr_conserved(candidate, baseline):
        failed.append(Gate.CAGR_NOT_CONSERVED)
    if candidate.capital_deficit > EXPOSURE_MATCH_TOLERANCE:
        failed.append(Gate.EXPOSURE_NOT_MATCHED)
    return not failed, tuple(failed)
