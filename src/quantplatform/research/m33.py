"""M33 — volatility compression and expansion: a family that is neither trend nor rotation.

**Pre-declaration. Written and committed before a single result was seen.**

Four milestones have now closed. M29 exhausted seven single-asset families over two timeframes.
M30 to M32 followed rotation to its end: a configuration that cleared every gate and its own
neighbourhood on six hand-picked markets, and then broke on a survivorship-corrected universe
because its edge lived only at one lookback. What all of those share is a *directional* premise
— something is going up, keep holding it, or something is going up more than its neighbours.

This milestone asks a different kind of question:

    Do the moves worth having begin when a quiet market stops being quiet?

That is a statement about the *second* moment rather than the first, and nothing this project has
run tests it. ``vol_filtered_momentum`` declines to enter a volatility spike, which is the
opposite reflex: it treats stillness as a precondition for a momentum trade rather than as the
setup itself.

**The one thing this family has that rotation never did: it runs on the production engine.**
These are single-asset rules, so they go through ``BacktestEngine`` with Risk V2, stop-based
sizing, order rejection and venue minimums — the certified path every promoted strategy uses.
M30 to M32 needed a second backtester because a cross-sectional rule cannot be expressed
single-symbol; M33 needs no such thing, and so carries none of that uncertainty.

**No production file was touched to make this possible.** Every feature the three rules read is
already in the indicator grammar and already known to ``warm_up``: dispersion and mean for the
Bollinger width, ``volratio`` for realised volatility, Donchian channels for the range and for
the breakout leg. The three strategies are registered in the *research* registry only, so
nothing here is visible to paper trading -- a test asserts that the paper-visible registry is
unchanged.

**One design decision does the work, so it is stated plainly.** Each rule measures a different
quantity going still, and each divides that measurement by what a random walk would produce over
the same pair of windows. So a threshold of 1.0 means "exactly as still as drift-free noise" in
all three rules, and the same two declared thresholds are comparable across them. Price
dispersion and realised range grow with the square root of the window, so their ratios are
scaled by ``sqrt(short/long)``; the volatility of one-bar returns does not grow with the window
at all, so its ratio is not scaled. That asymmetry is arithmetic, not judgement.
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
from quantplatform.research.m22 import SCREEN_MAX_DRAWDOWN, SCREEN_MIN_TRADES, _base, asset_for
from quantplatform.research.m29 import (
    COST_STRESS_MULTIPLIERS,
    MAX_SINGLE_YEAR_SHARE,
    MIN_ASSETS_POSITIVE,
    MIN_CALMAR,
    MIN_NEIGHBOUR_PROFIT_FACTOR,
    cagr,
    calmar,
)
from quantplatform.research.m30 import ASSETS_M30, OOS_START
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "ASSETS_M33",
    "COMPRESSION_THRESHOLDS",
    "COST_STRESS_MULTIPLIERS",
    "ENTRY_LOOKBACK",
    "EXIT_LOOKBACK",
    "INHERITED_MAX_RATIO",
    "LONG_WINDOW",
    "MAX_SINGLE_YEAR_SHARE",
    "MIN_ASSETS_POSITIVE",
    "MIN_CALMAR",
    "MIN_NEIGHBOUR_PROFIT_FACTOR",
    "NEIGHBOUR_SHORT_WINDOWS",
    "OOS_START",
    "SCREEN_MAX_DRAWDOWN",
    "SCREEN_MIN_TRADES",
    "SHORT_WINDOW",
    "TIMEFRAME",
    "VARIANTS_M33",
    "Compressed",
    "CompressionRobustness",
    "Rule",
    "Variant",
    "advances",
    "cagr",
    "calmar",
    "definition_for",
    "survives",
]


# --- What is searched ----------------------------------------------------------------------------

ASSETS_M33: Final[tuple[str, ...]] = ASSETS_M30
"""The six declared markets, the same set and the same order M30 and M31 used, so a result here
is comparable with theirs without a translation step."""

TIMEFRAME: Final[Timeframe] = Timeframe.D1
"""Phase 1 is 1d only, by instruction. 4h is reached only by a rule that passes here, and 1h is
excluded outright rather than deferred."""


class Rule(StrEnum):
    """The three ways of measuring stillness. Declaration order is the reporting order."""

    BB_SQUEEZE = "bb_squeeze"
    VOL_COMPRESSION = "vol_compression"
    RANGE_COMPRESSION = "range_compression"


RULES: Final[tuple[Rule, ...]] = tuple(Rule)


# --- Every parameter, and where it comes from ----------------------------------------------------

SHORT_WINDOW: Final[int] = 24
"""The window stillness is measured over. **Inherited** from ``vol_filtered_momentum``'s
``short_vol``."""

LONG_WINDOW: Final[int] = 168
"""What stillness is measured against. **Inherited** from the same strategy's ``long_vol``."""

ENTRY_LOOKBACK: Final[int] = 20
"""Bars the breakout leg looks back over. **Inherited** from ``breakout``'s ``entry_lookback``,
unchanged since M13 and the same leg ``breakout_trend`` runs in paper today."""

EXIT_LOOKBACK: Final[int] = 10
"""Bars the exit looks back over. **Inherited** from ``breakout``'s ``exit_lookback``."""

INHERITED_MAX_RATIO: Final[Decimal] = Decimal("1.5")
"""``vol_filtered_momentum``'s ``max_ratio``: the only volatility-ratio level this project has
ever declared. It is a *ceiling* there — do not enter when short-window volatility exceeds this
multiple of the long-window one — and M33 needs a *floor* on stillness, so it is inverted rather
than replaced."""

COMPRESSION_THRESHOLDS: Final[tuple[Decimal, ...]] = (
    Decimal(1),
    (Decimal(1) / INHERITED_MAX_RATIO).quantize(Decimal("0.0001")),
)
"""The two declared thresholds, and the only dimension that moves between a rule's two variants.

``1.0`` is the natural null: the market is exactly as still as drift-free noise would make it,
so the filter admits everything quieter than random. ``0.6667`` is the mechanical inverse of the
inherited ceiling — a third stiller again — and is derived here rather than typed, so it cannot
drift from the number it came from."""

NEIGHBOUR_SHORT_WINDOWS: Final[tuple[int, ...]] = (SHORT_WINDOW // 2, SHORT_WINDOW * 2)
"""The sensitivity probes: half and double the measurement window, by the same mechanical rule
that produced M22's second variants and M29's and M30's neighbours. A rule whose edge lives only
at 24 bars is a narrow peak, and these are the two points that say so."""


class Variant(DomainModel):
    """One declared configuration: a rule at one of the two thresholds."""

    key: Text
    rule: Rule
    threshold: Decimal
    params: tuple[tuple[Text, Text], ...]
    """Exactly what is handed to the registry, so a recorded result cannot later be
    re-interpreted under different numbers."""

    @property
    def strategy_id(self) -> str:
        """Return the research strategy this variant runs."""
        return self.rule.value


def _params(
    rule: Rule, threshold: Decimal, *, short: int = SHORT_WINDOW
) -> tuple[tuple[str, str], ...]:
    """Return the parameter pairs for one rule at one threshold.

    The two window names differ between rules -- ``short_vol`` for the volatility rule,
    ``short_window`` for the other two -- because each strategy names the window after what it
    measures. The values are identical.
    """
    first, second = (
        ("short_vol", "long_vol")
        if rule is Rule.VOL_COMPRESSION
        else ("short_window", "long_window")
    )
    return (
        (first, str(short)),
        (second, str(LONG_WINDOW)),
        ("entry_lookback", str(ENTRY_LOOKBACK)),
        ("exit_lookback", str(EXIT_LOOKBACK)),
        ("max_compression", str(threshold)),
    )


VARIANTS_M33: Final[tuple[Variant, ...]] = tuple(
    Variant(
        key=f"{letter}{index}",
        rule=rule,
        threshold=threshold,
        params=_params(rule, threshold),
    )
    for letter, rule in zip("BVR", RULES, strict=True)
    for index, threshold in enumerate(COMPRESSION_THRESHOLDS, start=1)
)
"""Six configurations: three rules at two thresholds, which is the declared ceiling of two
variants per rule.

Within a rule exactly one number moves, so a difference between its two variants has one
possible cause. Between rules nothing moves except what is being measured: same windows, same
breakout leg, same exit, same thresholds. That is what makes this a comparison of three
definitions of stillness rather than six unrelated strategies."""


# --- The gate ------------------------------------------------------------------------------------


class CompressionRobustness(StrEnum):
    """Every declared way one market's result can fail. Order is the reporting order.

    Exactly the conditions the user declared, and nothing else. M29's and M30's gates also read
    sample size and cross-asset breadth; breadth is the aggregation rule below rather than a
    per-market condition, and **sample size is measured and reported but does not gate** -- a
    compression rule is expected to trade rarely, and a floor nobody declared could reject a
    result on grounds that were never agreed.
    """

    NEGATIVE_AFTER_COSTS = "negative_after_costs"
    DRAWDOWN = "drawdown"
    LOW_CALMAR = "low_calmar"
    SINGLE_YEAR = "single_year"
    NARROW_PEAK = "narrow_peak"
    COST_FRAGILE = "cost_fragile"
    OUT_OF_SAMPLE_NEGATIVE = "out_of_sample_negative"


class Compressed(DomainModel):
    """Everything the gate reads about one variant on one market."""

    annual: Decimal | None
    max_drawdown: Decimal
    calmar_ratio: Decimal | None
    single_year_share: Decimal | None
    neighbour_min_profit_factor: Decimal | None
    annual_at_double_cost: Decimal | None
    annual_at_triple_cost: Decimal | None
    out_of_sample_return: Decimal | None


def survives(measured: Compressed) -> tuple[bool, tuple[CompressionRobustness, ...]]:
    """Return whether one market's result clears every declared condition, and what it failed.

    Returns:
        ``(True, ())`` when everything passed, otherwise ``(False, reasons)`` with the reasons
        in declaration order so two failures always report in the same sequence.
    """
    m = measured
    failures: list[CompressionRobustness] = []
    if m.annual is None or m.annual <= 0:
        failures.append(CompressionRobustness.NEGATIVE_AFTER_COSTS)
    if m.max_drawdown > SCREEN_MAX_DRAWDOWN:
        failures.append(CompressionRobustness.DRAWDOWN)
    if m.calmar_ratio is None or m.calmar_ratio < MIN_CALMAR:
        failures.append(CompressionRobustness.LOW_CALMAR)
    if m.single_year_share is None or m.single_year_share > MAX_SINGLE_YEAR_SHARE:
        failures.append(CompressionRobustness.SINGLE_YEAR)
    if (
        m.neighbour_min_profit_factor is None
        or m.neighbour_min_profit_factor < MIN_NEIGHBOUR_PROFIT_FACTOR
    ):
        failures.append(CompressionRobustness.NARROW_PEAK)
    if (
        m.annual_at_double_cost is None
        or m.annual_at_double_cost <= 0
        or m.annual_at_triple_cost is None
        or m.annual_at_triple_cost <= 0
    ):
        failures.append(CompressionRobustness.COST_FRAGILE)
    if m.out_of_sample_return is None or m.out_of_sample_return <= 0:
        failures.append(CompressionRobustness.OUT_OF_SAMPLE_NEGATIVE)
    return (not failures, tuple(failures))


def advances(markets_passed: int) -> bool:
    """Return whether a variant clears the cross-asset condition.

    **Declared before running, which M29 did not do and said so.** That milestone applied its
    gate per market and counted the passes, then had to record afterwards that the aggregation
    had never been specified. Here it is: a variant advances only if it passes in at least
    :data:`~quantplatform.research.m29.MIN_ASSETS_POSITIVE` of the six declared markets, the same
    three-of-N reading M29 used and M30 inherited.
    """
    return markets_passed >= MIN_ASSETS_POSITIVE


# --- Plumbing ------------------------------------------------------------------------------------


def definition_for(raw: str, variant: Variant, *, latching: bool) -> ExperimentDefinition:
    """Return the experiment definition for one variant on one market at 1d.

    Plumbing, not policy: it moves no threshold declared above and chooses no number. Risk is
    :func:`~quantplatform.research.m15.risk_for_timeframe` applied to the deployed Risk V2
    configuration, the conversion every run since M15 has used.

    Args:
        raw: Market symbol as the dataset names it, e.g. ``"BTCUSDT"``.
        variant: Which of the six configurations to run.
        latching: ``False`` for the screen's research variant, which releases the latching
            breakers so the rule is measured rather than the breakers; ``True`` for deployed
            Risk V2, which any PAPER CANDIDATE has to survive.

    Returns:
        A definition, round-tripped through canonical JSON so it carries no computed field.
    """
    asset, base = asset_for(raw), _base()
    version = build_research_registry().metadata_for(variant.strategy_id).version
    at_timeframe = risk_for_timeframe(base.risk, TIMEFRAME)
    risk = at_timeframe if latching else risk_configuration_for(REFERENCE, at_timeframe)
    dataset = base.dataset.model_copy(
        update={
            "symbol": asset.symbol,
            "symbol_rules": symbol_rules_for(asset),
            "market_type": MarketType.SPOT,
            "timeframe": TIMEFRAME,
            "start": asset.start,
            "end": DATA_END,
            "source": "m30_daily",
        }
    )
    suffix = "deployed" if latching else "ref"
    copy = base.model_copy(
        update={
            "name": f"m33-{raw}-{TIMEFRAME.value}-{variant.key}-{variant.strategy_id}-{suffix}",
            "strategy": StrategySpec(
                strategy_id=variant.strategy_id,
                strategy_version=version,
                params=variant.params,
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": TIMEFRAME}),
            "risk": risk,
            "role": ExperimentRole.IN_SAMPLE,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def neighbour_of(variant: Variant, short: int) -> Variant:
    """Return the same variant with its measurement window moved to a neighbour's.

    A sensitivity probe, never a candidate: a neighbour that scores better is recorded and not
    adopted, because changing a parameter because a result pointed at it is what a
    pre-declaration exists to prevent.
    """
    return Variant(
        key=f"{variant.key}-short{short}",
        rule=variant.rule,
        threshold=variant.threshold,
        params=_params(variant.rule, variant.threshold, short=short),
    )
