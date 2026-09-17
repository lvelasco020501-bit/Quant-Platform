"""M16 — does regime_trend at 4h work anywhere but BTC?

M15 left one combination standing: ``regime_trend`` at 4h under the research drawdown latch,
positive over six and a half years of BTC with a profit factor of 2.6 and a 2.7% drawdown — on
79 trades. Two explanations fit that evidence equally well: a real trend-following edge, or
one asset's history flattered by a strategy shaped on it. Only other markets can tell them
apart, so this milestone changes **nothing** but the market.

Held identical across all six assets, by test rather than by intention:

* the strategy and its canonical M13 parameters — no per-asset fitting, no grid search;
* the timeframe, 4h, resampled deterministically from hourly bars;
* the risk configuration: M15's 4h conversion of production Risk V2, under policy **C**
  (drawdown latch 10%, no streak breaker). **Policy C is a research variant, not Risk V2.**
* fees (10 bps) and slippage (5 bps), and the stress and walk-forward protocol.

What legitimately differs per asset: the market itself, its venue trading rules (tick size,
lot step, minimum notional — captured once and pinned into the definition), and how much
history the venue has. Each asset starts at its first *complete* month of trading, because
Binance Vision's first monthly archive is a partial listing month.

**Two windows are reported, and the difference matters.** Each asset's own maximum history
answers "does the edge exist here"; the common window every asset shares — 2020-09 onward,
where Solana begins — answers "does it exist at the same time", which is the only comparison
in which one asset's bull market cannot stand in for another's.

**Also run, and why.** Besides policy C, each asset gets the two benchmarks and one run under
deployed Risk V2 (policy A). Neither is exploration: :func:`quantplatform.research.sprint.judge`
refuses PAPER CANDIDATE to anything that cannot beat both benchmarks and stay positive under
the policy paper would actually run it under, so without those runs the milestone could not
answer whether this is a paper candidate at all.

**Declared before the results:** an asset is discarded when it never traded, lost money over
its own history, had a profit factor below one, or turned negative under the cost stress. A
pooled sample across assets is reported because the question "is the sample big enough yet"
is about the strategy, not about one market — but it never *grants* a verdict: verdicts stay
per experiment, since a pooled figure hides the one asset that carried it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.base import DomainModel
from quantplatform.core.models.market import SymbolRules
from quantplatform.research.definition import (
    ExperimentDefinition,
    ExperimentRole,
    StrategySpec,
    canonical_json,
)
from quantplatform.research.folds import Fold, WindowSpec
from quantplatform.research.latch_policy import LatchPolicy, risk_configuration_for
from quantplatform.research.m14 import POLICIES as M14_POLICIES
from quantplatform.research.m14 import STUDY_STRATEGIES as M14_STRATEGIES
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.sprint import Family, SprintCandidate
from quantplatform.strategies.research import build_research_registry

__all__ = [
    "ASSETS",
    "BENCHMARKS",
    "COMMON_START",
    "DATASET_SOURCE",
    "DATA_END",
    "DEPLOYED_POLICY",
    "POLICY",
    "STRATEGY",
    "TIMEFRAME",
    "Asset",
    "Pooled",
    "discard_reason",
    "pool",
    "study_definition",
    "symbol_rules_for",
    "walk_forward_folds_for",
    "years_for",
]

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
RULES_DIR: Final[Path] = ROOT / "tests/fixtures/m16_symbol_rules"
DATASET_SOURCE: Final[str] = "binance_vision_m16"
TIMEFRAME: Final[Timeframe] = Timeframe.H4
DATA_END: Final[datetime] = datetime(2026, 9, 15, tzinfo=UTC)
"""The same last day as M15, so BTC's numbers are comparable bar for bar."""


class Asset(DomainModel):
    """One market in the study, and how far back the venue's own archive goes."""

    symbol: str
    raw: str
    listing_month: str
    """Binance Vision's first monthly archive: a partial month, so not where the data starts."""

    start: datetime
    """The first complete month of trading — the month after :attr:`listing_month`."""


def _month_after(stamp: str) -> datetime:
    year, month = (int(part) for part in stamp.split("-"))
    return datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)


def _asset(symbol: str, listing_month: str) -> Asset:
    raw = symbol.replace("/", "")
    return Asset(
        symbol=symbol, raw=raw, listing_month=listing_month, start=_month_after(listing_month)
    )


ASSETS: Final[tuple[Asset, ...]] = (
    _asset("BTC/USDT", "2017-08"),
    _asset("ETH/USDT", "2017-08"),
    _asset("BNB/USDT", "2017-11"),
    _asset("SOL/USDT", "2020-08"),
    _asset("XRP/USDT", "2018-05"),
    _asset("ADA/USDT", "2018-04"),
)
"""The six markets, in the order asked for. Listing months are what Binance Vision publishes,
read from its archive index rather than assumed."""

COMMON_START: Final[datetime] = max(asset.start for asset in ASSETS)
"""Where every asset exists at once — Solana's start. The only window in which the six can be
compared without one market's era standing in for another's."""

POLICY: Final[LatchPolicy] = next(p for p in M14_POLICIES if p.key == "C")
DEPLOYED_POLICY: Final[LatchPolicy] = next(p for p in M14_POLICIES if p.key == "A")
STRATEGY: Final[SprintCandidate] = next(
    c for c in M14_STRATEGIES if c.strategy_id == "regime_trend"
)
BENCHMARKS: Final[tuple[SprintCandidate, ...]] = tuple(
    c for c in M14_STRATEGIES if c.family is Family.BENCHMARK
)


@cache
def symbol_rules_for(asset: Asset) -> SymbolRules:
    """Return the venue rules captured for ``asset`` and pinned into every definition."""
    raw = json.loads((RULES_DIR / f"{asset.raw}.json").read_text(encoding="utf-8"))
    return SymbolRules.model_validate(raw)


def years_for(asset: Asset) -> tuple[WindowSpec, ...]:
    """Return calendar years from the asset's first complete month to the end of the data.

    The first and last windows are short by construction: a year is the unit stability is
    judged in, not a claim that every window holds twelve months.
    """
    windows: list[WindowSpec] = []
    cursor = asset.start
    while cursor < DATA_END:
        following = min(DATA_END, datetime(cursor.year + 1, 1, 1, tzinfo=UTC))
        windows.append(WindowSpec(start=cursor, end=following))
        cursor = following
    return tuple(windows)


def walk_forward_folds_for(asset: Asset) -> tuple[Fold, ...]:
    """Return this asset's folds: train on one year, test on the next. Nothing is fitted."""
    years = years_for(asset)
    return tuple(
        Fold(index=i, train=train, test=test) for i, (train, test) in enumerate(pairwise(years))
    )


def study_definition(
    asset: Asset,
    candidate: SprintCandidate,
    *,
    base: ExperimentDefinition,
    policy: LatchPolicy,
    window: WindowSpec | None,
) -> ExperimentDefinition:
    """Return the definition for one asset, strategy, policy and window (``None``: all of it)."""
    version = build_research_registry().metadata_for(candidate.strategy_id).version
    span = window if window is not None else WindowSpec(start=asset.start, end=DATA_END)
    label = "full" if window is None else str(span.start.year)
    role = (
        ExperimentRole.BENCHMARK
        if candidate.family is Family.BENCHMARK
        else ExperimentRole.IN_SAMPLE
    )
    dataset = base.dataset.model_copy(
        update={
            "symbol": asset.symbol,
            "symbol_rules": symbol_rules_for(asset),
            "market_type": MarketType.SPOT,
            "timeframe": TIMEFRAME,
            "start": span.start,
            "end": span.end,
            "source": DATASET_SOURCE,
        }
    )
    copy = base.model_copy(
        update={
            "name": f"m16-{asset.raw}-{candidate.strategy_id}-{policy.key}-{label}",
            "strategy": StrategySpec(
                strategy_id=candidate.strategy_id,
                strategy_version=version,
                params=candidate.params,
            ),
            "dataset": dataset,
            "backtest": base.backtest.model_copy(update={"timeframe": TIMEFRAME}),
            "risk": risk_configuration_for(policy, risk_for_timeframe(base.risk, TIMEFRAME)),
            "role": role,
        }
    )
    return ExperimentDefinition.model_validate_json(canonical_json(copy))


class Pooled(DomainModel):
    """Every asset's trades counted as one sample, with the ratios rebuilt from the totals."""

    trades: int
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    """Average result per trade, in quote currency, after costs — never an average of averages."""


def pool(cards: list[dict[str, Any]]) -> Pooled:
    """Return one sample built from many scorecards, recomputed rather than averaged."""
    trades = sum(int(card["trades"]) for card in cards)
    profit = sum((Decimal(str(card["gross_profit"])) for card in cards), Decimal(0))
    loss = sum((Decimal(str(card["gross_loss"])) for card in cards), Decimal(0))
    return Pooled(
        trades=trades,
        gross_profit=profit,
        gross_loss=loss,
        profit_factor=(profit / loss) if loss > 0 else None,
        expectancy=((profit - loss) / trades) if trades else None,
    )


def discard_reason(
    *,
    trades: int,
    net: Decimal,
    profit_factor: Decimal | None,
    worst_stress: Decimal | None,
) -> str | None:
    """Return why this asset is discarded, or ``None`` to keep it. Fixed before the results."""
    if trades == 0:
        return "never traded"
    if net <= 0:
        return "lost money over its own history"
    if profit_factor is not None and profit_factor < 1:
        return "profit factor below one"
    if worst_stress is not None and worst_stress <= 0:
        return "negative under the cost stress"
    return None
