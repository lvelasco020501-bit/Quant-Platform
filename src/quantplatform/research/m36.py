"""M36 — does the combined portfolio keep its edge once Risk V2 actually moves the positions?

**Pre-declaration for phase 2, committed before a single phase-2 result existed.** Phase 1's
status is different and is stated first, because pretending otherwise would be the worst thing
this module could do.

**Phase 1 is a re-judgement, not a new measurement.** Both numbers it reads were produced by
M35's corrected probe and are already on disk: breadth 6 at 29.34% a year, 31.87% drawdown,
Calmar 0.92; breadth 12 at 35.21%, 25.50%, Calmar 1.38. What changes in M36 is the *pass
condition* -- it now asks only about breadths 6 and 12, where M35's asked about 3 as well and
failed on it.

That narrowing was made knowing which point failed, which is exactly the pattern seven
milestones of this project have been built to avoid. Two things are true about it and both
belong here. It rests on evidence rather than on preference: a full six markets were eligible on
**93.62% of bars**, a universe of three or fewer occurred on **3.13%** and only before
2018-03-31, so M35's condition was testing a regime that existed for the first six months of a
nine-year sample and never again. And it is still a gate loosened after seeing a result, so
phase 1 cannot be counted as independent confirmation of anything. It is recorded as what it is:
the user narrowing a gate they own, with a reason, and phase 1 therefore contributing no new
information.

**Phase 2 is the milestone.** Everything M30 through M35 measured ran on position timelines that
no stop had ever touched, and M34's own verification established that this matters: the certified
engine turns over 1.5 to 10.5 times as often as the signals alone, and because position state is
an input to these rules, a stop can even produce an entry the unstopped rule would never take.
So phase 2 replaces those timelines with **position intervals taken from ``BacktestEngine``**,
and the portfolio is rebuilt on them.

What Risk V2 does and does not get to decide, declared in advance so no difference found later
can be explained away:

* **Faithful.** Stops set and triggered, position state, the re-entries that state produces,
  order rejection, and the non-latching drawdown breakers -- all live, all deciding when a
  position exists and when it ends. A stop is a *price* distance, so whether it triggers depends
  on price alone: interval boundaries are what Risk V2 would produce at any position size.
* **Not faithful, and unfixable rather than unfixed.** Position *size*. The engine runs each
  sleeve-market pair as its own account holding the whole of it; the portfolio then imposes the
  declared weights. A per-pair run cannot know the portfolio's equity, so its drawdown breakers
  and its minimum-notional rejections see an account the portfolio's one-twelfth positions never
  have. That is the architectural limit M34 declared and it has not moved.
* **Released.** Latching. The reference policy M29 through M33 all used keeps the breakers and
  releases the latch, and M36 uses the same one so its numbers are comparable with every prior
  screen rather than with nothing. Latching can only ever remove exposure, so its absence biases
  phase 2 towards *higher* return and *higher* drawdown than a deployed configuration would give.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import COST_STRESS_MULTIPLIERS, MAX_SINGLE_YEAR_SHARE, MIN_CALMAR
from quantplatform.research.m30 import MAX_SINGLE_ASSET_SHARE, ONE_WAY_COST_BASIS_POINTS, OOS_START
from quantplatform.research.m32 import LIQUIDITY_WINDOW, UNIVERSE_SIZE
from quantplatform.research.m34 import (
    MAX_PER_ASSET,
    MAX_PER_SLEEVE_SHARE,
    SLEEVES,
    TIMEFRAME,
    Portfolio,
    pool_symbols,
    sleeves_of,
)
from quantplatform.research.m35 import Basis

__all__ = [
    "BREADTHS",
    "CANDIDATE",
    "COST_STRESS_MULTIPLIERS",
    "LIQUIDITY_WINDOW",
    "MAX_PER_ASSET",
    "MAX_PER_SLEEVE_SHARE",
    "MAX_SINGLE_ASSET_SHARE",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_CALMAR",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "OPERATIONAL_BREADTH_FLOOR",
    "SCREEN_MAX_DRAWDOWN",
    "SLEEVES",
    "TIMEFRAME",
    "UNIVERSE_SIZE",
    "Basis",
    "BreadthPoint",
    "Divergence",
    "FinalRobustness",
    "RiskV2Layer",
    "phase_one_passes",
    "pool_symbols",
    "sleeves_of",
    "survives",
]


CANDIDATE: Final[Portfolio] = Portfolio.COMBINED
"""M34's combined portfolio, unchanged. Its sleeves reach through M34 to M29's own probe
objects, so no period, lookback, threshold or weight is restated in this milestone either."""

OPERATIONAL_BREADTH_FLOOR: Final[int] = UNIVERSE_SIZE
"""The declared operating regime: this portfolio requires a universe of at least six markets.

Stated as a requirement of the design rather than discovered as a result, which is what makes
breadth 3 out of scope rather than merely inconvenient. A portfolio of six equally weighted
positions is not defined on a universe of three."""

BREADTHS: Final[tuple[int, ...]] = (UNIVERSE_SIZE, 12)
"""The two breadths phase 1 judges: the declared one and its double. Both were already measured
by M35; see this module's docstring for why that means phase 1 confirms nothing."""


class RiskV2Layer(StrEnum):
    """Every part of Risk V2, and whether phase 2 exercises it. Declared, not discovered."""

    STOPS = "stops"
    POSITION_STATE = "position_state"
    RE_ENTRIES = "re_entries"
    ORDER_REJECTION = "order_rejection"
    BREAKERS = "breakers"
    SIZING = "sizing"
    LATCHING = "latching"


EXERCISED: Final[frozenset[RiskV2Layer]] = frozenset(
    {
        RiskV2Layer.STOPS,
        RiskV2Layer.POSITION_STATE,
        RiskV2Layer.RE_ENTRIES,
        RiskV2Layer.ORDER_REJECTION,
        RiskV2Layer.BREAKERS,
    }
)
"""The layers that genuinely decide phase 2's positions."""

NOT_EXERCISED: Final[frozenset[RiskV2Layer]] = frozenset({RiskV2Layer.SIZING, RiskV2Layer.LATCHING})
"""Sizing, because a per-pair engine run cannot see the portfolio's equity; latching, because the
reference policy every prior screen used releases it. Both are stated in the module docstring
with the direction of the bias each introduces."""


class Divergence(DomainModel):
    """One measured difference between the signal basis and the certified basis."""

    measure: str
    on_signals: Decimal | None
    on_certified: Decimal | None
    """``None`` where a measure does not exist on one basis."""

    @property
    def relative(self) -> Decimal | None:
        """Return the certified basis as a multiple of the signal basis, where both exist."""
        if self.on_signals is None or self.on_certified is None or self.on_signals == 0:
            return None
        return self.on_certified / self.on_signals


class BreadthPoint(DomainModel):
    """One breadth judged against the phase-1 conditions."""

    breadth: int
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    out_of_sample_return: Decimal | None
    single_year_share: Decimal | None
    top_asset_share: Decimal | None
    top_sleeve_share: Decimal | None

    @property
    def passes(self) -> bool:
        """Return whether this breadth clears every phase-1 condition."""
        return (
            self.annual is not None
            and self.annual > 0
            and self.max_drawdown <= SCREEN_MAX_DRAWDOWN
            and self.calmar_ratio is not None
            and self.calmar_ratio >= MIN_CALMAR
            and self.annual_at_double_cost is not None
            and self.annual_at_double_cost > 0
            and self.annual_at_triple_cost is not None
            and self.annual_at_triple_cost > 0
            and self.out_of_sample_return is not None
            and self.out_of_sample_return > 0
            and self.single_year_share is not None
            and self.single_year_share <= MAX_SINGLE_YEAR_SHARE
            and self.top_asset_share is not None
            and self.top_asset_share <= MAX_SINGLE_ASSET_SHARE
            and (self.top_sleeve_share is None or self.top_sleeve_share <= MAX_PER_SLEEVE_SHARE)
        )


def phase_one_passes(points: tuple[BreadthPoint, ...]) -> bool:
    """Return whether both declared breadths clear phase 1.

    Phase 2 runs only if this is true. It reads the two breadths M36 declares and no others, so
    a run that measured only one of them has not answered the question.
    """
    return {point.breadth for point in points} == set(BREADTHS) and all(
        point.passes for point in points
    )


class FinalRobustness(StrEnum):
    """Every declared way the candidate can fail at the end of this line."""

    BREADTH_FAILED = "breadth_failed"
    NEGATIVE_AFTER_COSTS = "negative_after_costs"
    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    SINGLE_YEAR = "single_year"
    SINGLE_ASSET = "single_asset"
    SINGLE_SLEEVE = "single_sleeve"
    COST_FRAGILE = "cost_fragile"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"
    SIMULATOR_DEPENDENT = "simulator_dependent"
    """The certified basis failed a condition the simplified one passed: the result depended on
    the simulator rather than on the rules."""


class Final36(DomainModel):
    """Everything the final gate reads, measured on the certified basis."""

    basis: Basis
    breadths_passed: bool
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    single_year_share: Decimal | None
    top_asset_share: Decimal | None
    top_sleeve_share: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    out_of_sample_return: Decimal | None
    signal_basis_passed: bool
    """Whether the simplified basis cleared the same conditions. Used only to name a failure
    correctly: a candidate that fails on both bases fails on its own merits, while one that
    passes on signals and fails with Risk V2 was depending on the simulator."""


def survives(measured: Final36) -> tuple[bool, tuple[FinalRobustness, ...]]:
    """Return whether the candidate clears the final gate, and what it failed.

    Every threshold is the one its own milestone set, reused rather than restated. The last
    condition is what M36 exists for and it fires only in one direction: a result that holds on
    the simplified basis and breaks once Risk V2 moves the positions was a property of the
    simulator, and saying so is the whole point of having run the certified engine.

    Returns:
        ``(True, ())`` when everything passed, otherwise ``(False, reasons)`` with the reasons in
        declaration order so two failures always report in the same sequence.
    """
    m = measured
    failures: list[FinalRobustness] = []
    if not m.breadths_passed:
        failures.append(FinalRobustness.BREADTH_FAILED)
    if m.annual is None or m.annual <= 0:
        failures.append(FinalRobustness.NEGATIVE_AFTER_COSTS)
    if m.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failures.append(FinalRobustness.DRAWDOWN)
    if m.calmar_ratio is None or m.calmar_ratio < MIN_CALMAR:
        failures.append(FinalRobustness.LOW_CALMAR)
    if m.single_year_share is None or m.single_year_share > MAX_SINGLE_YEAR_SHARE:
        failures.append(FinalRobustness.SINGLE_YEAR)
    if m.top_asset_share is None or m.top_asset_share > MAX_SINGLE_ASSET_SHARE:
        failures.append(FinalRobustness.SINGLE_ASSET)
    if m.top_sleeve_share is not None and m.top_sleeve_share > MAX_PER_SLEEVE_SHARE:
        failures.append(FinalRobustness.SINGLE_SLEEVE)
    if (
        m.annual_at_double_cost is None
        or m.annual_at_double_cost <= 0
        or m.annual_at_triple_cost is None
        or m.annual_at_triple_cost <= 0
    ):
        failures.append(FinalRobustness.COST_FRAGILE)
    if m.out_of_sample_return is None or m.out_of_sample_return <= 0:
        failures.append(FinalRobustness.OUT_OF_SAMPLE_NEGATIVE)
    if failures and m.signal_basis_passed and m.basis is Basis.CERTIFIED:
        failures.append(FinalRobustness.SIMULATOR_DEPENDENT)
    return (not failures, tuple(failures))
