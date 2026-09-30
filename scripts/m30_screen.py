"""M30 phase 1 at 1d: run every declared rotation rule and record what the gates need.

One worker, by instruction. Nothing here decides anything -- it measures, and
:func:`~quantplatform.research.m30.survives` judges afterwards from the JSON this writes.

Eleven passes per rule, every one of them declared in ``research/m30.py`` before this ran:

    base            full history, 15 bps a side
    cost x2, x3     the declared cost stress; x2 is the one that matters
    oos             2024-01-01 onward, M23's window, reused unchanged
    walk-forward    five contiguous windows, the identical rule in each
    neighbours      half and double the lookback, the sensitivity probe

Plus the benchmarks: buy-and-hold BTC and the static equal-weight basket, both through this
same simulator, and B2 and regime_trend on BTC through the **production engine**, so the
comparison against the incumbent is not made against a reimplementation of it.

Usage:
    uv run python scripts/m30_screen.py [--keys RS1,CS1] [--probe]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.research.m29 import COST_STRESS_MULTIPLIERS, cagr, calmar
from quantplatform.research.m30 import (
    ASSETS_M30,
    MIN_FOLD_TRADES,
    NEIGHBOUR_LOOKBACKS,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    RULES_M30,
    WALK_FORWARD_FOLDS,
    RotationRule,
)
from quantplatform.research.rotation import (
    RotationRun,
    RotationSpec,
    Series,
    align,
    buy_and_hold,
    equal_weight_basket,
    max_drawdown,
    pairwise_correlation,
    profit_factor,
    simulate,
    yearly_returns,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m30"
DATA: Final[Path] = ROOT / "data/raw/m30/out"
TIMEFRAME: Final[Timeframe] = Timeframe.D1


def load(raw: str) -> tuple[MarketBar, ...]:
    """Read one market's canonical daily series, re-validating every row through the model."""
    path = next(DATA.glob(f"{raw}_1d_*.csv"))
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
                    source=row.get("source") or "m30_screen",
                    is_closed=True,
                )
            )
    return tuple(bars)


def spec_of(rule: RotationRule, *, lookback: int | None = None) -> RotationSpec:
    """Return the simulator spec for a declared rule, optionally at a neighbour's lookback."""
    return RotationSpec(
        lookback=rule.lookback if lookback is None else lookback,
        hold=rule.hold,
        normalised=rule.volatility_normalised,
        threshold=rule.entry_threshold,
        regime_filter=rule.regime_filter,
        vol_window=rule.vol_window,
    )


def card(run: RotationRun) -> dict[str, Any]:
    """Return every figure one run offers, with no judgement applied to any of it."""
    total = run.total_return
    drawdown = max_drawdown(run.equity_curve)
    annual = cagr(total, bars=run.bars, timeframe=TIMEFRAME)
    years = yearly_returns(run.equity_curve, run.initial_equity)
    contributions = {c.asset: c.net_profit for c in run.contributions}
    net_total = sum(contributions.values(), start=Decimal(0))
    positive = [value for value in contributions.values() if value > 0]
    held = sorted(c.asset for c in run.contributions if c.episodes > 0)
    return {
        "total_return": total,
        "cagr": annual,
        "max_drawdown": drawdown,
        "calmar": calmar(annual, drawdown),
        "profit_factor": profit_factor(run.episodes),
        "trades": len(run.episodes),
        "bars": run.bars,
        "exposure": Decimal(run.bars_held) / Decimal(run.bars) if run.bars else None,
        "time_in_cash": Decimal(run.bars_in_cash) / Decimal(run.bars) if run.bars else None,
        "turnover": run.turnover,
        "fees": run.cost_paid,
        "final_equity": run.final_equity,
        "assets_positive": len(positive),
        "top_asset_share": (max(contributions.values()) / net_total) if net_total > 0 else None,
        "contributions": dict(contributions),
        "episodes_per_asset": {c.asset: c.episodes for c in run.contributions},
        "bars_per_asset": {c.asset: c.bars_held for c in run.contributions},
        "assets_held": held,
        "years": years,
        "years_positive_share": _positive_share(years),
        "single_year_share": _single_year_share(run),
    }


def _positive_share(years: dict[int, Decimal]) -> Decimal | None:
    """Return the share of calendar years that ended up."""
    if not years:
        return None
    up = sum(1 for value in years.values() if value > 0)
    return Decimal(up) / Decimal(len(years))


def _single_year_share(run: RotationRun) -> Decimal | None:
    """Return the share of net profit the best calendar year accounts for.

    Measured on equity *changes* rather than returns, so the shares sum to one -- the same
    convention M29 used, including the property that a value above one means the other years
    lost money between them.
    """
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


def folds(series: Series) -> list[tuple[datetime, datetime]]:
    """Return the declared contiguous test windows, cut from the grid by index."""
    grid = align(series)
    step = len(grid) // WALK_FORWARD_FOLDS
    return [
        (
            grid[index * step],
            grid[(index + 1) * step] if index + 1 < WALK_FORWARD_FOLDS else grid[-1],
        )
        for index in range(WALK_FORWARD_FOLDS)
    ]


def _until(series: Series, end: datetime) -> Series:
    """Return the same series truncated at ``end``, keeping all history before it."""
    return {asset: [bar for bar in bars if bar.open_time < end] for asset, bars in series.items()}


def cell(rule: RotationRule, series: Series, cost: Decimal) -> dict[str, Any]:
    """Run every declared pass for one rule."""
    spec = spec_of(rule)
    entry: dict[str, Any] = {
        "key": rule.key,
        "family": rule.family.value,
        "lookback": rule.lookback,
        "hold": rule.hold,
        "normalised": rule.volatility_normalised,
        "threshold": rule.entry_threshold,
        "regime_filter": rule.regime_filter,
    }
    entry["base"] = card(simulate(series, spec, cost_basis_points=cost))

    for multiplier in COST_STRESS_MULTIPLIERS:
        entry[f"cost_x{multiplier}"] = card(
            simulate(series, spec, cost_basis_points=cost * multiplier)
        )

    entry["oos"] = card(simulate(series, spec, cost_basis_points=cost, start=OOS_START))

    fold_cards: list[dict[str, Any]] = []
    for start, end in folds(series):
        run = simulate(_until(series, end), spec, cost_basis_points=cost, start=start)
        fold_cards.append({"start": start.isoformat(), "end": end.isoformat(), **card(run)})
    entry["folds"] = fold_cards
    counted = [f for f in fold_cards if f["trades"] >= MIN_FOLD_TRADES]
    entry["walk_forward_positive_share"] = (
        Decimal(sum(1 for f in counted if f["total_return"] > 0)) / Decimal(len(counted))
        if counted
        else None
    )
    entry["folds_counted"] = len(counted)

    neighbours: dict[str, Any] = {}
    for lookback in NEIGHBOUR_LOOKBACKS:
        neighbours[str(lookback)] = card(
            simulate(series, spec_of(rule, lookback=lookback), cost_basis_points=cost)
        )
    entry["neighbours"] = neighbours
    factors = [n["profit_factor"] for n in neighbours.values() if n["profit_factor"] is not None]
    entry["neighbour_min_profit_factor"] = min(factors) if factors else None

    entry["selected_correlation"] = pairwise_correlation(series, entry["base"]["assets_held"])
    return entry


def benchmarks(series: Series, cost: Decimal) -> dict[str, Any]:
    """Return the two benchmarks this simulator can produce."""
    return {
        "buy_and_hold_btc": card(buy_and_hold(series["BTCUSDT"], cost_basis_points=cost)),
        "equal_weight_basket": card(equal_weight_basket(series, cost_basis_points=cost)),
    }


def main() -> int:
    """Run the declared rules and write one report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", default="", help="restrict to these rule keys, e.g. RS1,CS1")
    parser.add_argument("--probe", action="store_true", help="time one base pass and stop")
    args = parser.parse_args()
    keys = {name for name in args.keys.split(",") if name}
    rules = [rule for rule in RULES_M30 if not keys or rule.key in keys]

    series: Series = {raw: load(raw) for raw in ASSETS_M30}
    for raw, bars in series.items():
        sys.stdout.write(f"{raw:9} {len(bars):5d} daily bars from {bars[0].open_time.date()}\n")
    sys.stdout.flush()

    if args.probe:
        at = time.time()
        run = simulate(series, spec_of(RULES_M30[0]), cost_basis_points=ONE_WAY_COST_BASIS_POINTS)
        sys.stdout.write(
            f"probe: one base pass in {time.time() - at:.1f}s, "
            f"{len(run.episodes)} episodes, return {float(run.total_return):+.2%}\n"
        )
        return 0

    HOME.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cells: list[dict[str, Any]] = []
    for rule in rules:
        at = time.time()
        entry = cell(rule, series, ONE_WAY_COST_BASIS_POINTS)
        cells.append(entry)
        base = entry["base"]
        sys.stdout.write(
            f"  {rule.key:4} {rule.family.value:18} "
            f"cagr {float(base['cagr'] or 0):+7.2%} dd {float(base['max_drawdown']):6.2%} "
            f"calmar {float(base['calmar'] or 0):5.2f} trades {base['trades']:4d} "
            f"cash {float(base['time_in_cash'] or 0):5.1%}  ({time.time() - at:.1f}s)\n"
        )
        sys.stdout.flush()

    report = {
        "milestone": "m30",
        "phase": "screen",
        "timeframe": TIMEFRAME.value,
        "assets": list(ASSETS_M30),
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "walk_forward_folds": WALK_FORWARD_FOLDS,
        "neighbour_lookbacks": list(NEIGHBOUR_LOOKBACKS),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "benchmarks": benchmarks(series, ONE_WAY_COST_BASIS_POINTS),
        "cells": cells,
    }
    # A restricted run writes to its own file, so a smoke test can never overwrite a finished
    # screen's evidence. That mistake cost a 31-minute re-run in M29.
    out = HOME / f"screen_1d{'-partial' if keys else ''}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n{len(cells)} rules in {report['seconds']}s -> {out.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
