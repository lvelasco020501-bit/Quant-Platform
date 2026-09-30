"""M30 — multi-asset rotation: is picking the best asset better than trading each one alone?

**Pre-declaration. Written and committed before a single result was seen**, which is the only
thing that makes the numbers below evidence rather than description.

M29 exhausted a search space. Fourteen configurations of seven families, two timeframes, four
markets, and nothing cleared a gate calibrated to admit the incumbent the project already has.
The conclusion it reached was narrow and useful: *asking "when do I enter BTC?" better is not
where the remaining money is.* So this milestone changes the question rather than the answer,
and asks instead:

    Which asset has the best relative opportunity right now?

That is a different kind of rule. Every strategy the platform has run to date looks at one
symbol's history and decides long or flat. A rotation rule looks **across** symbols at the same
instant and decides *which*. The edge it hunts is not a better entry — it is the dispersion
between assets, which is a quantity no single-asset backtest can see at all.

Three things about the design are stated here because they are the load-bearing choices:

**Every parameter is inherited, not chosen.** The lookback is 72 bars because that is what
:data:`~quantplatform.research.sprint.CANDIDATES` has declared for ``momentum_roc`` since M13;
the regime filter is 200 bars because that is ``breakout_trend``'s ``trend_period``; the
volatility window is 72 because that is ``vol_momentum``'s. M30 picks no new numbers. The one
free choice a rotation rule genuinely adds — how often to rebalance — is removed rather than
tuned: the target holding is recomputed every bar and trades only when the set actually
changes, so turnover is an *output* of the rule and never a knob on it.

**Robustness is a GATE. Profit is the ORDER among whatever survives it.** M29's separation is
kept verbatim, including the two-function shape that stops one from becoming the other.

**The benchmarks answer the milestone's question but do not gate it.** The user declared seven
gates and beating a benchmark is not among them, so :func:`survives` does not read one — adding
a rejection criterion after the fact would be as much a breach as removing one. Benchmarks
decide something narrower and declared here: **no configuration may be called PAPER CANDIDATE
while a static equal-weight basket of the same six assets earns a better Calmar.** Rotation
that cannot beat holding everything has answered this milestone's question in the negative,
whatever its own robustness looks like.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.m15 import MIN_TRADES_PER_TEST_WINDOW
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN, SCREEN_MIN_TRADES
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_ASSETS_POSITIVE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
    MIN_YEARS_POSITIVE_SHARE,
    cagr,
    calmar,
    profit_rank,
)
from quantplatform.research.sprint import PROMISING_WALK_FORWARD_SHARE

__all__ = [
    "ASSETS_M30",
    "BENCHMARKS",
    "COST_STRESS_MULTIPLIERS",
    "CROSS_SECTIONAL_THRESHOLDS",
    "FAMILIES_M30",
    "FEE_BASIS_POINTS",
    "LOOKBACK_BARS",
    "MAX_SINGLE_ASSET_SHARE",
    "MIN_FOLD_TRADES",
    "MIN_WALK_FORWARD_POSITIVE_SHARE",
    "NEIGHBOUR_LOOKBACKS",
    "ONE_WAY_COST_BASIS_POINTS",
    "OOS_START",
    "REGIME_FILTER_BARS",
    "REGIME_FILTER_BARS_DOUBLED",
    "RULES_M30",
    "SLIPPAGE_BASIS_POINTS",
    "VOL_WINDOW_BARS",
    "WALK_FORWARD_FOLDS",
    "Benchmark",
    "FamilyM30",
    "RotationMeasured",
    "RotationRobustness",
    "RotationRule",
    "beats_basket",
    "cagr",
    "calmar",
    "profit_rank",
    "survives",
]


# --- The universe --------------------------------------------------------------------------------

ASSETS_M30: Final[tuple[str, ...]] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "XRPUSDT",
)
"""The six declared markets. All six were downloaded and validated by M16 against two
checksummed archives plus the exchange API, so none of them needs a new lineage.

**These six are today's large caps, and that is a selection made with hindsight.** Nothing in
this milestone can remove that: a rotation study is exactly the kind of design that survivorship
bias flatters, because the assets that would have dragged a real 2018 portfolio down are the
ones nobody lists in 2026. It is recorded here, at the top, rather than in a limitations
section, because every number this milestone produces inherits it."""


class FamilyM30(StrEnum):
    """The three questions. Declaration order is the order every table reports them in."""

    RELATIVE_STRENGTH = "relative_strength"
    CROSS_SECTIONAL = "cross_sectional"
    REGIME_FILTERED = "regime_filtered"


FAMILIES_M30: Final[tuple[FamilyM30, ...]] = tuple(FamilyM30)


# --- The parameters, every one of them inherited -------------------------------------------------

LOOKBACK_BARS: Final[int] = 72
"""Ranking lookback. **Inherited** from ``momentum_roc``'s ``lookback`` in
:data:`~quantplatform.research.sprint.CANDIDATES`, unchanged since M13. At 1d this is 72 days,
which is also the classical three-month momentum horizon — a coincidence worth noting and not
worth tuning towards."""

VOL_WINDOW_BARS: Final[int] = 72
"""Window for the volatility a cross-sectional score divides by. **Inherited** from
``vol_momentum``'s ``vol_window``."""

REGIME_FILTER_BARS: Final[int] = 200
"""Length of the moving average the aggregate basket must sit above for the regime-filtered
family to hold anything. **Inherited** from ``breakout_trend``'s ``trend_period`` — the same
200 the strategy now running in paper uses."""

REGIME_FILTER_BARS_DOUBLED: Final[int] = REGIME_FILTER_BARS * 2
"""The second regime variant, produced by :func:`~quantplatform.research.m22.doubled`'s rule
(windows double, levels stay) rather than by anyone's judgement."""

CROSS_SECTIONAL_THRESHOLDS: Final[tuple[Decimal, ...]] = (Decimal(0), Decimal("1.0"))
"""The two score floors the cross-sectional family tests. Zero is the natural null — the winner
must at least be going up. ``1.0`` is **inherited** from ``vol_momentum``'s ``threshold``, the
only volatility-normalised threshold the project has ever declared."""

NEIGHBOUR_LOOKBACKS: Final[tuple[int, ...]] = (LOOKBACK_BARS // 2, LOOKBACK_BARS * 2)
"""The sensitivity probes: half and double the declared lookback, by the same mechanical rule
that produced M22's second variants. A rule whose edge lives only at 72 bars is a narrow peak,
and these are the two points that say so."""


# --- What is run ---------------------------------------------------------------------------------


class RotationRule(DomainModel):
    """One declared rotation configuration.

    Frozen, and carrying its own parameters rather than reading them from module state, so that
    a result recorded against a key can never be re-interpreted under different numbers later.
    """

    key: Text
    family: FamilyM30
    lookback: int
    hold: int
    """How many assets are held at once, equally weighted."""
    volatility_normalised: bool
    """Whether the ranking score is divided by trailing volatility, making the comparison
    between assets one of risk-adjusted rather than raw momentum."""
    entry_threshold: Decimal | None
    """Score the best asset must exceed before anything is held; ``None`` holds the winner
    unconditionally. This is what puts the strategy in cash, and cash is a position."""
    regime_filter: int | None
    """Length of the equal-weight basket's moving-average filter, or ``None`` for no filter."""
    vol_window: int = VOL_WINDOW_BARS


RULES_M30: Final[tuple[RotationRule, ...]] = (
    RotationRule(
        key="RS1",
        family=FamilyM30.RELATIVE_STRENGTH,
        lookback=LOOKBACK_BARS,
        hold=1,
        volatility_normalised=False,
        entry_threshold=None,
        regime_filter=None,
    ),
    RotationRule(
        key="RS2",
        family=FamilyM30.RELATIVE_STRENGTH,
        lookback=LOOKBACK_BARS,
        hold=2,
        volatility_normalised=False,
        entry_threshold=None,
        regime_filter=None,
    ),
    RotationRule(
        key="CS1",
        family=FamilyM30.CROSS_SECTIONAL,
        lookback=LOOKBACK_BARS,
        hold=1,
        volatility_normalised=True,
        entry_threshold=CROSS_SECTIONAL_THRESHOLDS[0],
        regime_filter=None,
    ),
    RotationRule(
        key="CS2",
        family=FamilyM30.CROSS_SECTIONAL,
        lookback=LOOKBACK_BARS,
        hold=1,
        volatility_normalised=True,
        entry_threshold=CROSS_SECTIONAL_THRESHOLDS[1],
        regime_filter=None,
    ),
    RotationRule(
        key="RF1",
        family=FamilyM30.REGIME_FILTERED,
        lookback=LOOKBACK_BARS,
        hold=1,
        volatility_normalised=False,
        entry_threshold=None,
        regime_filter=REGIME_FILTER_BARS,
    ),
    RotationRule(
        key="RF2",
        family=FamilyM30.REGIME_FILTERED,
        lookback=LOOKBACK_BARS,
        hold=1,
        volatility_normalised=False,
        entry_threshold=None,
        regime_filter=REGIME_FILTER_BARS_DOUBLED,
    ),
)
"""Six configurations: two per family, the declared ceiling.

Within a family exactly one dimension moves, so a difference between two variants has one
possible cause. RS varies breadth (hold the winner, or the best two). CS varies the score floor
that decides between holding and cash. RF varies the length of the regime filter, by the
doubling rule. Nothing here is a grid: six runs, each one a question."""


class Benchmark(StrEnum):
    """What rotation is measured against. Reported always, never a gate."""

    BUY_AND_HOLD_BTC = "buy_and_hold_btc"
    EQUAL_WEIGHT_BASKET = "equal_weight_basket"
    B2_BREAKOUT_BTC = "b2_breakout_btc"
    REGIME_TREND_BTC = "regime_trend_btc"


BENCHMARKS: Final[tuple[Benchmark, ...]] = tuple(Benchmark)


# --- Costs, taken from the platform rather than assumed ------------------------------------------

FEE_BASIS_POINTS: Final[Decimal] = Decimal(10)
"""Venue fee on executed notional, in basis points. The value every M22-M29 definition carries
in ``definition.risk.execution_policy.fee.basis_points``."""

SLIPPAGE_BASIS_POINTS: Final[Decimal] = Decimal(5)
"""Adverse price move on a market fill, from the same execution policy's ``slippage``."""

ONE_WAY_COST_BASIS_POINTS: Final[Decimal] = FEE_BASIS_POINTS + SLIPPAGE_BASIS_POINTS
"""What one side of one rotation costs: 15 basis points on the notional that moves.

This is the platform's own arithmetic, not a new assumption. The backtest engine charges the
fee on filled notional and moves the fill price by the slippage; the third number those
definitions carry, ``assumed_spread_basis_points``, is fed to the **risk engine as a metric**
and never adjusts a price, so adding it here would charge a cost the engine does not charge.

Turnover is what makes this matter. A rule that swaps its holding every bar pays this 15 bps
twice per swap, and the declared cost stress multiplies it by
:data:`~quantplatform.research.m29.COST_STRESS_MULTIPLIERS`."""


# --- Windows -------------------------------------------------------------------------------------

OOS_START: Final[datetime] = datetime(2024, 1, 1, tzinfo=UTC)
"""M23's out-of-sample boundary, reused unchanged through M29 and again here. Redrawing it per
milestone would make every prior verdict incomparable and hand this one a window chosen after
the fact."""

WALK_FORWARD_FOLDS: Final[int] = 5
"""Contiguous equal test windows the history is cut into. Nothing is selected in any fold — the
identical configuration runs in all of them — so this measures **stability across windows**,
which is what "no depender de una sola ventana" asks for, and not out-of-sample edge."""

MIN_WALK_FORWARD_POSITIVE_SHARE: Final[Decimal] = PROMISING_WALK_FORWARD_SHARE
"""Share of folds that must end positive. **Inherited** from the sprint's PROMISING bar."""


# --- The gate ------------------------------------------------------------------------------------

MAX_SINGLE_ASSET_SHARE: Final[Decimal] = Decimal("0.60")
"""Most of the net profit any one asset may account for.

This is the only threshold in M30 that is not inherited, because no prior milestone measured
per-asset contribution within a single strategy — every earlier run traded one symbol, where
the question is meaningless. So it is a judgement, and the judgement is declared here before
any result exists.

0.60 rather than the 0.50 the year gate uses, and the asymmetry is deliberate: a rotation rule
is *built* to concentrate into whatever is strongest, so holding it to the same bar as calendar
years would penalise the mechanism rather than test it. 0.60 still requires that at least two
fifths of the profit came from somewhere other than the best asset — which is what distinguishes
"rotation worked" from "it found BTC and sat there", the single most likely way for this
milestone to fool itself."""


class RotationRobustness(StrEnum):
    """Every declared way a configuration can fail. Order is the order they are reported in."""

    THIN_SAMPLE = "thin_sample"
    NEGATIVE_AFTER_COSTS = "negative_after_costs"
    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    SINGLE_ASSET = "single_asset"
    ASSET_CONCENTRATION = "asset_concentration"
    SINGLE_YEAR = "single_year"
    INCONSISTENT_YEARS = "inconsistent_years"
    UNSTABLE_ACROSS_WINDOWS = "unstable_across_windows"
    NARROW_PEAK = "narrow_peak"
    COST_FRAGILE = "cost_fragile"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"


class RotationMeasured(DomainModel):
    """Everything the gate reads about one configuration, gathered from its runs.

    A record rather than a dozen arguments, because every field here has to be *measured* and a
    positional call would make it easy to hand the gate a number from the wrong run. Frozen, so
    nothing can be adjusted between gathering the evidence and judging it.
    """

    trades: int
    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    assets_positive: int
    top_asset_share: Decimal | None
    """Share of net profit the single best-contributing asset accounts for."""
    years_positive_share: Decimal | None
    single_year_share: Decimal | None
    walk_forward_positive_share: Decimal | None
    neighbour_min_profit_factor: Decimal | None
    annual_at_double_cost: Decimal | None
    out_of_sample_return: Decimal | None


def survives(measured: RotationMeasured) -> tuple[bool, tuple[RotationRobustness, ...]]:
    """Return whether a configuration clears every declared gate, and what it failed.

    **Reads no ranking, no benchmark and no score.** Its answer is a boolean and a list of
    reasons, so the ordering step downstream cannot reach past it: profit is allowed to sort
    survivors and is never allowed to rescue a failure. Every condition is one the user
    declared — out of sample, stress, several positive years, no single asset explaining the
    result, sensitivity, drawdown, and not depending on one window.

    Returns:
        ``(True, ())`` when everything passed, otherwise ``(False, reasons)`` with the reasons
        in declaration order so two failures always report in the same sequence.
    """
    m = measured
    failures: list[RotationRobustness] = []
    if m.trades < SCREEN_MIN_TRADES:
        failures.append(RotationRobustness.THIN_SAMPLE)
    if m.annual is None or m.annual <= 0:
        failures.append(RotationRobustness.NEGATIVE_AFTER_COSTS)
    if m.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failures.append(RotationRobustness.DRAWDOWN)
    if m.calmar_ratio is None or m.calmar_ratio < MIN_CALMAR:
        failures.append(RotationRobustness.LOW_CALMAR)
    if m.assets_positive < MIN_ASSETS_POSITIVE:
        failures.append(RotationRobustness.SINGLE_ASSET)
    if m.top_asset_share is None or m.top_asset_share > MAX_SINGLE_ASSET_SHARE:
        failures.append(RotationRobustness.ASSET_CONCENTRATION)
    if m.single_year_share is None or m.single_year_share > MAX_SINGLE_YEAR_SHARE:
        failures.append(RotationRobustness.SINGLE_YEAR)
    if m.years_positive_share is None or m.years_positive_share < MIN_YEARS_POSITIVE_SHARE:
        failures.append(RotationRobustness.INCONSISTENT_YEARS)
    if (
        m.walk_forward_positive_share is None
        or m.walk_forward_positive_share < MIN_WALK_FORWARD_POSITIVE_SHARE
    ):
        failures.append(RotationRobustness.UNSTABLE_ACROSS_WINDOWS)
    if (
        m.neighbour_min_profit_factor is None
        or m.neighbour_min_profit_factor < MIN_NEIGHBOUR_PROFIT_FACTOR
    ):
        failures.append(RotationRobustness.NARROW_PEAK)
    if m.annual_at_double_cost is None or m.annual_at_double_cost <= 0:
        failures.append(RotationRobustness.COST_FRAGILE)
    if m.out_of_sample_return is None or m.out_of_sample_return <= 0:
        failures.append(RotationRobustness.OUT_OF_SAMPLE_NEGATIVE)
    return (not failures, tuple(failures))


def beats_basket(*, rotation_calmar: Decimal | None, basket_calmar: Decimal | None) -> bool:
    """Return whether dynamic selection earned a better Calmar than holding everything.

    Not part of :func:`survives` — the user declared seven gates and this is not one of them,
    so it cannot reject anything. It is the **promotion** condition instead: a configuration
    that clears every gate but loses to a static equal-weight basket of the same six assets has
    answered this milestone's question in the negative, and calling it a PAPER CANDIDATE would
    be recommending work the evidence says is not worth doing.

    A basket whose Calmar could not be measured cannot be beaten, so the answer is ``False``:
    an unmeasurable comparison is not a win.
    """
    if rotation_calmar is None or basket_calmar is None:
        return False
    return rotation_calmar > basket_calmar


MIN_FOLD_TRADES: Final[int] = MIN_TRADES_PER_TEST_WINDOW
"""Trades a walk-forward window needs before its sign is treated as information. **Inherited**
from M15, where the same question was asked of the same kind of window."""
