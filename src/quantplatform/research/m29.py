"""M29 — profit-first research: where the money is, across seven families and three timeframes.

**Pre-declaration. Written and committed before a single result was seen**, which is the only
thing that makes the thresholds below evidence rather than description.

M22 through M24 asked "is there an edge?" and answered yes, barely: B2 on BTC annualises at
**1.7% out of sample**, and M23's own words fix the magnitude at 1.6%-2.9% a year before
stress. The infrastructure is certified; the edge is marginal. This milestone asks a
different question — **where is the largest robust economic edge available under the current
constraints** — and it is allowed to rank by profit, which M22 deliberately was not.

That difference is the design decision of this module, so it is stated plainly:

    Robustness is a GATE. Profit is the ORDER among whatever survives it.

M22's :func:`~quantplatform.research.m22.ranking_key` excluded return entirely, because
picking the top three by return with an extra step in front of it is still picking by return.
Here the two are separated instead: :func:`survives` decides pass or fail and reads no
return at all, and :func:`profit_rank` orders only what has already passed. Neither can be
quietly turned into the other — that is why they are two functions and not one score.

**Frequency is not an objective here.** Trade count enters only as a floor on sample size, and
a family that trades eight times a year and clears every gate beats one that trades four
hundred times and does not. If 1h turns out to lose the edge to costs, the declared response
is to close 1h, not to hunt for a variant that survives it.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.latch_policy import risk_configuration_for
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m16 import DATA_END, symbol_rules_for
from quantplatform.research.m22 import Horizon, _base, asset_for, doubled
from quantplatform.research.sprint import CANDIDATES, SprintCandidate
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "ASSETS",
    "CANDIDATES_M29",
    "COST_STRESS_MULTIPLIERS",
    "FAMILIES",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_ASSETS_POSITIVE",
    "MIN_CALMAR",
    "MIN_NEIGHBOUR_PROFIT_FACTOR",
    "MIN_YEARS_POSITIVE_SHARE",
    "SCREEN_STAGES",
    "SECONDS_PER_YEAR",
    "Family",
    "Horizon",
    "Measured",
    "Probe",
    "Robustness",
    "Stage",
    "cagr",
    "calmar",
    "definition_for",
    "profit_rank",
    "survives",
]


# --- What is searched ----------------------------------------------------------------------------

ASSETS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
"""Phase 1's universe. ADA and XRP have data on disk and are deliberately out: four markets
is already enough to fail the cross-asset gate, and adding two more before any family has
cleared it would buy compute time with no new question answered."""


class Family(StrEnum):
    """The seven questions. Declaration order is the order every table reports them in."""

    TREND = "trend"
    BREAKOUT = "breakout"
    MOMENTUM = "momentum"
    REGIME_TREND = "regime_trend"
    MEAN_REVERSION = "mean_reversion"
    VOL_FILTERED = "vol_filtered"
    REGIME_SWITCH = "regime_switch"


_FAMILY_RULE: Final[tuple[tuple[str, Family, str], ...]] = (
    ("T", Family.TREND, "ema_slope"),
    ("B", Family.BREAKOUT, "breakout_trend"),
    ("M", Family.MOMENTUM, "momentum_roc"),
    ("G", Family.REGIME_TREND, "regime_trend"),
    ("R", Family.MEAN_REVERSION, "zscore_revert"),
    ("V", Family.VOL_FILTERED, "vol_filtered_momentum"),
    ("S", Family.REGIME_SWITCH, "regime_switch"),
)
"""One rule per family, keyed by the letter its two variants carry.

Six are M22's, unchanged and on purpose: re-running the same rules at three timeframes is
only a timeframe comparison if the rules are identical. ``regime_trend`` is the seventh and
the only addition — it was M22's *benchmark*, the incumbent the candidates had to beat, and
M24 then showed it and B2 failing in complementary markets. Carrying it as a benchmark only
would leave the milestone unable to say whether the incumbent itself is the best available
edge, which is precisely the question."""

FAMILIES: Final[tuple[Family, ...]] = tuple(family for _, family, _ in _FAMILY_RULE)


def _m13(strategy_id: str) -> SprintCandidate:
    return next(candidate for candidate in CANDIDATES if candidate.strategy_id == strategy_id)


class Probe(DomainModel):
    """One configuration this milestone runs: a family, a horizon and its parameters.

    Not :class:`~quantplatform.research.m22.Variant`, which carries an
    :class:`~quantplatform.research.m22.EdgeFamily` — that enum has six members and this
    milestone has seven, ``regime_trend`` being the one M22 held as a benchmark rather than a
    candidate. Widening M22's enum would rewrite the vocabulary a finished milestone reports
    in; a type of its own leaves M22's record exactly as it was published.
    """

    key: Text
    family: Family
    horizon: Horizon
    candidate: SprintCandidate


def _pair(letter: str, family: Family, strategy_id: str) -> tuple[Probe, Probe]:
    base = _m13(strategy_id)
    long = base.model_copy(update={"params": doubled(base.params), "neighbours": ()})
    return (
        Probe(key=f"{letter}1", family=family, horizon=Horizon.BASE, candidate=base),
        Probe(key=f"{letter}2", family=family, horizon=Horizon.DOUBLED, candidate=long),
    )


CANDIDATES_M29: Final[tuple[Probe, ...]] = tuple(
    probe for args in _FAMILY_RULE for probe in _pair(*args)
)
"""Fourteen configurations: seven families, each at M13's horizon and at twice it.

Two per family and no more, and the second comes from
:func:`~quantplatform.research.m22.doubled`
— a mechanical rule that doubles every window and leaves every level alone. Nobody chooses
these numbers, which is what keeps this a screen and not a grid search. **The same parameter
values run at all three timeframes**: a window of 50 bars is 50 bars whether the bar is an
hour or a day. Converting windows to constant calendar time would be a second hypothesis
smuggled into a timeframe comparison, so the bar count is held and the calendar horizon is
allowed to differ — that difference *is* the axis being tested."""


# --- Cost of compute, measured before planning ---------------------------------------------------


class Stage(StrEnum):
    """Which pass of the screen a timeframe belongs to."""

    CHEAP = "cheap"
    FULL = "full"
    DEFERRED = "deferred"


SCREEN_STAGES: Final[dict[Timeframe, Stage]] = {
    Timeframe.D1: Stage.CHEAP,
    Timeframe.H4: Stage.FULL,
    Timeframe.H1: Stage.DEFERRED,
}
"""Staged by measured cost, not by guess.

The backtest engine is **quadratic in history length** — measured on this machine at 1 000,
2 000, 4 000 and 8 000 bars, the time multiplied by 3.88, 3.91 and 3.99 each time the bars
doubled. Extrapolating to the full series on disk gives, for the 56 runs of one screen
(7 families x 2 variants x 4 assets):

    1d   2 449 bars      ~5 s a run      ~5 minutes
    4h  19 790 bars    ~333 s a run      ~5.2 hours
    1h  79 097 bars   ~5 315 s a run    ~82.7 hours

1h is deferred on arithmetic, not on opinion, and the instruction to run a cheap screen first
is what makes that the right call rather than a compromise. It is **not** closed: if a family
clears the gate at 1d or 4h, asking the same question at 1h costs 8 runs per family, not 56.
Closing 1h is reserved for what its own results show, per the declared stop rule."""

SECONDS_PER_YEAR: Final[Decimal] = Decimal(365 * 24 * 60 * 60)


# --- The profit metrics, declared because Scorecard does not carry them --------------------------


def cagr(total_return: Decimal, *, bars: int, timeframe: Timeframe) -> Decimal | None:
    """Return the annualised compound rate implied by a run's total return.

    ``Scorecard`` reports ``total_return`` over whatever window the run covered, and windows
    differ by asset — SOL's history starts in 2020, BTC's in 2017. Comparing raw totals across
    them would rank a longer history above a better rule. Length is read from the bar count
    and the timeframe rather than from wall-clock dates, so a run over a gapped series is
    annualised by the data it actually had.

    Returns:
        The annual rate, or ``None`` when the window is empty or the strategy lost everything
        (a total return of -100% has no real compound rate, and reporting one would invent it).
    """
    if bars <= 0:
        return None
    growth = Decimal(1) + total_return
    if growth <= 0:
        return None
    years = Decimal(bars) * Decimal(timeframe.seconds) / SECONDS_PER_YEAR
    if years <= 0:
        return None
    return Decimal(str(float(growth) ** (1.0 / float(years)))) - Decimal(1)


def calmar(annual: Decimal | None, max_drawdown: Decimal) -> Decimal | None:
    """Return annual return divided by maximum drawdown — profit per unit of pain.

    The milestone's primary metric, and the reason it is here rather than raw return: a rule
    earning 3% a year against a 4% drawdown is a better business than one earning 6% against
    20%, and total return cannot tell the two apart. Undefined when there was no drawdown,
    because dividing by zero would rank a run that never lost as infinitely good on what is
    almost always too short a sample.
    """
    if annual is None or max_drawdown <= 0:
        return None
    return annual / max_drawdown


# --- The gate: robustness, pass or fail, reading no return ---------------------------------------

MIN_CALMAR: Final[Decimal] = Decimal("0.50")
"""Annual return must be at least half the worst drawdown suffered to earn it.

Chosen against what the project already has rather than out of the air: B2 on BTC annualised
1.7% out of sample against a 2.00% drawdown, a Calmar of 0.85. A bar at 0.50 therefore admits
the incumbent comfortably while refusing anything that pays less than half its own worst
loss — a floor the existing candidate clears, not one drawn to flatter a new one."""

MIN_ASSETS_POSITIVE: Final[int] = 3
"""Of four. M23 left this unstated and had to report per-market instead; stating it now is
the fix for that gap, and three of four is what B2 achieved and SOL denied it."""

MIN_YEARS_POSITIVE_SHARE: Final[Decimal] = Decimal("0.60")
MAX_SINGLE_YEAR_SHARE: Final[Decimal] = Decimal("0.50")
"""Both carried unchanged from M23, where they were declared before its runs and where SOL's
concentration of 1.32 — one year contributing more than the total — is what they caught."""

MIN_NEIGHBOUR_PROFIT_FACTOR: Final[Decimal] = Decimal("1.00")
"""Every declared neighbour must still make money. A peak that only stands at its own exact
parameters is a fitted artefact whatever its return."""

COST_STRESS_MULTIPLIERS: Final[tuple[int, ...]] = (2, 3)
"""Costs are the one assumption this milestone cannot verify from data on disk: the backtests
charge 10 bps fees, 5 bps slippage and 2 bps spread, and only the fee is a published number.
Rather than assert a measurement that has not been made, the declared test is survival —
a family must still clear its gates at **twice** the modelled cost, and the run at three times
is reported so the distance to the cliff is visible rather than binary. A rule whose edge
dies between 1x and 2x was never robust to a cost model nobody validated."""


class Robustness(StrEnum):
    """Why a configuration failed the gate. One name per declared condition."""

    THIN_SAMPLE = "thin_sample"
    NEGATIVE_AFTER_COSTS = "negative_after_costs"
    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    SINGLE_ASSET = "single_asset"
    SINGLE_YEAR = "single_year"
    INCONSISTENT_YEARS = "inconsistent_years"
    NARROW_PEAK = "narrow_peak"
    COST_FRAGILE = "cost_fragile"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"


class Measured(DomainModel):
    """Everything the gate reads about one configuration, gathered from its runs.

    A record rather than a dozen arguments, because every field here has to be *measured* and
    a positional call would make it easy to hand the gate a number from the wrong run. Frozen,
    so nothing can be adjusted between gathering the evidence and judging it.
    """

    trades: int
    min_trades: int
    annual: Decimal | None
    max_drawdown: Decimal
    screen_max_drawdown: Decimal
    calmar_ratio: Decimal | None
    assets_positive: int
    years_positive_share: Decimal | None
    single_year_share: Decimal | None
    neighbour_min_profit_factor: Decimal | None
    annual_at_double_cost: Decimal | None
    out_of_sample_return: Decimal | None


def survives(measured: Measured) -> tuple[bool, tuple[Robustness, ...]]:
    """Return whether a configuration clears every declared gate, and what it failed.

    **Reads no ranking and produces no score.** Its answer is a boolean and a list of reasons,
    so that the ordering step downstream cannot reach past it — profit is allowed to sort
    survivors and is never allowed to rescue a failure. Every condition is one the user
    declared: costs, drawdown, multi-year, cross-asset, sensitivity, sample, out of sample.

    Returns:
        ``(True, ())`` when everything passed, otherwise ``(False, reasons)`` with the reasons
        in declaration order so two failures always report in the same sequence.
    """
    m = measured
    failures: list[Robustness] = []
    if m.trades < m.min_trades:
        failures.append(Robustness.THIN_SAMPLE)
    if m.annual is None or m.annual <= 0:
        failures.append(Robustness.NEGATIVE_AFTER_COSTS)
    if m.max_drawdown > m.screen_max_drawdown:
        failures.append(Robustness.DRAWDOWN)
    if m.calmar_ratio is None or m.calmar_ratio < MIN_CALMAR:
        failures.append(Robustness.LOW_CALMAR)
    if m.assets_positive < MIN_ASSETS_POSITIVE:
        failures.append(Robustness.SINGLE_ASSET)
    if m.years_positive_share is None or m.years_positive_share < MIN_YEARS_POSITIVE_SHARE:
        failures.append(Robustness.INCONSISTENT_YEARS)
    if m.single_year_share is None or m.single_year_share > MAX_SINGLE_YEAR_SHARE:
        failures.append(Robustness.SINGLE_YEAR)
    if (
        m.neighbour_min_profit_factor is None
        or m.neighbour_min_profit_factor < MIN_NEIGHBOUR_PROFIT_FACTOR
    ):
        failures.append(Robustness.NARROW_PEAK)
    if m.annual_at_double_cost is None or m.annual_at_double_cost <= 0:
        failures.append(Robustness.COST_FRAGILE)
    if m.out_of_sample_return is None or m.out_of_sample_return <= 0:
        failures.append(Robustness.OUT_OF_SAMPLE_NEGATIVE)
    return (not failures, tuple(failures))


# --- The order: profit, among survivors only ------------------------------------------------------


def profit_rank(*, calmar_ratio: Decimal | None, annual: Decimal | None) -> tuple[Decimal, Decimal]:
    """Return the sort key for configurations that have **already cleared** :func:`survives`.

    Calmar first because the objective is robust economic edge and not raw return; annualised
    return second, to break ties between rules that carry risk equally well. Trade count is
    absent deliberately — frequency is not an objective of this milestone, and a rule trading
    eight times a year that clears every gate outranks one trading four hundred times that
    does not.

    Applying this to anything :func:`survives` rejected would be choosing by profit with the
    gate as decoration; the two are separate functions so that misuse has to be written on
    purpose rather than arrived at by accident.
    """
    return (
        calmar_ratio if calmar_ratio is not None else Decimal(-1),
        annual if annual is not None else Decimal(-1),
    )


# --- Building one run ----------------------------------------------------------------------------


def definition_for(
    raw: str, probe: Probe, timeframe: Timeframe, *, latching: bool
) -> ExperimentDefinition:
    """Return the experiment definition for one probe, on one market, at one timeframe.

    Plumbing, not policy: it moves no threshold declared above and chooses no number. The only
    thing it adds over M22's builder is that the timeframe is an argument rather than a
    constant, which is what lets the same fourteen rules be asked the same question at 1d, 4h
    and 1h.

    Risk is :func:`~quantplatform.research.m15.risk_for_timeframe` applied to the deployed Risk
    V2 configuration — the conversion M15 declared and every run from M15 onward has used, so a
    daily run is risked on a daily bar's terms rather than an hour's.

    Args:
        raw: Market symbol as the dataset names it, e.g. ``"BTCUSDT"``.
        probe: Which of the fourteen configurations to run.
        timeframe: The bar interval, which also selects the risk conversion.
        latching: ``False`` for the screen's research variant, which releases the latching
            breakers so a rule is measured rather than the breakers; ``True`` for deployed
            Risk V2, which any PAPER CANDIDATE has to survive.

    Returns:
        A definition, round-tripped through canonical JSON so it carries no computed field.
    """
    asset, base = asset_for(raw), _base()
    version = build_research_registry().metadata_for(probe.candidate.strategy_id).version
    at_timeframe = risk_for_timeframe(base.risk, timeframe)
    risk = at_timeframe if latching else risk_configuration_for(REFERENCE, at_timeframe)
    dataset = base.dataset.model_copy(
        update={
            "symbol": asset.symbol,
            "symbol_rules": symbol_rules_for(asset),
            "market_type": MarketType.SPOT,
            "timeframe": timeframe,
            "start": asset.start,
            "end": DATA_END,
            "source": "binance_vision_m16" if timeframe is not Timeframe.D1 else "m29_daily",
        }
    )
    suffix = "deployed" if latching else "ref"
    copy = base.model_copy(
        update={
            "name": (
                f"m29-{raw}-{timeframe.value}-{probe.key}-{probe.candidate.strategy_id}-{suffix}"
            ),
            "strategy": StrategySpec(
                strategy_id=probe.candidate.strategy_id,
                strategy_version=version,
                params=probe.candidate.params,
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": timeframe}),
            "risk": risk,
            "role": ExperimentRole.IN_SAMPLE,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))
