"""M22 — six families of rule, asked the same question on the same markets.

Seven milestones went into the account's protection and none of them moved a return, because
protection cannot create edge it can only spend. M17 through M21 answered what they were
asked and closed: the permanent latch shuts a market off, a cooldown that keeps its reference
never reopens usefully, a local reset is the only operable rule, detection has to happen on
every bar, and an account stop is a guarantee rather than an improvement. None of that is a
reason to trade anything. So this milestone goes back to the only question that is:

**Is there an edge here at all, and does it belong to a family or to a market?**

**Six families, and why these six.** Every one is already implemented, tested and registered
from the M13 sprint, so this milestone writes no strategy code — which is what keeps it a
study of strategies rather than another study of infrastructure:

======================  =======================  ==========================================
 family                  rule                     the question it asks
======================  =======================  ==========================================
 trend                   ``ema_slope``            is price above an average that is rising?
 breakout                ``breakout_trend``       is this a new high, in an uptrend?
 momentum                ``momentum_roc``         is the N-bar return positive?
 mean reversion          ``zscore_revert``        is price stretched below its own mean?
 regime switch           ``regime_switch``        which rule does *this* regime call for?
 volatility-filtered     ``vol_filtered_momentum`` is the trend running in calm conditions?
======================  =======================  ==========================================

They are six different questions, not six spellings of one: two ask about direction, one
about a level being broken, one about distance from a mean, one about which of two rules to
apply, and one about whether conditions are calm enough to act at all. Where two could have
collapsed into each other they were kept apart on purpose — the plain Donchian channel is not
a candidate here, it is a *benchmark*, so the breakout family is tested as the filtered rule a
practitioner would actually run and the bare channel still appears in every table.

**Two variants each, and neither was chosen.** The first is M13's canonical configuration,
unchanged, fixed before any of this existed. The second is that configuration with **every
window doubled and every threshold left alone**, computed by :func:`doubled` rather than
picked — a test recomputes all six, so a variant cannot be quietly tuned afterwards. Doubling
is the one move that asks a different question of the same rule ("does the horizon matter?")
without asking a different rule. A threshold is not a horizon: doubling a z-score or an
efficiency ratio would change what the rule *means*, so thresholds are held.

**The same parameters on every market, structurally.** :class:`Variant` has no symbol field.
Per-asset fitting is not forbidden here by discipline; there is nowhere to write it down.

**What the screen measures, and the limitation that governs every number it produces.** Runs
are made under the research variant — the deployed Risk V2 configuration with its two
latching breakers released, exactly M13's instrument and not Risk V2. Sizing, stops, costs
and the time stop stay as deployed, which means the figure being measured is *entry rule plus
platform exit*, never the textbook rule on its own. Two deployed exits can end a trade before
its rule asks to: a stop at 6% of price, and a time stop after :data:`TIME_STOP_BARS` bars —
seven days. The time stop is the one that would be fatal to a trend family, since nothing
could hold a trend past a week however its own exit reads, so every run here reports its
holding-time distribution and the share of trades that reached the cap. Those two numbers,
not an assumption, are what says whether a family's result describes its rule or the
platform's clock.

**The screen is triage, not a verdict.** :func:`shows_signal` decides only where to spend the
next hours of compute, and its thresholds are deliberately looser than
:func:`quantplatform.research.sprint.judge`'s — a configuration can pass the screen and still
be REJECT. What the screen is strict about is the thing that costs nothing to be strict about
and cannot be recovered later: a family advances only when **one single variant** cleared the
gate on **both** BTC and ETH. One variant per market would be per-asset fitting reached by
accident, and is refused by :func:`forwarded`.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.folds import WindowSpec
from quantplatform.research.latch_policy import risk_configuration_for
from quantplatform.research.m14 import REFERENCE
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m16 import ASSETS, DATA_END, Asset, symbol_rules_for
from quantplatform.research.sprint import CANDIDATES, Family, SprintCandidate
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "CANDIDATES_M22",
    "EXTENSION_ASSETS",
    "LEVEL_PARAMS",
    "MAX_FAMILIES_FORWARD",
    "REFERENCES",
    "SCREEN_ASSETS",
    "SCREEN_MAX_DRAWDOWN",
    "SCREEN_MIN_PROFIT_FACTOR",
    "SCREEN_MIN_TRADES",
    "TIMEFRAME",
    "TIME_STOP_BARS",
    "WINDOW_PARAMS",
    "EdgeFamily",
    "Horizon",
    "Variant",
    "asset_for",
    "deployed_definition",
    "doubled",
    "forwarded",
    "ranking_key",
    "reference_definition",
    "shows_signal",
    "study_definition",
]

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEPLOYED_FIXTURE: Final[Path] = ROOT / "tests/fixtures/deployed_risk_v2_definition.json"
"""The same deployed Risk V2 definition M13 through M21 built every run from."""

TIMEFRAME: Final[Timeframe] = Timeframe.H4
TIME_STOP_BARS: Final[int] = 42
"""Seven days at 4h. Declared here because it bounds every result this milestone produces."""

SCREEN_ASSETS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT")
EXTENSION_ASSETS: Final[tuple[str, ...]] = ("BNBUSDT", "SOLUSDT")
"""Run only for families the screen carried forward. ADA and XRP are out of scope entirely."""


class EdgeFamily(StrEnum):
    """The six questions. Declaration order is the order every table reports them in."""

    TREND = "trend"
    BREAKOUT = "breakout"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    REGIME_SWITCH = "regime_switch"
    VOL_FILTERED = "vol_filtered"


class Horizon(StrEnum):
    """Which of a family's two variants this is."""

    BASE = "base"
    DOUBLED = "doubled"


# --- The doubling rule --------------------------------------------------------------------------

WINDOW_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "period",
        "slope_bars",
        "lookback",
        "window",
        "entry_lookback",
        "exit_lookback",
        "trend_period",
        "er_window",
        "short_vol",
        "long_vol",
        "vol_window",
    }
)
"""Counted in bars. Doubling one asks the same rule over a longer horizon."""

LEVEL_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "entry_z",
        "exit_z",
        "band_z",
        "er_min",
        "er_max",
        "er_trend",
        "er_range",
        "max_ratio",
        "threshold",
        "oversold",
        "exit_level",
        "fast_period",
        "slow_period",
    }
)
"""Held fixed. A threshold is where a rule acts, not how far back it looks, and moving one
would produce a different rule rather than the same rule at a different horizon.

``fast_period`` and ``slow_period`` are listed so the benchmarks can be passed through
unchanged; no benchmark is ever doubled."""

Params = tuple[tuple[Text, Text], ...]


def doubled(params: Params) -> Params:
    """Return ``params`` with every window doubled and every threshold untouched.

    Raises:
        ValueError: If a parameter is in neither set. Leaving an unclassified name alone
            would silently produce a hybrid — a rule with one window stretched and another
            not — which is exactly the kind of quiet tuning this milestone exists to exclude.
    """
    out: list[tuple[Text, Text]] = []
    for name, value in params:
        if name in WINDOW_PARAMS:
            out.append((name, str(int(value) * 2)))
        elif name in LEVEL_PARAMS:
            out.append((name, value))
        else:
            msg = f"no doubling rule for parameter {name!r}: classify it as a window or a level"
            raise ValueError(msg)
    return tuple(out)


# --- The candidates -----------------------------------------------------------------------------


class Variant(DomainModel):
    """One configuration under test: a family, a horizon, and the rule at those numbers.

    There is deliberately no symbol here. The same numbers run on every market, and the model
    gives per-asset fitting nowhere to live.
    """

    key: Text
    family: EdgeFamily
    horizon: Horizon
    candidate: SprintCandidate


_FAMILY_RULE: Final[tuple[tuple[str, EdgeFamily, str], ...]] = (
    ("T", EdgeFamily.TREND, "ema_slope"),
    ("B", EdgeFamily.BREAKOUT, "breakout_trend"),
    ("M", EdgeFamily.MOMENTUM, "momentum_roc"),
    ("R", EdgeFamily.MEAN_REVERSION, "zscore_revert"),
    ("G", EdgeFamily.REGIME_SWITCH, "regime_switch"),
    ("V", EdgeFamily.VOL_FILTERED, "vol_filtered_momentum"),
)
"""One rule per family, and the letter its two variants are keyed by."""


def _m13(strategy_id: str) -> SprintCandidate:
    return next(c for c in CANDIDATES if c.strategy_id == strategy_id)


def _pair(letter: str, family: EdgeFamily, strategy_id: str) -> tuple[Variant, Variant]:
    base = _m13(strategy_id)
    long = base.model_copy(update={"params": doubled(base.params), "neighbours": ()})
    return (
        Variant(key=f"{letter}1", family=family, horizon=Horizon.BASE, candidate=base),
        Variant(key=f"{letter}2", family=family, horizon=Horizon.DOUBLED, candidate=long),
    )


CANDIDATES_M22: Final[tuple[Variant, ...]] = tuple(
    variant for args in _FAMILY_RULE for variant in _pair(*args)
)
"""Twelve configurations: six families, each at M13's horizon and at twice it."""

REFERENCES: Final[tuple[SprintCandidate, ...]] = (
    _m13("ema_trend").model_copy(update={"strategy_id": "ema_trend_mtf"}),
    _m13("breakout").model_copy(update={"strategy_id": "breakout_mtf"}),
    _m13("regime_trend"),
)
"""Not candidates, and never judged as such. The two benchmarks paper is compared against,
plus the incumbent M15 and M16 left standing — re-run here under this milestone's own
instrument so that "better than what we already have" is a like-for-like comparison rather
than a number carried over from a run made under a different risk policy."""


# --- The screen ---------------------------------------------------------------------------------

SCREEN_MIN_TRADES: Final[int] = 30
"""The platform's existing meaningful-sample threshold, used unchanged."""

SCREEN_MIN_PROFIT_FACTOR: Final[Decimal] = Decimal("1.0")
"""Below one the rule gives back more than it takes, whatever its headline return says."""

SCREEN_MAX_DRAWDOWN: Final[Decimal] = Decimal("0.35")
"""Looser than the verdict's 15%, and knowingly so. These are nine-year continuous runs on
long-only crypto under 1% risk per trade; a ceiling set at the verdict's would cut candidates
for the length of the window rather than for the quality of the rule. Anything above 35% is
not worth cross-asset compute, because nothing there could reach PROMISING later."""

MAX_FAMILIES_FORWARD: Final[int] = 3
"""At most three families get cross-asset compute, as asked. If more clear the gate, the
ranking below decides which — and it cannot see a return."""


def shows_signal(
    *,
    trades: int,
    total_return: Decimal,
    profit_factor: Decimal | None,
    max_drawdown: Decimal,
) -> bool:
    """Say whether one configuration on one market is worth more compute.

    Deliberately looser than the verdict: this decides where hours go, not what anything
    means. A profit factor of ``None`` — no losing trade at all — passes, because reading an
    undefined ratio as failure would discard the one case it cannot be computed for.
    """
    return (
        trades >= SCREEN_MIN_TRADES
        and total_return > 0
        and (profit_factor is None or profit_factor >= SCREEN_MIN_PROFIT_FACTOR)
        and max_drawdown <= SCREEN_MAX_DRAWDOWN
    )


def forwarded(passing: set[tuple[str, str]]) -> tuple[EdgeFamily, ...]:
    """Return the families one of whose variants cleared the gate on **both** screen markets.

    Args:
        passing: ``(variant key, asset)`` pairs that passed :func:`shows_signal`.

    A family carried by T1 on BTC and T2 on ETH does not advance. That pattern is per-asset
    fitting arrived at by accident: no single configuration worked on both markets, and the
    thing being carried forward would be the choice of which variant to use where.
    """
    markets = set(SCREEN_ASSETS)
    covered = {
        key for key in {k for k, _ in passing} if {a for k, a in passing if k == key} >= markets
    }
    families = {v.family for v in CANDIDATES_M22 if v.key in covered}
    return tuple(family for family in EdgeFamily if family in families)


def ranking_key(
    *, positive_years: int, walk_forward_share: Decimal, trades: int
) -> tuple[int, Decimal, int]:
    """Return the order families are picked in when more than three clear the gate.

    Consistency first, then consistency out of sample, then sample size. **Return is not in
    it**, which is the whole point: if the tie-break could read a return, choosing the top
    three would be choosing by return with an extra step in front of it.
    """
    return (positive_years, walk_forward_share, trades)


# --- Definitions ---------------------------------------------------------------------------------


def asset_for(raw: str) -> Asset:
    """Return the M16 asset record for a raw symbol, so the dataset story is the same one."""
    return next(asset for asset in ASSETS if asset.raw == raw)


@cache
def _base() -> ExperimentDefinition:
    return ExperimentDefinition.model_validate_json(DEPLOYED_FIXTURE.read_text(encoding="utf-8"))


def _definition(
    raw: str, strategy_id: str, params: Params, *, key: str, latching: bool
) -> ExperimentDefinition:
    asset, base = asset_for(raw), _base()
    version = build_research_registry().metadata_for(strategy_id).version
    span = WindowSpec(start=asset.start, end=DATA_END)
    at_timeframe = risk_for_timeframe(base.risk, TIMEFRAME)
    risk = at_timeframe if latching else risk_configuration_for(REFERENCE, at_timeframe)
    dataset = base.dataset.model_copy(
        update={
            "symbol": asset.symbol,
            "symbol_rules": symbol_rules_for(asset),
            "market_type": MarketType.SPOT,
            "timeframe": TIMEFRAME,
            "start": span.start,
            "end": span.end,
            "source": "binance_vision_m16",
        }
    )
    suffix = "deployed" if latching else "ref"
    copy = base.model_copy(
        update={
            "name": f"m22-{raw}-{key}-{strategy_id}-{suffix}",
            "strategy": StrategySpec(
                strategy_id=strategy_id, strategy_version=version, params=params
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": TIMEFRAME}),
            "risk": risk,
            "role": ExperimentRole.IN_SAMPLE,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


def study_definition(raw: str, variant: Variant) -> ExperimentDefinition:
    """Return the screening definition: this variant, this market, research variant risk."""
    return _definition(
        raw,
        variant.candidate.strategy_id,
        variant.candidate.params,
        key=variant.key,
        latching=False,
    )


def deployed_definition(raw: str, variant: Variant) -> ExperimentDefinition:
    """Return the same run under deployed Risk V2, which PAPER CANDIDATE cannot be had without."""
    return _definition(
        raw,
        variant.candidate.strategy_id,
        variant.candidate.params,
        key=variant.key,
        latching=True,
    )


def reference_definition(raw: str, reference: SprintCandidate) -> ExperimentDefinition:
    """Return a benchmark or the incumbent, on the same market under the same instrument."""
    role = "bench" if reference.family is Family.BENCHMARK else "incumbent"
    return _definition(raw, reference.strategy_id, reference.params, key=role, latching=False)
