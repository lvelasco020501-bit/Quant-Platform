"""M33 phase 1 at 1d: run the three compression rules through the production engine.

One worker, by instruction. **This is the certified path**: ``BacktestEngine`` with Risk V2,
stop-based sizing, order rejection and venue minimums, the same chain every promoted strategy
runs. M30 to M32 needed a second backtester because a cross-sectional rule cannot be expressed
single-symbol; nothing here needs one, and so nothing here carries that uncertainty.

Seven passes per configuration per market, every one declared in ``research/m33.py`` before this
ran:

    base            full history, research risk -- the rule measured rather than the breakers
    cost x2, x3     the declared cost stress; both must stay positive
    oos             2024-01-01 onward, M23's window, reused unchanged
    deployed        latching Risk V2, which no PAPER CANDIDATE can be had without
    neighbours      the measurement window halved and doubled, for the sensitivity condition

Usage:
    uv run python scripts/m33_screen.py [--only BTCUSDT] [--keys B1,V1] [--probe]
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
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.m33 import (
    ASSETS_M33,
    COMPRESSION_THRESHOLDS,
    COST_STRESS_MULTIPLIERS,
    NEIGHBOUR_SHORT_WINDOWS,
    OOS_START,
    TIMEFRAME,
    VARIANTS_M33,
    Compressed,
    Variant,
    advances,
    cagr,
    calmar,
    definition_for,
    neighbour_of,
    survives,
)
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import Scorecard
from quantplatform.strategies.research import build_research_registry

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m33"
DATA: Final[Path] = ROOT / "data/raw/m30/out"
MIN_OOS_BARS: Final[int] = 30


def load(raw: str) -> tuple[MarketBar, ...]:
    """Read one market's canonical daily series, re-validating every row through the model."""
    path = next(DATA.glob(f"{raw}_{TIMEFRAME.value}_*.csv"))
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
                    source=row.get("source") or "m33_screen",
                    is_closed=True,
                )
            )
    return tuple(bars)


def _factory() -> ExperimentEngineFactory:
    return ExperimentEngineFactory(
        registry=build_research_registry(), features_for=features_for, quote_asset="USDT"
    )


def at_cost(definition: ExperimentDefinition, multiplier: int) -> ExperimentDefinition:
    """Return the same definition with every modelled trading cost multiplied.

    Fee and slippage are fields of the execution policy the risk configuration carries, so the
    whole policy is rebuilt rather than patched: ``RiskConfiguration`` validates its own
    coherence on construction, and a partial merge would skip that.
    """
    policy = definition.risk.execution_policy
    scaled = policy.model_copy(
        update={
            "fee": policy.fee.model_copy(
                update={"basis_points": policy.fee.basis_points * multiplier}
            ),
            "slippage": policy.slippage.model_copy(
                update={"basis_points": policy.slippage.basis_points * multiplier}
            ),
        }
    )
    risk = definition.risk.model_copy(update={"execution_policy": scaled})
    backtest = definition.backtest.model_copy(
        update={
            "assumed_spread_basis_points": definition.backtest.assumed_spread_basis_points
            * multiplier
        }
    )
    return definition.model_copy(update={"risk": risk, "backtest": backtest})


def run(definition: ExperimentDefinition, bars: tuple[MarketBar, ...]) -> ExperimentResult | None:
    """Execute one experiment, returning the result or ``None`` if it failed."""
    result = ExperimentRunner().run(
        definition, bars=bars, factory=_factory(), code_revision="m33-screen"
    )
    return result if result.status.value == "succeeded" else None


def _single_year_share(result: ExperimentResult) -> Decimal | None:
    """Return the share of net profit the best calendar year accounts for."""
    if not result.equity_curve or result.performance is None:
        return None
    opening = result.performance.initial_equity
    deltas: list[Decimal] = []
    for year in sorted({point.at.year for point in result.equity_curve}):
        closing = [p.equity for p in result.equity_curve if p.at.year == year][-1]
        deltas.append(closing - opening)
        opening = closing
    total = sum(deltas, start=Decimal(0))
    return max(deltas) / total if total > 0 and deltas else None


def card(result: ExperimentResult | None) -> dict[str, Any] | None:
    """Return one run's scorecard plus the annualised figures the gate reads."""
    if result is None or result.performance is None:
        return None
    out: dict[str, Any] = Scorecard.from_performance(result.performance).model_dump()
    bars = getattr(result.performance, "bars_processed", 0) or len(result.equity_curve)
    out["bars"] = bars
    annual = cagr(Decimal(str(out["total_return"])), bars=bars, timeframe=TIMEFRAME)
    out["cagr"] = annual
    out["calmar"] = calmar(annual, Decimal(str(out["max_drawdown"])))
    out["exposure"] = out.get("time_in_market")
    out["single_year_share"] = _single_year_share(result)
    out["years"] = _per_year(result)
    return out


def _per_year(result: ExperimentResult) -> dict[str, Any]:
    """Return each calendar year's return, cut from the continuous run."""
    if result.performance is None:
        return {}
    opening = result.performance.initial_equity
    out: dict[str, Any] = {}
    for year in sorted({point.at.year for point in result.equity_curve}):
        closing = [p.equity for p in result.equity_curve if p.at.year == year][-1]
        out[str(year)] = (closing / opening - 1) if opening else None
        opening = closing
    return out


def cell(raw: str, variant: Variant, bars: tuple[MarketBar, ...]) -> dict[str, Any]:
    """Run every declared pass for one variant on one market, and judge it."""
    base_def = definition_for(raw, variant, latching=False)
    entry: dict[str, Any] = {
        "asset": raw,
        "key": variant.key,
        "rule": variant.rule.value,
        "threshold": str(variant.threshold),
        "params": dict(variant.params),
        "experiment_id": base_def.experiment_id,
    }
    entry["base"] = card(run(base_def, bars))
    for multiplier in COST_STRESS_MULTIPLIERS:
        entry[f"cost_x{multiplier}"] = card(run(at_cost(base_def, multiplier), bars))
    oos = tuple(bar for bar in bars if bar.open_time >= OOS_START)
    entry["oos"] = card(run(base_def, oos)) if len(oos) > MIN_OOS_BARS else None
    entry["deployed"] = card(run(definition_for(raw, variant, latching=True), bars))

    neighbours: dict[str, Any] = {}
    for short in NEIGHBOUR_SHORT_WINDOWS:
        neighbours[str(short)] = card(
            run(definition_for(raw, neighbour_of(variant, short), latching=False), bars)
        )
    entry["neighbours"] = neighbours
    factors = [
        Decimal(str(n["profit_factor"]))
        for n in neighbours.values()
        if n is not None and n.get("profit_factor") is not None
    ]
    entry["neighbour_min_profit_factor"] = min(factors) if factors else None

    base = entry["base"]
    if base is None:
        entry["passes"] = False
        entry["failed"] = ["no_result"]
        return entry
    measured = Compressed(
        annual=base["cagr"],
        max_drawdown=Decimal(str(base["max_drawdown"])),
        calmar_ratio=base["calmar"],
        single_year_share=base["single_year_share"],
        neighbour_min_profit_factor=entry["neighbour_min_profit_factor"],
        annual_at_double_cost=entry["cost_x2"]["cagr"] if entry["cost_x2"] else None,
        annual_at_triple_cost=entry["cost_x3"]["cagr"] if entry["cost_x3"] else None,
        out_of_sample_return=(Decimal(str(entry["oos"]["total_return"])) if entry["oos"] else None),
    )
    passed, reasons = survives(measured)
    entry["passes"] = passed
    entry["failed"] = [reason.value for reason in reasons]
    return entry


def main() -> int:
    """Run the declared screen and write one report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--keys", default="")
    parser.add_argument("--probe", action="store_true", help="time one cell and stop")
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    keys = {name for name in args.keys.split(",") if name}
    assets = [raw for raw in ASSETS_M33 if not wanted or raw in wanted]
    variants = [v for v in VARIANTS_M33 if not keys or v.key in keys]

    series = {raw: load(raw) for raw in assets}
    for raw, bars in series.items():
        sys.stdout.write(f"{raw:9} {len(bars):5d} {TIMEFRAME.value} bars\n")
    sys.stdout.flush()

    if args.probe:
        at = time.time()
        entry = cell(assets[0], variants[0], series[assets[0]])
        base = entry["base"]
        sys.stdout.write(
            f"probe: {assets[0]} {variants[0].key} in {time.time() - at:.1f}s, "
            f"trades {base['trades'] if base else 0}\n"
        )
        return 0

    HOME.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cells: list[dict[str, Any]] = []
    total = len(assets) * len(variants)
    done = 0
    for variant in variants:
        for raw in assets:
            at = time.time()
            entry = cell(raw, variant, series[raw])
            cells.append(entry)
            done += 1
            base = entry["base"]
            summary = (
                f"cagr {float(base['cagr'] or 0):+7.2%} dd {float(base['max_drawdown']):6.2%} "
                f"calmar {float(base['calmar'] or 0):5.2f} trades {base['trades']:3d}"
                if base
                else "no result"
            )
            sys.stdout.write(
                f"  [{done:2d}/{total}] {variant.key:3} {variant.rule.value:18} {raw:9} "
                f"{summary}  {'PASS' if entry['passes'] else ','.join(entry['failed'])}"
                f"  ({time.time() - at:.0f}s)\n"
            )
            sys.stdout.flush()

    families: dict[str, Any] = {}
    for variant in variants:
        mine = [c for c in cells if c["key"] == variant.key]
        passed = sum(1 for c in mine if c["passes"])
        families[variant.key] = {
            "rule": variant.rule.value,
            "threshold": str(variant.threshold),
            "markets_passed": passed,
            "markets_run": len(mine),
            "advances": advances(passed),
        }

    report = {
        "milestone": "m33",
        "phase": "screen",
        "engine": "production BacktestEngine with Risk V2",
        "timeframe": TIMEFRAME.value,
        "assets": assets,
        "thresholds": [str(t) for t in COMPRESSION_THRESHOLDS],
        "neighbour_short_windows": list(NEIGHBOUR_SHORT_WINDOWS),
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "families": families,
        "cells": cells,
    }
    narrowed = "-partial" if (keys or wanted) else ""
    out = HOME / f"screen_{TIMEFRAME.value}{narrowed}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n{len(cells)} cells in {report['seconds']}s -> {out.relative_to(ROOT)}\n")
    for key, family in families.items():
        sys.stdout.write(
            f"  {key} {family['rule']:18} passes {family['markets_passed']}/{family['markets_run']}"
            f"  {'ADVANCES' if family['advances'] else 'closed'}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
