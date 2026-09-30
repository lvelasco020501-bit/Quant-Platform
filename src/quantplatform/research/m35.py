"""M35 — validating M34's combined portfolio without searching for anything.

**Pre-declaration. Written and committed before a single M35 result was seen.**

M34 produced the first genuinely encouraging portfolio result this project has had. B2 and
regime_trend combined ran at 29.34% a year with a 31.87% drawdown and a Calmar of 0.92 -- a lower
drawdown than *either* sleeve alone, the best Calmar of the milestone, roughly double the
risk-adjusted return of holding everything passively, and seven of eight declared conditions
cleared including concentration by year, by market and by strategy. It failed one thing: a
breadth sensitivity probe.

M35 does not look for a better portfolio. It attacks the two reasons the M34 result cannot be
trusted yet.

**First, the probe was a bad instrument, and the diagnosis in M34's report was too kind to it.**
That report said the breadth-3 neighbour ran "books twice as large", implying the extra drawdown
was mostly arithmetic. Measuring it properly says otherwise:

    breadth   per-signal   mean deployed   deployed when invested   mean positions
       3        16.67%         23.4%                43.1%                2.6
       6         8.33%         21.7%                33.0%                4.0
      12         4.17%         19.8%                25.8%                6.2

Aggregate deployed capital rises only about eight percent going from breadth 6 to breadth 3, not
double. So **most of the breadth-3 drawdown was concentration, not size** -- which makes M34's
failure more meaningful than its own report allowed, and makes a corrected probe the right thing
to build rather than an excuse to look for.

**The corrected probe holds the aggregate constant and varies only how it is divided.** At each
bar the neighbour carries the *same deployed weight the declared portfolio carried at that bar*,
spread equally across whatever signals the narrower or wider universe admits. Total capital,
aggregate exposure and therefore total portfolio risk are identical by construction; the only
thing that changes is the number of positions the same money sits in. That is the experiment
M34's probe should have been, and it introduces no constant of its own -- the exposure path is
taken from the declared portfolio rather than chosen.

One property of that construction is worth stating because it is also a check: at breadth 6 the
corrected probe must reproduce the declared portfolio, since the per-asset cap of one sixth is
never binding when two sleeves each hold one twelfth. Equal to within Decimal rounding rather
than bit-identical -- summing twelfths and dividing back rounds differently from writing a
twelfth down, and the difference lands in the 28th digit. If the breadth-6 point differs by more
than that, the normalisation is wrong.

**Second, nothing in M34 let Risk V2's stop touch a position.** M34's own verification established
that this matters: the engine turns over 1.5 to 10.5 times as often as the signals alone, and
position state is an input to these rules, so a stop can even create an entry the unstopped rule
would never take. A portfolio built on unstopped timelines is therefore a portfolio of different
strategies than the ones running in paper. Phase 2 replaces those timelines with **position
intervals taken from the certified engine**, where the stop, the breakers and order rejection are
all live, and compares the two portfolios pass for pass.

What Phase 2 cannot do is make the comparison exact, and the reason is declared rather than
discovered later: the engine runs each sleeve-market pair as its own account at full capital, so
its risk budget and drawdown breakers operate at a scale the portfolio's one-twelfth positions
never see. Every material difference is attributed to a mechanism instead of being bounded by a
tolerance nobody could have justified in advance.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.enums import Timeframe
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
    "SCREEN_MAX_DRAWDOWN",
    "SLEEVES",
    "TIMEFRAME",
    "UNIVERSE_SIZE",
    "Basis",
    "Phase1",
    "Validated",
    "ValidationRobustness",
    "pool_symbols",
    "sleeves_of",
    "survives",
]


# --- What is validated ----------------------------------------------------------------------------

CANDIDATE: Final[Portfolio] = Portfolio.COMBINED
"""The only thing M35 may examine: M34's combined portfolio, unchanged.

Its sleeves reach through M34 to M29's own probe objects, so no period, lookback, threshold or
weight is restated anywhere in this milestone either."""

BREADTHS: Final[tuple[int, ...]] = (3, UNIVERSE_SIZE, 12)
"""The three universe breadths the corrected probe walks, declared as the user named them.

Six is the declared portfolio and must reproduce it exactly under the normalisation; three and
twelve are the neighbours. The question is whether the advantage needs exactly six markets."""


class Basis(StrEnum):
    """Where a portfolio's position timelines came from. Both are run and compared."""

    SIGNALS = "signal_timelines"
    """The frozen rules' own entry and exit logic, with no stop. M34's basis."""
    CERTIFIED = "certified_engine_positions"
    """Position intervals taken from ``BacktestEngine`` runs, where Risk V2's stop, its
    breakers and order rejection are all live and do change the positions."""


# --- The gate -------------------------------------------------------------------------------------


class ValidationRobustness(StrEnum):
    """Every declared way the candidate can fail. Order is the reporting order."""

    NEGATIVE_AFTER_COSTS = "negative_after_costs"
    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    SINGLE_YEAR = "single_year"
    SINGLE_ASSET = "single_asset"
    SINGLE_SLEEVE = "single_sleeve"
    COST_FRAGILE = "cost_fragile"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"
    BREADTH_DEPENDENT = "breadth_dependent"
    """The corrected probe: at constant aggregate exposure, some breadth broke the candidate."""
    RISK_V2_DESTROYS_IT = "risk_v2_destroys_it"
    """The certified-engine basis failed a condition the signal basis passed."""


class Phase1(DomainModel):
    """What the corrected breadth probe produced, one entry per declared breadth."""

    breadth: int
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    mean_deployed: Decimal
    """Average share of the account actually at work, which the probe holds constant."""
    positions_when_invested: Decimal | None
    bars_forced_to_cash: int
    """Bars where the declared portfolio was invested and this breadth had no eligible signal,
    so the exposure could not be matched. Reported because it is the one way the construction
    cannot hold the aggregate equal, and pretending otherwise would hide it."""

    @property
    def stable(self) -> bool:
        """Return whether this breadth keeps the already-declared drawdown and Calmar bars."""
        return (
            self.max_drawdown <= SCREEN_MAX_DRAWDOWN
            and self.calmar_ratio is not None
            and self.calmar_ratio >= MIN_CALMAR
        )


class Validated(DomainModel):
    """Everything the gate reads about the candidate, on one basis."""

    basis: Basis
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    single_year_share: Decimal | None
    top_asset_share: Decimal | None
    top_sleeve_share: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    out_of_sample_return: Decimal | None
    breadths_stable: bool
    """Whether every declared breadth cleared the drawdown and Calmar bars at constant exposure."""
    certified_basis_holds: bool
    """Whether the certified-engine basis also cleared them. ``True`` on the signal basis itself,
    which is what keeps this from double-counting: a basis is not asked to validate itself."""


def survives(measured: Validated) -> tuple[bool, tuple[ValidationRobustness, ...]]:
    """Return whether the candidate clears every declared condition, and what it failed.

    The first eight conditions are M34's, reused at the same thresholds rather than restated.
    The last two are what M35 exists for: stability across breadth at constant aggregate
    exposure, and survival of Risk V2 actually moving the positions.

    Returns:
        ``(True, ())`` when everything passed, otherwise ``(False, reasons)`` with the reasons in
        declaration order so two failures always report in the same sequence.
    """
    m = measured
    failures: list[ValidationRobustness] = []
    if m.annual is None or m.annual <= 0:
        failures.append(ValidationRobustness.NEGATIVE_AFTER_COSTS)
    if m.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failures.append(ValidationRobustness.DRAWDOWN)
    if m.calmar_ratio is None or m.calmar_ratio < MIN_CALMAR:
        failures.append(ValidationRobustness.LOW_CALMAR)
    if m.single_year_share is None or m.single_year_share > MAX_SINGLE_YEAR_SHARE:
        failures.append(ValidationRobustness.SINGLE_YEAR)
    if m.top_asset_share is None or m.top_asset_share > MAX_SINGLE_ASSET_SHARE:
        failures.append(ValidationRobustness.SINGLE_ASSET)
    if m.top_sleeve_share is not None and m.top_sleeve_share > MAX_PER_SLEEVE_SHARE:
        failures.append(ValidationRobustness.SINGLE_SLEEVE)
    if (
        m.annual_at_double_cost is None
        or m.annual_at_double_cost <= 0
        or m.annual_at_triple_cost is None
        or m.annual_at_triple_cost <= 0
    ):
        failures.append(ValidationRobustness.COST_FRAGILE)
    if m.out_of_sample_return is None or m.out_of_sample_return <= 0:
        failures.append(ValidationRobustness.OUT_OF_SAMPLE_NEGATIVE)
    if not m.breadths_stable:
        failures.append(ValidationRobustness.BREADTH_DEPENDENT)
    if not m.certified_basis_holds:
        failures.append(ValidationRobustness.RISK_V2_DESTROYS_IT)
    return (not failures, tuple(failures))


def phase_one_passes(entries: tuple[Phase1, ...]) -> bool:
    """Return whether every declared breadth held up at constant aggregate exposure.

    The stop condition for phase 1: if this is false, phase 2 is not run and the line closes,
    because a portfolio whose advantage needs exactly six markets is a portfolio fitted to a
    number nobody could have known in advance.
    """
    return len(entries) == len(BREADTHS) and all(entry.stable for entry in entries)


REFERENCE_BREADTH: Final[int] = UNIVERSE_SIZE
"""The breadth whose bar-by-bar deployed weight every neighbour is held to, and which must
reproduce M34's portfolio C exactly."""

TIMEFRAME_CHECK: Final[Timeframe] = TIMEFRAME
"""4h, carried from M34 so the two milestones cannot drift apart on it."""
