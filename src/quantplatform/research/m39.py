"""M39 — look for an edge that survives Risk V2 from its first measurement.

**Pre-declaration, committed before a single M39 result existed.** Research only. Nothing is
deployed; production Risk, production execution, the paper sessions and the VPS are untouched.
Risk V2 is **not modified** -- that is the point of the milestone, not a constraint on it. No
Risk V3 or V4 is designed here, and no stop is optimised.

**Why this milestone exists.** Every edge this project has screened was found on signal
timelines and then handed to Risk V2, which destroyed it: M36 measured 29.34% a year falling to
9.01% with the drawdown rising, and M37 traced that to the protective stop closing 92.7% of all
positions. M38 then showed that loosening the stop trades the churn for a worse drawdown. The
remaining move is not another risk layer. It is to stop screening on signals at all: a candidate
here is measured through ``Market -> Strategy -> Risk V2 -> Execution -> Portfolio`` from its
first run, and a rule that only works without a stop never becomes a candidate.

**The signal basis is a diagnostic, never a gate.** It is measured, because the ratio between
the two is the quantity this milestone is about, but nothing passes or fails on it. A strategy
whose signal basis is spectacular and whose risk-managed result is not has told us it is the
wrong strategy, which is exactly the lesson M30 through M38 paid for.

**The mechanism hypothesis, stated so it can be wrong.** Risk V2 derives a 600 bps stop from the
entry price. A rule that enters at an extended price puts that stop 6% below a level the market
has just run away from, where ordinary retracement reaches it; a rule that enters after a
pullback puts the same 6% below a level the market has just defended. If that reasoning is
right, the two families that enter on weakness should keep materially more of their signal
performance than the one that enters on strength. **MC is therefore the control, and the
prediction is that it scores worst on the compatibility ratio.** If MC wins, the hypothesis is
wrong and this milestone's framing was wrong with it.

**No parameter here is new.** Every window is M33's 1D scale -- the only 1D convention this
project has -- and every second variant is the first with one window doubled, which is M22's
``Horizon.DOUBLED`` convention used by M29 and M30 before this. There is no grid search, no
per-asset tuning, and no parameter may move after a result is seen.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.enums import Timeframe
from quantplatform.core.models.base import DomainModel
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN, SCREEN_MIN_TRADES
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_ASSETS_POSITIVE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
    MIN_YEARS_POSITIVE_SHARE,
)
from quantplatform.research.m30 import (
    ASSETS_M30,
    MAX_SINGLE_ASSET_SHARE,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
)
from quantplatform.research.m33 import ENTRY_LOOKBACK, EXIT_LOOKBACK, LONG_WINDOW, SHORT_WINDOW

__all__ = [
    "ASSETS_M39",
    "COST_STRESS_MULTIPLIERS",
    "ENTRY_LOOKBACK",
    "EXIT_LOOKBACK",
    "LONG_WINDOW",
    "MAX_SINGLE_ASSET_SHARE",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_ASSETS_POSITIVE",
    "MIN_CALMAR",
    "MIN_COMPATIBILITY_RATIO",
    "MIN_NEIGHBOUR_PROFIT_FACTOR",
    "MIN_YEARS_POSITIVE_SHARE",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "PREDICTED_WORST_COMPATIBILITY",
    "SCREEN_MAX_DRAWDOWN",
    "SCREEN_MIN_TRADES",
    "SHORT_WINDOW",
    "TIMEFRAME",
    "VARIANTS_M39",
    "Family",
    "Gate",
    "Measured",
    "Variant",
    "compatibility_ratio",
    "survives",
]


TIMEFRAME: Final[Timeframe] = Timeframe.D1
"""1D, and only 1D. A survivor may advance to 4H; nothing here goes to 1H."""

ASSETS_M39: Final[tuple[str, ...]] = ASSETS_M30
"""The six markets, taken by reference from M30 rather than retyped."""


class Family(StrEnum):
    """The three families, each entering at a structurally different place."""

    PULLBACK = "pullback"
    """Long-horizon momentum up, short-horizon momentum down: buying weakness inside strength."""

    RETEST = "retest"
    """A breakout that already happened, entered on the way back rather than at the extreme."""

    CONFIRMED = "confirmed"
    """Momentum agreeing across two horizons: buying strength. The control."""


PREDICTED_WORST_COMPATIBILITY: Final[Family] = Family.CONFIRMED
"""Which family the mechanism hypothesis says will keep the least of its signal performance.

Recorded before any run so the prediction can be scored. ``CONFIRMED`` enters while both
horizons are positive, which is an extended price, so Risk V2's stop sits where retracement
reaches it. If this family instead scores *best*, the hypothesis behind M39's family choice is
wrong and the milestone says so."""


class Variant(DomainModel):
    """One pre-declared rule: a strategy id, its parameters, and which family it belongs to."""

    key: str
    family: Family
    strategy_id: str
    params: tuple[tuple[str, str], ...]
    """Parameters as the registry takes them, so a run cannot be filed under other numbers."""

    doubled: bool = False
    """Whether this is the first variant with one window doubled, by M22's convention."""


VARIANTS_M39: Final[tuple[Variant, ...]] = (
    Variant(
        key="PB1",
        family=Family.PULLBACK,
        strategy_id="trend_pullback",
        params=(("trend_window", str(LONG_WINDOW)), ("pullback_window", str(SHORT_WINDOW))),
    ),
    Variant(
        key="PB2",
        family=Family.PULLBACK,
        strategy_id="trend_pullback",
        params=(("trend_window", str(LONG_WINDOW)), ("pullback_window", str(SHORT_WINDOW * 2))),
        doubled=True,
    ),
    Variant(
        key="RT1",
        family=Family.RETEST,
        strategy_id="breakout_retest",
        params=(
            ("trend_window", str(LONG_WINDOW)),
            ("breakout_lookback", str(ENTRY_LOOKBACK)),
            ("recent_lookback", str(EXIT_LOOKBACK)),
            ("exit_lookback", str(EXIT_LOOKBACK)),
        ),
    ),
    Variant(
        key="RT2",
        family=Family.RETEST,
        strategy_id="breakout_retest",
        params=(
            ("trend_window", str(LONG_WINDOW)),
            ("breakout_lookback", str(ENTRY_LOOKBACK * 2)),
            ("recent_lookback", str(EXIT_LOOKBACK * 2)),
            ("exit_lookback", str(EXIT_LOOKBACK)),
        ),
        doubled=True,
    ),
    Variant(
        key="MC1",
        family=Family.CONFIRMED,
        strategy_id="dual_horizon_momentum",
        params=(("fast_window", str(SHORT_WINDOW)), ("slow_window", str(LONG_WINDOW))),
    ),
    Variant(
        key="MC2",
        family=Family.CONFIRMED,
        strategy_id="dual_horizon_momentum",
        params=(("fast_window", str(SHORT_WINDOW * 2)), ("slow_window", str(LONG_WINDOW))),
        doubled=True,
    ),
)
"""Six variants: three families, two each, no more. Every window is M33's 1D scale and every
second variant doubles one of them by M22's convention. The same parameters run on every
market."""


MIN_COMPATIBILITY_RATIO: Final[Decimal] = Decimal("0.50")
"""How much of its signal-basis annual return a candidate must keep under Risk V2.

The milestone's own threshold and the only number M39 adds. Half, and the reasoning is a
measurement rather than a preference: M36's combined portfolio kept 31% of its signal CAGR and
was rejected; M37's best structural alternative kept 67% and was accepted on six markets. Half
sits between the thing that failed and the thing that did not, and it is set before any M39
candidate was run.

A ratio is only meaningful where the signal basis made money. Where it did not, the candidate is
judged on its absolute gates alone and the ratio is reported as undefined rather than as a pass.
"""


class Gate(StrEnum):
    """Why a variant does not survive. Declared before any result existed."""

    NO_TRADES = "no_trades"
    NEGATIVE = "negative"
    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    WEAK_PROFIT_FACTOR = "weak_profit_factor"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"
    COST_FRAGILE = "cost_fragile"
    TOO_FEW_ASSETS_POSITIVE = "too_few_assets_positive"
    ASSET_CONCENTRATION = "asset_concentration"
    SINGLE_YEAR_CONCENTRATION = "single_year_concentration"
    YEARS_INCONSISTENT = "years_inconsistent"
    RISK_DESTROYS_EDGE = "risk_destroys_edge"


class Measured(DomainModel):
    """One variant measured through the full chain, with no judgement applied."""

    key: str
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    profit_factor: Decimal | None
    trades: int
    turnover: Decimal
    fees: Decimal
    forced_exits: int
    re_entries: int
    held_share_of_wanted: Decimal | None
    out_of_sample_return: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    assets_positive: int
    top_asset_share: Decimal | None
    single_year_share: Decimal | None
    years_positive_share: Decimal | None
    signal_annual: Decimal | None
    """The same rule's annual return with no risk layer. Diagnostic only; never a gate."""


def compatibility_ratio(measured: Measured) -> Decimal | None:
    """Return risk-managed annual return over signal annual return, where that is meaningful.

    ``None`` where the signal basis did not make money, because a ratio against a loss says
    nothing: a candidate that loses less than its unmanaged form has not kept an edge, it has
    lost less. Such a candidate is judged on its absolute gates alone.
    """
    if measured.signal_annual is None or measured.signal_annual <= 0:
        return None
    if measured.annual is None:
        return Decimal(0)
    return measured.annual / measured.signal_annual


def survives(measured: Measured) -> tuple[bool, tuple[Gate, ...]]:
    """Return whether a variant clears every declared gate, and which it failed.

    Every threshold is inherited: the trade floor and drawdown cap are M22's, the Calmar floor,
    profit-factor floor, year-consistency share, single-year cap and asset floor are M29's, the
    asset-concentration cap is M30's, and the cost multipliers and out-of-sample window are
    M29's and M30's. Only the compatibility ratio is M39's own.
    """
    failed: list[Gate] = []
    if measured.trades < SCREEN_MIN_TRADES:
        failed.append(Gate.NO_TRADES)
    if measured.annual is None or measured.annual <= 0:
        failed.append(Gate.NEGATIVE)
    if measured.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failed.append(Gate.DRAWDOWN)
    if measured.calmar_ratio is None or measured.calmar_ratio < MIN_CALMAR:
        failed.append(Gate.LOW_CALMAR)
    if measured.profit_factor is None or measured.profit_factor < MIN_NEIGHBOUR_PROFIT_FACTOR:
        failed.append(Gate.WEAK_PROFIT_FACTOR)
    if measured.out_of_sample_return is None or measured.out_of_sample_return <= 0:
        failed.append(Gate.OUT_OF_SAMPLE_NEGATIVE)
    if (
        measured.annual_at_double_cost is None
        or measured.annual_at_double_cost <= 0
        or measured.annual_at_triple_cost is None
        or measured.annual_at_triple_cost <= 0
    ):
        failed.append(Gate.COST_FRAGILE)
    if measured.assets_positive < MIN_ASSETS_POSITIVE:
        failed.append(Gate.TOO_FEW_ASSETS_POSITIVE)
    if measured.top_asset_share is not None and measured.top_asset_share > MAX_SINGLE_ASSET_SHARE:
        failed.append(Gate.ASSET_CONCENTRATION)
    if (
        measured.single_year_share is not None
        and measured.single_year_share > MAX_SINGLE_YEAR_SHARE
    ):
        failed.append(Gate.SINGLE_YEAR_CONCENTRATION)
    if (
        measured.years_positive_share is not None
        and measured.years_positive_share < MIN_YEARS_POSITIVE_SHARE
    ):
        failed.append(Gate.YEARS_INCONSISTENT)
    ratio = compatibility_ratio(measured)
    if ratio is not None and ratio < MIN_COMPATIBILITY_RATIO:
        failed.append(Gate.RISK_DESTROYS_EDGE)
    return not failed, tuple(failed)
