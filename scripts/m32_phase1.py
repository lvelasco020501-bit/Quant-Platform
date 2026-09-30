"""M32 phase 1: re-measure the two candidates on a survivorship-corrected universe.

Three runs per candidate, so the effect of the correction is separable from the effect of
simply having more markets around:

    incumbents    M30's six, no universe cap -- reproduces M31 exactly, and is checked against
                  M31's recorded numbers so a disagreement fails the run rather than being
                  discovered later
    pool          all thirty markets rankable at once, which is what a naive expansion does
    corrected     the declared run: thirty markets available, and only the six most traded at
                  each bar rankable, by trailing median of volume x close over 72 bars

Every parameter of the signal is frozen and comes from M31 by reference. The only things that
change between the three runs are which markets exist and which of them are investable.

Usage:
    uv run python scripts/m32_phase1.py [--timeframe 1d] [--sensitivity]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m31 import policy_for
from quantplatform.research.m32 import (
    ASSETS_M30,
    CANDIDATES_M32,
    COST_STRESS_MULTIPLIERS,
    FIXED_EXPOSURE,
    LIQUIDITY_WINDOW,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    UNIVERSE_SIZE,
    Candidate,
    Controlled,
    pool_symbols,
    signal_of,
    survives,
)
from quantplatform.research.rotation import (
    ExposurePolicy,
    RotationRun,
    RotationSpec,
    Series,
    max_drawdown,
    profit_factor,
    simulate,
    yearly_returns,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m32"
M31_SCREEN: Final[Path] = ROOT / "var/research/m31"
INCUMBENT_DIR: Final[dict[Timeframe, Path]] = {
    Timeframe.D1: ROOT / "data/raw/m30/out",
    Timeframe.H4: ROOT / "data/raw/m16/out",
}
POOL_DIR: Final[Path] = ROOT / "data/raw/m32/out"
TOLERANCE: Final[Decimal] = Decimal("0.000001")


def _directory(raw: str, timeframe: Timeframe) -> Path:
    """Return where one market's canonical series lives for this timeframe.

    The incumbents keep the series M16 validated and M30 derived. Re-acquiring them here would
    create a second lineage for exactly the assets whose numbers this milestone must reproduce.
    """
    return INCUMBENT_DIR[timeframe] if raw in ASSETS_M30 else POOL_DIR


def load(raw: str, timeframe: Timeframe) -> tuple[MarketBar, ...]:
    """Read one market's canonical series, re-validating every row through the model."""
    matches = sorted(_directory(raw, timeframe).glob(f"{raw}_{timeframe.value}_*.csv"))
    if len(matches) != 1:
        msg = f"expected exactly one {timeframe.value} series for {raw}, found {len(matches)}"
        raise RuntimeError(msg)
    bars: list[MarketBar] = []
    with matches[0].open(encoding="utf-8") as handle:
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
                    source=row.get("source") or "m32_phase1",
                    is_closed=True,
                )
            )
    return tuple(bars)


def _single_year_share(run: RotationRun) -> Decimal | None:
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


def card(run: RotationRun, timeframe: Timeframe) -> dict[str, Any]:
    """Return every figure one run offers, with no judgement applied to any of it."""
    total = run.total_return
    drawdown = max_drawdown(run.equity_curve)
    annual = cagr(total, bars=run.bars, timeframe=timeframe)
    years = yearly_returns(run.equity_curve, run.initial_equity)
    contributions = {c.asset: c.net_profit for c in run.contributions if c.episodes}
    net_total = sum(contributions.values(), start=Decimal(0))
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
        "markets_touched": len(contributions),
        "assets_positive": len([v for v in contributions.values() if v > 0]),
        "top_asset_share": (max(contributions.values()) / net_total) if net_total > 0 else None,
        "contributions": dict(contributions),
        "years": years,
        "years_positive_share": (
            Decimal(sum(1 for v in years.values() if v > 0)) / Decimal(len(years))
            if years
            else None
        ),
        "single_year_share": _single_year_share(run),
    }


def _spec(candidate: Candidate, *, capped: bool, lookback: int | None = None) -> RotationSpec:
    """Return the simulator spec for a frozen candidate, optionally with the universe cap."""
    rule = signal_of(candidate)
    spec = RotationSpec(
        lookback=rule.lookback if lookback is None else lookback,
        hold=rule.hold,
        normalised=rule.volatility_normalised,
        threshold=rule.entry_threshold,
        regime_filter=rule.regime_filter,
        vol_window=rule.vol_window,
        exposure=policy_for(FIXED_EXPOSURE),
    )
    if capped:
        spec = replace(spec, universe_size=UNIVERSE_SIZE, liquidity_window=LIQUIDITY_WINDOW)
    return spec


def cell(
    candidate: Candidate,
    label: str,
    series: Series,
    *,
    capped: bool,
    lookback: int | None = None,
    exposure: Decimal | None = None,
) -> dict[str, Any]:
    """Run the four declared passes for one candidate over one universe."""
    spec = _spec(candidate, capped=capped, lookback=lookback)
    if exposure is not None:
        spec = replace(spec, exposure=ExposurePolicy(fixed=exposure))
    timeframe = candidate.timeframe
    cost = ONE_WAY_COST_BASIS_POINTS
    entry: dict[str, Any] = {
        "candidate": candidate.key,
        "universe": label,
        "markets_available": len(series),
        "universe_cap": UNIVERSE_SIZE if capped else None,
        "lookback": spec.lookback,
        "exposure": str(spec.exposure.fixed),
        "base": card(simulate(series, spec, cost_basis_points=cost), timeframe),
    }
    for multiplier in COST_STRESS_MULTIPLIERS:
        entry[f"cost_x{multiplier}"] = card(
            simulate(series, spec, cost_basis_points=cost * multiplier), timeframe
        )
    entry["oos"] = card(simulate(series, spec, cost_basis_points=cost, start=OOS_START), timeframe)
    measured = Controlled(
        max_drawdown=entry["base"]["max_drawdown"],
        calmar_ratio=entry["base"]["calmar"],
        single_year_share=entry["base"]["single_year_share"],
        top_asset_share=entry["base"]["top_asset_share"],
        annual_at_double_cost=entry["cost_x2"]["cagr"],
        annual_at_triple_cost=entry["cost_x3"]["cagr"],
        out_of_sample_return=entry["oos"]["total_return"],
    )
    passed, reasons = survives(measured)
    entry["passes"] = passed
    entry["failed"] = [reason.value for reason in reasons]
    return entry


def _check_m31(entry: dict[str, Any], candidate: Candidate) -> list[str]:
    """Return any disagreement between the incumbents run and M31's recorded numbers."""
    path = M31_SCREEN / f"screen_{candidate.timeframe.value}.json"
    if not path.exists():
        return [f"M31's {candidate.timeframe.value} screen is not on disk"]
    key = f"{candidate.signal}-fixed25%"
    cells = json.loads(path.read_text(encoding="utf-8"))["cells"]
    theirs = next((c["base"] for c in cells if c["key"] == key), None)
    if theirs is None:
        return [f"{key}: not present in M31's screen"]
    problems: list[str] = []
    for field in ("total_return", "max_drawdown", "turnover", "trades"):
        mine = Decimal(str(entry["base"][field]))
        other = Decimal(str(theirs[field]))
        if abs(mine - other) > TOLERANCE:
            problems.append(f"{key}.{field}: {mine} != M31's {other}")
    return problems


def _line(entry: dict[str, Any]) -> str:
    """Return one result row for the console."""
    b = entry["base"]
    return (
        f"  {entry['candidate']:18} {entry['universe']:22} "
        f"cagr {float(b['cagr'] or 0):+7.2%} dd {float(b['max_drawdown']):6.2%} "
        f"calmar {float(b['calmar'] or 0):5.2f} "
        f"held {b['markets_touched']:2d} top {float(b['top_asset_share'] or 0):5.2f} "
        f"{'PASS' if entry['passes'] else ','.join(entry['failed'])}"
    )


def main() -> int:
    """Run phase 1 for whichever candidate matches the requested timeframe."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--sensitivity", action="store_true")
    args = parser.parse_args()
    timeframe = Timeframe(args.timeframe)
    candidates = [c for c in CANDIDATES_M32 if c.timeframe is timeframe]
    if not candidates:
        sys.stdout.write(f"no candidate declared at {timeframe.value}\n")
        return 1

    pool: Series = {raw: load(raw, timeframe) for raw in pool_symbols()}
    incumbents: Series = {raw: pool[raw] for raw in ASSETS_M30}
    sys.stdout.write(
        f"{len(pool)} markets, {sum(len(b) for b in pool.values())} {timeframe.value} bars; "
        f"incumbents {len(incumbents)}\n\n"
    )
    sys.stdout.flush()

    HOME.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cells: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    problems: list[str] = []

    for candidate in candidates:
        for label, series, capped in (
            ("incumbents", incumbents, False),
            ("pool uncapped", pool, False),
            ("corrected top-6", pool, True),
        ):
            at = time.time()
            entry = cell(candidate, label, series, capped=capped)
            cells.append(entry)
            sys.stdout.write(f"{_line(entry)}  ({time.time() - at:.0f}s)\n")
            sys.stdout.flush()
            if label == "incumbents":
                problems += _check_m31(entry, candidate)

        if args.sensitivity:
            corrected = next(
                c for c in cells if c["candidate"] == candidate.key and c["universe_cap"]
            )
            rule = signal_of(candidate)
            for look in (rule.lookback // 2, rule.lookback * 2):
                probe = cell(candidate, f"lookback {look}", pool, capped=True, lookback=look)
                probe["probes"] = corrected["candidate"]
                probes.append(probe)
                sys.stdout.write(f"{_line(probe)}\n")
            for level in (Decimal("0.20"), Decimal("0.30")):
                probe = cell(candidate, f"fixed {level:.0%}", pool, capped=True, exposure=level)
                probe["probes"] = corrected["candidate"]
                probes.append(probe)
                sys.stdout.write(f"{_line(probe)}\n")
            sys.stdout.flush()

    report = {
        "milestone": "m32",
        "phase": "survivorship",
        "timeframe": timeframe.value,
        "pool": list(pool_symbols()),
        "universe_cap": UNIVERSE_SIZE,
        "liquidity_window": LIQUIDITY_WINDOW,
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "reproduces_m31": not problems,
        "disagreements": problems,
        "cells": cells,
        "sensitivity": probes,
    }
    out = HOME / f"phase1_{timeframe.value}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n{len(cells)} cells in {report['seconds']}s -> {out.relative_to(ROOT)}\n")
    if problems:
        sys.stdout.write("INCUMBENTS RUN DISAGREES WITH M31:\n")
        for line in problems:
            sys.stdout.write(f"  {line}\n")
        return 1
    sys.stdout.write("incumbents run reproduces M31 exactly\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
