"""M34 — a portfolio of edges this project already has, rather than a search for a new one.

**Pre-declaration. Written and committed before a single result was seen.**

Five research lines have now closed. M29 exhausted seven single-asset families. M30-M32 followed
rotation to a candidate that cleared every gate on six hand-picked markets and then broke on a
survivorship-corrected universe. M33 asked about the second moment and found a family that is
genuinely low-risk, genuinely low-return, and lands within a point of where the incumbent already
is. The structural finding underneath all of them is the same and it is worth repeating here,
because it is what makes this milestone the obvious next thing to try and also what bounds what
it can achieve:

    Every single-asset edge this platform has measured at 1d pays roughly one percent a year
    after costs, at a drawdown of two to four percent.

M34 invents nothing. It takes the two rules the project already trusts — the breakout
configuration running in paper, and the incumbent it was measured against — and asks whether
several modest, partly uncorrelated edges add up to a better portfolio than any of them alone.

**What is frozen, and frozen by import.** :data:`SLEEVES` reaches into
:data:`~quantplatform.research.m29.CANDIDATES_M29` for the two probes M29 already screened, so no
lookback, no threshold and no period is restated anywhere in this module. B2 is
``breakout_trend`` at 40/20/400, which is the configuration live on the VPS; RT is
``regime_trend`` at 72/72/0.30, M22's benchmark.

**What is new, and it is only one thing: capital allocation.** The signals come from the frozen
strategies driven by the production feature pipeline. The per-asset eligibility comes from M32's
point-in-time liquidity rule, unchanged. What M34 adds is the rule deciding how much of the
account stands behind each active signal, and that rule is arithmetic rather than a choice:

    one active signal gets ``1 / (universe size x number of sleeves)`` of the account, no asset
    may hold more than ``1 / universe size`` in total, and whatever is not allocated is cash.

Both fractions are derived from the universe breadth M32 declared, so M34 introduces no number
of its own. The fixed denominator is deliberate and is what makes "the rest in cash" mean
something: an allocation of one over the *active* count would be fully invested the moment a
single signal fired, which is a different and much riskier portfolio than the one asked for.

**What this portfolio is built on, and the gap that comes with it.** Declared here before any
portfolio result was produced, and established by measurement rather than assumed.

A portfolio with its own allocator cannot be run on ``BacktestEngine``: that engine is a
single-account, single-strategy backtester which sizes each position from a stop distance and a
risk budget assuming the whole account is its own. Running each pair at full capital and then
scaling to one twelfth is not the same portfolio -- the drawdown breakers would fire at different
times. M32's phase-2 pre-declaration already said as much about a weight-based overlay.

So the sleeves' position timelines come from :mod:`~quantplatform.research.sleeve`, which drives
the **same frozen strategy objects** through the **same production feature pipeline**, and
``scripts/m34_verify.py`` holds that driver to the engine's own trade list across twelve series.
The result of that check is part of this declaration:

* **321 of the engine's 322 trades open inside one of the driver's long stretches.** The two
  agree about when the rules speak.
* **They disagree about what happens next, and the disagreement is large.** Risk V2's stop takes
  the engine flat part-way through a stretch the strategy still wants to hold, and the strategy
  then re-enters on a later breakout inside it. The engine turns over **1.5 to 10.5 times** as
  often as the signals alone -- a median near 3.
* **The one exception is instructive rather than noise.** B2 on ETH opens a trade at
  2017-11-29 20:00, one bar after the driver's stretch ended. The engine had been stopped out
  earlier, so the strategy saw a flat position and evaluated its *entry* branch where the driver,
  still long, evaluated its *exit* branch. Position state is an input to these rules, so a stop
  does not merely chop a stretch -- it can produce an entry the unstopped rule would never take.

**Therefore the portfolios measured here are portfolios of the rules' own entry and exit logic,
with Risk V2's stop not modelled.** Every arm and every benchmark is built by the identical
machinery, so the question this milestone asks -- does combining beat each alone -- is answered on
a consistent basis. The absolute CAGR and drawdown are *not* the deployed strategies' numbers and
are not offered as such. Certified-engine reproduction is the user's own declared next gate, to
be reached only by something that passes here.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.enums import Timeframe
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN
from quantplatform.research.m29 import (
    CANDIDATES_M29,
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_CALMAR,
    Probe,
    cagr,
    calmar,
)
from quantplatform.research.m30 import MAX_SINGLE_ASSET_SHARE, ONE_WAY_COST_BASIS_POINTS, OOS_START
from quantplatform.research.m32 import LIQUIDITY_WINDOW, UNIVERSE_SIZE, pool_symbols

__all__ = [
    "COST_STRESS_MULTIPLIERS",
    "LIQUIDITY_WINDOW",
    "MAX_PER_ASSET",
    "MAX_PER_SLEEVE_SHARE",
    "MAX_SINGLE_ASSET_SHARE",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_CALMAR",
    "NEIGHBOUR_UNIVERSE_SIZES",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "PORTFOLIOS",
    "SCREEN_MAX_DRAWDOWN",
    "SLEEVES",
    "SLEEVE_KEYS",
    "TIMEFRAME",
    "UNIVERSE_SIZE",
    "Combined",
    "Portfolio",
    "PortfolioRobustness",
    "cagr",
    "calmar",
    "pool_symbols",
    "survives",
    "weight_per_signal",
]


# --- What is frozen -------------------------------------------------------------------------------

SLEEVE_KEYS: Final[tuple[str, ...]] = ("B2", "G1")
"""M29's keys for the two rules M34 is allowed to combine.

``B2`` is ``breakout_trend`` at 40/20/400 -- the configuration running in paper on the VPS, and
the only rule this project has ever promoted. ``G1`` is ``regime_trend`` at 72/72/0.30, the
benchmark M22 measured every candidate against and which M24 then showed failing in markets
complementary to B2's. That complementarity is the entire reason these two and no others: a
portfolio of two rules that fail in the same places would add nothing but fees."""

SLEEVES: Final[tuple[Probe, ...]] = tuple(
    probe for key in SLEEVE_KEYS for probe in CANDIDATES_M29 if probe.key == key
)
"""The two rules themselves, taken from M29 by reference rather than restated.

This is what makes "do not change the signals" checkable instead of promised: there is no second
copy of a lookback, a period or a threshold in this module, so none can drift. A test asserts
these are the same objects M29 screened."""

TIMEFRAME: Final[Timeframe] = Timeframe.H4
"""4h first, by instruction. 1h is excluded outright rather than deferred."""


class Portfolio(StrEnum):
    """The three portfolios. Declaration order is the reporting order."""

    B2_ONLY = "b2_multi_asset"
    REGIME_ONLY = "regime_trend_multi_asset"
    COMBINED = "b2_plus_regime_trend"


PORTFOLIOS: Final[dict[Portfolio, tuple[str, ...]]] = {
    Portfolio.B2_ONLY: ("B2",),
    Portfolio.REGIME_ONLY: ("G1",),
    Portfolio.COMBINED: SLEEVE_KEYS,
}
"""Which sleeves each portfolio holds. A and B are the same machinery with one sleeve, which is
what makes the comparison against C a comparison of *combining* rather than of two designs."""


# --- The allocation, derived rather than chosen ---------------------------------------------------

MAX_PER_ASSET: Final[Decimal] = Decimal(1) / Decimal(UNIVERSE_SIZE)
"""The most of the account any single market may hold, counting every sleeve long in it.

One sixth, because six is the universe breadth M32 declared and an equal share of that universe
is the natural cap. Derived, so M34 adds no number of its own."""

MAX_PER_SLEEVE_SHARE: Final[Decimal] = MAX_SINGLE_ASSET_SHARE
"""Most of the net profit one sleeve may account for in a combined portfolio.

The same 0.60 M30 set for per-asset concentration, reused deliberately: "does one component
explain the result" is the same question about a different axis, so it gets the same answer
rather than a new one. Evaluated only for :attr:`Portfolio.COMBINED` -- a single-sleeve portfolio
trivially owns all of its own profit, and failing it for that would be meaningless."""

NEIGHBOUR_UNIVERSE_SIZES: Final[tuple[int, ...]] = (UNIVERSE_SIZE // 2, UNIVERSE_SIZE * 2)
"""The sensitivity probes: half and double the universe breadth, by the same mechanical rule that
produced M22's second variants and every neighbour since.

Breadth is the right axis to probe because it is the only thing M34 chose. Probing the signals'
own parameters would be probing M29's work, not this milestone's, and the signals are frozen."""


def weight_per_signal(*, sleeves: int, universe: int = UNIVERSE_SIZE) -> Decimal:
    """Return the share of the account one active signal receives.

    ``1 / (universe x sleeves)``, so a portfolio is fully invested only when every sleeve is long
    in every eligible market at once, and is in cash to exactly the extent that it is not. No
    configuration of signals can ever exceed the account.
    """
    return Decimal(1) / (Decimal(universe) * Decimal(sleeves))


# --- The gate -------------------------------------------------------------------------------------


class PortfolioRobustness(StrEnum):
    """Every declared way a portfolio can fail. Order is the reporting order."""

    NEGATIVE_AFTER_COSTS = "negative_after_costs"
    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    SINGLE_YEAR = "single_year"
    SINGLE_ASSET = "single_asset"
    SINGLE_SLEEVE = "single_sleeve"
    COST_FRAGILE = "cost_fragile"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"
    FRAGILE_TO_BREADTH = "fragile_to_breadth"


class Combined(DomainModel):
    """Everything the gate reads about one portfolio, gathered from its runs."""

    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    single_year_share: Decimal | None
    top_asset_share: Decimal | None
    top_sleeve_share: Decimal | None
    """Share of net profit the best-contributing sleeve accounts for; ``None`` for a
    single-sleeve portfolio, where the question does not arise."""
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    out_of_sample_return: Decimal | None
    neighbours_positive: bool
    """Whether every declared breadth neighbour stayed profitable after costs."""
    neighbours_within_drawdown: bool
    """Whether every declared breadth neighbour kept its drawdown under the declared cap."""


def survives(measured: Combined) -> tuple[bool, tuple[PortfolioRobustness, ...]]:
    """Return whether a portfolio clears every declared gate, and what it failed.

    Sensitivity is expressed with conditions that were already declared, applied to the
    neighbours, rather than with a new threshold: a breadth that halves or doubles must leave the
    portfolio profitable after costs and inside the drawdown cap. Inventing a fresh number for
    the neighbourhood would be exactly the kind of after-the-fact criterion this gate exists to
    exclude.

    Returns:
        ``(True, ())`` when everything passed, otherwise ``(False, reasons)`` with the reasons in
        declaration order so two failures always report in the same sequence.
    """
    m = measured
    failures: list[PortfolioRobustness] = []
    if m.annual is None or m.annual <= 0:
        failures.append(PortfolioRobustness.NEGATIVE_AFTER_COSTS)
    if m.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failures.append(PortfolioRobustness.DRAWDOWN)
    if m.calmar_ratio is None or m.calmar_ratio < MIN_CALMAR:
        failures.append(PortfolioRobustness.LOW_CALMAR)
    if m.single_year_share is None or m.single_year_share > MAX_SINGLE_YEAR_SHARE:
        failures.append(PortfolioRobustness.SINGLE_YEAR)
    if m.top_asset_share is None or m.top_asset_share > MAX_SINGLE_ASSET_SHARE:
        failures.append(PortfolioRobustness.SINGLE_ASSET)
    if m.top_sleeve_share is not None and m.top_sleeve_share > MAX_PER_SLEEVE_SHARE:
        failures.append(PortfolioRobustness.SINGLE_SLEEVE)
    if (
        m.annual_at_double_cost is None
        or m.annual_at_double_cost <= 0
        or m.annual_at_triple_cost is None
        or m.annual_at_triple_cost <= 0
    ):
        failures.append(PortfolioRobustness.COST_FRAGILE)
    if m.out_of_sample_return is None or m.out_of_sample_return <= 0:
        failures.append(PortfolioRobustness.OUT_OF_SAMPLE_NEGATIVE)
    if not (m.neighbours_positive and m.neighbours_within_drawdown):
        failures.append(PortfolioRobustness.FRAGILE_TO_BREADTH)
    return (not failures, tuple(failures))


class Benchmark(StrEnum):
    """What a portfolio is measured against. Reported always, and never a gate."""

    B2_BTC = "b2_btc_alone"
    REGIME_BTC = "regime_trend_btc_alone"
    EQUAL_WEIGHT_BASKET = "equal_weight_basket"
    BUY_AND_HOLD_BTC = "buy_and_hold_btc"


BENCHMARKS: Final[tuple[Benchmark, ...]] = tuple(Benchmark)
"""The two single-asset sleeves are run through this milestone's own portfolio machinery on BTC
alone, rather than quoted from M29, so the comparison against the combined portfolio differs in
one thing only: what is in it."""


class Sleeve(DomainModel):
    """One frozen rule inside a portfolio, named for the report."""

    key: Text
    strategy_id: Text
    params: tuple[tuple[Text, Text], ...]


def sleeves_of(portfolio: Portfolio) -> tuple[Sleeve, ...]:
    """Return the frozen rules one portfolio holds, in declaration order."""
    wanted = PORTFOLIOS[portfolio]
    return tuple(
        Sleeve(
            key=probe.key,
            strategy_id=probe.candidate.strategy_id,
            params=probe.candidate.params,
        )
        for key in wanted
        for probe in SLEEVES
        if probe.key == key
    )
