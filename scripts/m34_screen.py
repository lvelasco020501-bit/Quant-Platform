"""M34: three portfolios over frozen edges, on M32's point-in-time corrected universe at 4h.

Signals come from the frozen strategies driven through the production feature pipeline; the
per-market eligibility is M32's liquidity rule unchanged; the allocation is the one declared in
``research/m34.py`` and is the only thing this milestone adds. What Risk V2's stop does to these
rules is measured in ``m34_verify.py`` and declared, not modelled here.

Five passes per portfolio: base, cost x2, cost x3, out of sample, and the two breadth neighbours
that make up the sensitivity condition. The masks are computed once and reused across all of
them, because a signal timeline does not depend on what the portfolio pays or how wide it
allocates.

Usage:
    uv run python scripts/m34_screen.py [--only B2] [--markets 6]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m32 import ASSETS_M30
from quantplatform.research.m34 import (
    COST_STRESS_MULTIPLIERS,
    LIQUIDITY_WINDOW,
    NEIGHBOUR_UNIVERSE_SIZES,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    SLEEVES,
    TIMEFRAME,
    UNIVERSE_SIZE,
    Combined,
    Portfolio,
    pool_symbols,
    sleeves_of,
    survives,
)
from quantplatform.research.portfolio import (
    Allocation,
    Holding,
    PortfolioRun,
    SleeveContribution,
    positions,
    simulate_portfolio,
    targets_for,
)
from quantplatform.research.rotation import (
    align,
    buy_and_hold,
    eligible_universe,
    equal_weight_basket,
    max_drawdown,
    yearly_returns,
)
from quantplatform.research.sleeve import long_intervals, long_mask
from quantplatform.strategies.research import build_research_registry

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m34"
MIN_OBSERVATIONS: Final[int] = 2
"""Fewest points that can carry a correlation."""
DRAWDOWN_CAP: Final[Decimal] = Decimal("0.35")
"""The declared cap, read here only to judge the breadth neighbours."""


def load(raw: str) -> tuple[MarketBar, ...]:
    """Read one market's canonical 4h series, re-validating every row through the model."""
    folder = "data/raw/m16/out" if raw in ASSETS_M30 else "data/raw/m32/out"
    path = next((ROOT / folder).glob(f"{raw}_{TIMEFRAME.value}_*.csv"))
    bars: list[MarketBar] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bars.append(
                MarketBar(
                    symbol=row["symbol"],
                    market_type=MarketType(row["market_type"]),
                    timeframe=Timeframe(row["timeframe"]),
                    open_time=datetime.fromisoformat(row["open_time"]),
                    close_time=datetime.fromisoformat(row["close_time"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                    quote_volume=Decimal(row["quote_volume"]) if row.get("quote_volume") else None,
                    trade_count=int(row["trade_count"]) if row.get("trade_count") else None,
                    source="m34_screen",
                    is_closed=True,
                )
            )
    return tuple(bars)


def _single_year_share(run: PortfolioRun) -> Decimal | None:
    """Return the share of net profit the best calendar year accounts for."""
    if not run.equity_curve:
        return None
    opening = run.initial_equity
    deltas: list[Decimal] = []
    for year in sorted({point.at.year for point in run.equity_curve}):
        closing = [p.equity for p in run.equity_curve if p.at.year == year][-1]
        deltas.append(closing - opening)
        opening = closing
    total = sum(deltas, start=Decimal(0))
    return max(deltas) / total if total > 0 and deltas else None


def _profit_factor(returns: tuple[Decimal, ...]) -> Decimal | None:
    """Return gross profit over gross loss across closed episodes."""
    won = sum((r for r in returns if r > 0), start=Decimal(0))
    lost = sum((-r for r in returns if r < 0), start=Decimal(0))
    return None if lost == 0 else won / lost


def _share(contributions: Sequence[SleeveContribution]) -> Decimal | None:
    """Return the share of net profit the best contributor accounts for."""
    values = [c.net_profit for c in contributions]
    total = sum(values, start=Decimal(0))
    return max(values) / total if total > 0 and values else None


def card(run: PortfolioRun) -> dict[str, Any]:
    """Return every figure one portfolio run offers, with no judgement applied."""
    total = run.total_return
    drawdown = max_drawdown(run.equity_curve)
    annual = cagr(total, bars=run.bars, timeframe=TIMEFRAME)
    years = yearly_returns(run.equity_curve, run.initial_equity)
    return {
        "total_return": total,
        "cagr": annual,
        "max_drawdown": drawdown,
        "calmar": calmar(annual, drawdown),
        "profit_factor": _profit_factor(run.episode_returns),
        "episodes": len(run.episode_returns),
        "bars": run.bars,
        "exposure": Decimal(run.bars_held) / Decimal(run.bars) if run.bars else None,
        "time_in_cash": Decimal(run.bars_in_cash) / Decimal(run.bars) if run.bars else None,
        "turnover": run.turnover,
        "fees": run.cost_paid,
        "final_equity": run.final_equity,
        "top_asset_share": _share(run.by_asset),
        "top_sleeve_share": _share(run.by_sleeve) if len(run.by_sleeve) > 1 else None,
        "by_asset": {c.name: c.net_profit for c in run.by_asset if c.episodes},
        "by_sleeve": {c.name: c.net_profit for c in run.by_sleeve},
        "assets_touched": sum(1 for c in run.by_asset if c.episodes),
        "years": years,
        "single_year_share": _single_year_share(run),
    }


def _returns(run: PortfolioRun) -> list[Decimal]:
    """Return the equity curve's bar-to-bar returns."""
    out: list[Decimal] = []
    previous = run.initial_equity
    for point in run.equity_curve:
        out.append(point.equity / previous - 1 if previous else Decimal(0))
        previous = point.equity
    return out


def _correlation(left: list[Decimal], right: list[Decimal]) -> Decimal | None:
    """Return the Pearson correlation of two return series."""
    pairs = list(zip(left, right, strict=False))
    if len(pairs) < MIN_OBSERVATIONS:
        return None
    n = Decimal(len(pairs))
    mx = sum((a for a, _ in pairs), start=Decimal(0)) / n
    my = sum((b for _, b in pairs), start=Decimal(0)) / n
    cov = sum(((a - mx) * (b - my) for a, b in pairs), start=Decimal(0))
    vx = sum(((a - mx) ** 2 for a, _ in pairs), start=Decimal(0))
    vy = sum(((b - my) ** 2 for _, b in pairs), start=Decimal(0))
    return None if vx == 0 or vy == 0 else cov / (vx * vy).sqrt()


def _drawdown_series(run: PortfolioRun) -> list[Decimal]:
    """Return the fall from the running peak at each bar."""
    peak = run.initial_equity
    out: list[Decimal] = []
    for point in run.equity_curve:
        peak = max(peak, point.equity)
        out.append((peak - point.equity) / peak if peak > 0 else Decimal(0))
    return out


def _one_portfolio(
    portfolio: Portfolio,
    series: dict[str, tuple[MarketBar, ...]],
    masks: dict[Holding, tuple[bool, ...]],
    universes: dict[int, tuple[frozenset[str], ...]],
    cost: Decimal,
) -> dict[str, Any]:
    """Run every declared pass for one portfolio and judge it."""
    keys = [s.key for s in sleeves_of(portfolio)]
    mine = {h: m for h, m in masks.items() if h[0] in keys}
    allocation = Allocation(universe_size=UNIVERSE_SIZE, sleeves=len(keys))
    targets = targets_for(mine, universes[UNIVERSE_SIZE], allocation)
    entry: dict[str, Any] = {
        "sleeves": keys,
        "per_signal": str(allocation.per_signal),
        "per_asset_cap": str(allocation.per_asset_cap),
        "base": card(
            simulate_portfolio(series, targets, cost_basis_points=cost, holdings=list(mine))
        ),
    }
    for multiplier in COST_STRESS_MULTIPLIERS:
        entry[f"cost_x{multiplier}"] = card(
            simulate_portfolio(
                series, targets, cost_basis_points=cost * multiplier, holdings=list(mine)
            )
        )
    entry["oos"] = card(
        simulate_portfolio(
            series, targets, cost_basis_points=cost, start=OOS_START, holdings=list(mine)
        )
    )
    neighbours: dict[str, Any] = {}
    for size in NEIGHBOUR_UNIVERSE_SIZES:
        wider = Allocation(universe_size=size, sleeves=len(keys))
        neighbours[str(size)] = card(
            simulate_portfolio(
                series,
                targets_for(mine, universes[size], wider),
                cost_basis_points=cost,
                holdings=list(mine),
            )
        )
    entry["neighbours"] = neighbours
    base = entry["base"]
    passed, reasons = survives(
        Combined(
            annual=base["cagr"],
            max_drawdown=base["max_drawdown"],
            calmar_ratio=base["calmar"],
            single_year_share=base["single_year_share"],
            top_asset_share=base["top_asset_share"],
            top_sleeve_share=base["top_sleeve_share"],
            annual_at_double_cost=entry["cost_x2"]["cagr"],
            annual_at_triple_cost=entry["cost_x3"]["cagr"],
            out_of_sample_return=entry["oos"]["total_return"],
            neighbours_positive=all((n["cagr"] or Decimal(-1)) > 0 for n in neighbours.values()),
            neighbours_within_drawdown=all(
                n["max_drawdown"] <= DRAWDOWN_CAP for n in neighbours.values()
            ),
        )
    )
    entry["passes"] = passed
    entry["failed"] = [r.value for r in reasons]
    return entry


def _solo(
    key: str,
    series: dict[str, tuple[MarketBar, ...]],
    masks: dict[Holding, tuple[bool, ...]],
    universe: tuple[frozenset[str], ...],
    cost: Decimal,
) -> PortfolioRun:
    """Return one sleeve run alone over the whole eligible universe."""
    mine = {h: m for h, m in masks.items() if h[0] == key}
    return simulate_portfolio(
        series,
        targets_for(mine, universe, Allocation(universe_size=UNIVERSE_SIZE, sleeves=1)),
        cost_basis_points=cost,
        holdings=list(mine),
    )


def _on_btc(
    key: str,
    series: dict[str, tuple[MarketBar, ...]],
    masks: dict[Holding, tuple[bool, ...]],
    slots: int,
    cost: Decimal,
) -> dict[str, Any]:
    """Return one sleeve run on BTC alone, through this milestone's own machinery."""
    holding: Holding = (key, "BTCUSDT")
    return card(
        simulate_portfolio(
            {"BTCUSDT": series["BTCUSDT"]},
            targets_for(
                {holding: masks[holding]},
                [frozenset({"BTCUSDT"})] * slots,
                Allocation(universe_size=1, sleeves=1),
            ),
            cost_basis_points=cost,
            holdings=[holding],
        )
    )


def main() -> int:
    """Build every declared portfolio, judge it, and write one report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=0, help="cap the pool, for a smoke run")
    args = parser.parse_args()

    symbols = list(pool_symbols())[: args.markets or None]
    series = {raw: load(raw) for raw in symbols}
    grid = align(series)
    slots = positions(series, grid)
    sys.stdout.write(
        f"{len(series)} markets, {sum(len(b) for b in series.values())} {TIMEFRAME.value} bars, "
        f"grid {len(grid)}\n"
    )
    sys.stdout.flush()

    universes = {
        size: eligible_universe(series, slots, window=LIQUIDITY_WINDOW, size=size)
        for size in (UNIVERSE_SIZE, *NEIGHBOUR_UNIVERSE_SIZES)
    }
    ever = sorted({a for slot in universes[UNIVERSE_SIZE] for a in slot})
    sys.stdout.write(f"ever eligible at breadth {UNIVERSE_SIZE}: {len(ever)} markets\n\n")

    registry = build_research_registry()
    masks: dict[Holding, tuple[bool, ...]] = {}
    started = time.time()
    for probe in SLEEVES:
        strategy = registry.create(probe.candidate.strategy_id, dict(probe.candidate.params))
        for raw in symbols:
            at = time.time()
            intervals = long_intervals(strategy, series[raw], pipelines=features_for)
            masks[probe.key, raw] = long_mask(intervals, grid)
            sys.stdout.write(
                f"  {probe.key:3} {raw:11} {len(intervals):4d} stretches "
                f"({time.time() - at:4.1f}s)\n"
            )
            sys.stdout.flush()
    sys.stdout.write(f"masks in {time.time() - started:.0f}s\n\n")

    cost = ONE_WAY_COST_BASIS_POINTS
    results: dict[str, Any] = {}
    for portfolio in Portfolio:
        entry = _one_portfolio(portfolio, series, masks, universes, cost)
        results[portfolio.value] = entry
        base = entry["base"]
        sys.stdout.write(
            f"  {portfolio.value:26} cagr {float(base['cagr'] or 0):+7.2%} "
            f"dd {float(base['max_drawdown']):6.2%} calmar {float(base['calmar'] or 0):5.2f} "
            f"{'PASS' if entry['passes'] else ','.join(entry['failed'])}\n"
        )
        sys.stdout.flush()

    # --- how alike are the two sleeves? ---------------------------------------------------
    solo = {
        probe.key: _solo(probe.key, series, masks, universes[UNIVERSE_SIZE], cost)
        for probe in SLEEVES
    }
    first, second = (solo[probe.key] for probe in SLEEVES)
    falls = (_drawdown_series(first), _drawdown_series(second))
    diversification = {
        "sleeves": [probe.key for probe in SLEEVES],
        "return_correlation": _correlation(_returns(first), _returns(second)),
        "drawdown_correlation": _correlation(*falls),
        "bars_both_in_drawdown": sum(1 for x, y in zip(*falls, strict=False) if x > 0 and y > 0),
        "bars_either_in_drawdown": sum(1 for x, y in zip(*falls, strict=False) if x > 0 or y > 0),
        "bars": min(first.bars, second.bars),
        "signal_overlap_bars": sum(
            1
            for slot in range(len(grid))
            if any(masks[SLEEVES[0].key, r][slot] for r in symbols)
            and any(masks[SLEEVES[1].key, r][slot] for r in symbols)
        ),
        "solo": {
            probe.key: {
                "cagr": cagr(
                    solo[probe.key].total_return,
                    bars=solo[probe.key].bars,
                    timeframe=TIMEFRAME,
                ),
                "max_drawdown": max_drawdown(solo[probe.key].equity_curve),
            }
            for probe in SLEEVES
        },
    }
    benchmarks: dict[str, Any] = {
        "b2_btc_alone": _on_btc("B2", series, masks, len(grid), cost),
        "regime_trend_btc_alone": _on_btc("G1", series, masks, len(grid), cost),
    }
    basket = equal_weight_basket(series, cost_basis_points=cost)
    hold = buy_and_hold(series["BTCUSDT"], cost_basis_points=cost)
    for name, run in (("equal_weight_basket", basket), ("buy_and_hold_btc", hold)):
        drawdown = max_drawdown(run.equity_curve)
        annual = cagr(run.total_return, bars=run.bars, timeframe=TIMEFRAME)
        benchmarks[name] = {
            "total_return": run.total_return,
            "cagr": annual,
            "max_drawdown": drawdown,
            "calmar": calmar(annual, drawdown),
            "turnover": run.turnover,
        }

    report = {
        "milestone": "m34",
        "phase": "portfolio",
        "timeframe": TIMEFRAME.value,
        "basis": "frozen-strategy signal timelines; Risk V2 stop not modelled -- see m34.py",
        "pool": symbols,
        "ever_eligible": ever,
        "universe_size": UNIVERSE_SIZE,
        "liquidity_window": LIQUIDITY_WINDOW,
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "neighbour_universe_sizes": list(NEIGHBOUR_UNIVERSE_SIZES),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "portfolios": results,
        "benchmarks": benchmarks,
        "diversification": diversification,
    }
    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / f"portfolio_{TIMEFRAME.value}{'-partial' if args.markets else ''}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n-> {out.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
