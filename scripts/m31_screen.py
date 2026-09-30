"""M31 phase 1 at 1d: put every declared exposure policy behind CS2 and RF1, and measure.

Sixteen cells -- two frozen signals against eight policies, one of which is no policy at all --
and four passes each: base, cost x2, cost x3, and M23's out-of-sample window. Walk-forward and
sensitivity are deliberately **not** run here: they are not among the seven conditions declared
for M31, and the instruction is to reach for them only once something has passed.

The signals are never rebuilt. They come from :data:`~quantplatform.research.m31.FROZEN`, which
is M30's own rule objects by reference, so this script has no way to alter a lookback or a
threshold even by accident.

``card`` below repeats the arithmetic ``m30_screen.py`` uses rather than importing it, because
that script is what produced M30's committed evidence and is left untouched. The duplication is
not taken on trust: the uncontrolled cells here must reproduce M30's recorded numbers to the
digit, and :func:`_check_against_m30` fails the run if they do not.

Usage:
    uv run python scripts/m31_screen.py [--keys CS2,RF1]
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
from quantplatform.research.m31 import (
    ASSETS_M30,
    COST_STRESS_MULTIPLIERS,
    FROZEN,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    POLICIES,
    Controlled,
    ExposurePolicy,
    RotationRule,
    survives,
)
from quantplatform.research.rotation import (
    RotationRun,
    RotationSpec,
    Series,
    max_drawdown,
    profit_factor,
    simulate,
    yearly_returns,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m31"
DATA_FOR: Final[dict[Timeframe, Path]] = {
    Timeframe.D1: ROOT / "data/raw/m30/out",
    Timeframe.H4: ROOT / "data/raw/m16/out",
}
"""Where each timeframe's canonical series lives. 1d is the series ``m30_dataset.py`` derived
and verified; 4h is M16's own output, used unchanged and never re-derived."""
M30_SCREEN: Final[Path] = ROOT / "var/research/m30/screen_1d.json"
TOLERANCE: Final[Decimal] = Decimal("0.000001")


def load(raw: str, timeframe: Timeframe) -> tuple[MarketBar, ...]:
    """Read one market's canonical series, re-validating every row through the model."""
    path = next(DATA_FOR[timeframe].glob(f"{raw}_{timeframe.value}_*.csv"))
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
                    source=row.get("source") or "m31_screen",
                    is_closed=True,
                )
            )
    return tuple(bars)


def spec_of(rule: RotationRule, policy: ExposurePolicy) -> RotationSpec:
    """Return the simulator spec for a frozen rule with one exposure policy behind it."""
    return RotationSpec(
        lookback=rule.lookback,
        hold=rule.hold,
        normalised=rule.volatility_normalised,
        threshold=rule.entry_threshold,
        regime_filter=rule.regime_filter,
        vol_window=rule.vol_window,
        exposure=policy,
    )


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
    contributions = {c.asset: c.net_profit for c in run.contributions}
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


def cell(
    rule: RotationRule,
    label: str,
    policy: ExposurePolicy,
    series: Series,
    timeframe: Timeframe,
    *,
    lookback: int | None = None,
) -> dict[str, Any]:
    """Run the four declared passes for one signal under one exposure policy.

    ``lookback`` overrides the frozen signal's own only for a sensitivity probe, which is a
    measurement of fragility and never a candidate. The frozen rule is untouched either way.
    """
    spec = spec_of(rule, policy)
    if lookback is not None:
        spec = replace(spec, lookback=lookback)
    cost = ONE_WAY_COST_BASIS_POINTS
    entry: dict[str, Any] = {
        "key": f"{rule.key}-{label.replace(' ', '')}",
        "signal": rule.key,
        "label": label,
        "policy": {
            "fixed": policy.fixed,
            "volatility_window": policy.volatility_window,
            "drawdown_power": policy.drawdown_power,
        },
        "lookback": spec.lookback,
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


def _check_against_m30(cells: list[dict[str, Any]]) -> list[str]:
    """Return any disagreement between the uncontrolled cells and M30's recorded numbers."""
    if not M30_SCREEN.exists():
        return ["M30's screen is not on disk, so the uncontrolled cells could not be checked"]
    recorded = {
        entry["key"]: entry["base"]
        for entry in json.loads(M30_SCREEN.read_text(encoding="utf-8"))["cells"]
    }
    problems: list[str] = []
    for entry in cells:
        if entry["label"] != "uncontrolled":
            continue
        theirs = recorded.get(entry["signal"])
        if theirs is None:
            problems.append(f"{entry['signal']}: not present in M30's screen")
            continue
        for field in ("total_return", "max_drawdown", "turnover", "trades"):
            mine = Decimal(str(entry["base"][field]))
            other = Decimal(str(theirs[field]))
            if abs(mine - other) > TOLERANCE:
                problems.append(f"{entry['signal']}.{field}: {mine} != M30's {other}")
    return problems


def _probe(
    cells: list[dict[str, Any]], series: Series, timeframe: Timeframe
) -> list[dict[str, Any]]:
    """Return neighbour runs for every cell that passed, or missed by a single condition.

    Which cells get probed is decided mechanically -- cleared the gate, or failed exactly one
    of it -- rather than chosen after reading the table, so the selection carries no judgement.

    Two neighbourhoods are walked. The **exposure** neighbours ask whether the level that
    passed is a knife-edge. The **lookback** neighbours are M30's own declared sensitivity
    probes, half and double. Neither is a candidate: a neighbour that scores better is recorded
    and not adopted, because changing a parameter because a result pointed at it is what a
    pre-declaration exists to prevent.
    """
    interesting = [c for c in cells if c["passes"] or len(c["failed"]) == 1]
    out: list[dict[str, Any]] = []
    for c in interesting:
        rule = next(r for r in FROZEN if r.key == c["signal"])
        fixed = c["policy"]["fixed"]
        neighbours: list[tuple[str, ExposurePolicy, int | None]] = []
        if fixed is not None:
            level = Decimal(str(fixed))
            for step in (Decimal("-0.05"), Decimal("0.05")):
                nearby = level + step
                if Decimal(0) < nearby <= Decimal(1):
                    neighbours.append((f"fixed {nearby:.0%}", ExposurePolicy(fixed=nearby), None))
        policy = ExposurePolicy(
            fixed=None if fixed is None else Decimal(str(fixed)),
            volatility_window=c["policy"]["volatility_window"],
            drawdown_power=c["policy"]["drawdown_power"],
        )
        neighbours.extend(
            (f"lookback {look}", policy, look) for look in (rule.lookback // 2, rule.lookback * 2)
        )
        for label, nearby_policy, look in neighbours:
            probe = cell(rule, label, nearby_policy, series, timeframe, lookback=look)
            probe["probes"] = c["key"]
            out.append(probe)
            sys.stdout.write(
                f"    probe {c['key']:22} {label:14} "
                f"cagr {float(Decimal(str(probe['base']['cagr'] or 0))):+7.2%} "
                f"dd {float(Decimal(str(probe['base']['max_drawdown']))):6.2%} "
                f"calmar {float(Decimal(str(probe['base']['calmar'] or 0))):5.2f} "
                f"{'PASS' if probe['passes'] else ','.join(probe['failed'])}\n"
            )
            sys.stdout.flush()
    return out


def main() -> int:
    """Run the declared matrix and write one report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", default="", help="restrict to these signals, e.g. CS2")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument(
        "--sensitivity",
        action="store_true",
        help="probe the neighbours of whatever cleared the gate, or missed it by one condition",
    )
    args = parser.parse_args()
    keys = {name for name in args.keys.split(",") if name}
    rules = [rule for rule in FROZEN if not keys or rule.key in keys]
    timeframe = Timeframe(args.timeframe)

    series: Series = {raw: load(raw, timeframe) for raw in ASSETS_M30}
    sys.stdout.write(
        f"{len(series)} markets, {sum(len(b) for b in series.values())} {timeframe.value} bars, "
        f"{ONE_WAY_COST_BASIS_POINTS} bps a side\n\n"
    )
    sys.stdout.flush()

    HOME.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cells: list[dict[str, Any]] = []
    for rule in rules:
        for label, _, policy in POLICIES:
            at = time.time()
            entry = cell(rule, label, policy, series, timeframe)
            cells.append(entry)
            b = entry["base"]
            sys.stdout.write(
                f"  {rule.key:4} {label:16} cagr {float(b['cagr'] or 0):+7.2%} "
                f"dd {float(b['max_drawdown']):6.2%} calmar {float(b['calmar'] or 0):5.2f} "
                f"cash {float(b['time_in_cash'] or 0):5.1%} "
                f"{'PASS' if entry['passes'] else 'fail'}  ({time.time() - at:.1f}s)\n"
            )
            sys.stdout.flush()

    probes: list[dict[str, Any]] = []
    if args.sensitivity:
        probes = _probe(cells, series, timeframe)

    problems = _check_against_m30(cells) if timeframe is Timeframe.D1 else []
    report = {
        "milestone": "m31",
        "phase": "exposure_control",
        "timeframe": timeframe.value,
        "assets": list(ASSETS_M30),
        "frozen_signals": [rule.key for rule in FROZEN],
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "agrees_with_m30": not problems,
        "disagreements": problems,
        "cells": cells,
        "sensitivity": probes,
    }
    narrowed = "-partial" if keys else ""
    out = HOME / f"screen_{timeframe.value}{narrowed}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n{len(cells)} cells in {report['seconds']}s -> {out.relative_to(ROOT)}\n")
    if problems:
        sys.stdout.write("UNCONTROLLED CELLS DISAGREE WITH M30:\n")
        for line in problems:
            sys.stdout.write(f"  {line}\n")
        return 1
    if timeframe is Timeframe.D1:
        sys.stdout.write("uncontrolled cells reproduce M30 exactly\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
