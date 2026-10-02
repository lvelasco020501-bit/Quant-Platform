"""M36 phase 2: rebuild the combined portfolio on positions Risk V2 actually decided.

Reads the intervals ``m36_positions.py`` extracted from the certified engine -- where stops,
position state, re-entries, order rejection and the non-latching breakers are all live -- and
runs the declared allocation over them. Then it puts that portfolio beside the one M34 and M35
built from untouched signal timelines and names every difference.

The allocation, the universe, the costs, the stress multipliers and the out-of-sample window are
all the same objects the earlier milestones used. The only thing that changes between the two
portfolios is where the positions came from, which is what makes the comparison mean something.

Usage:
    uv run python scripts/m36_phase2.py
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m36 import (
    BREADTHS,
    COST_STRESS_MULTIPLIERS,
    LIQUIDITY_WINDOW,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    SLEEVES,
    TIMEFRAME,
    UNIVERSE_SIZE,
    Basis,
    BreadthPoint,
    Divergence,
    Final36,
    phase_one_passes,
    pool_symbols,
    survives,
)
from quantplatform.research.portfolio import (
    Allocation,
    Holding,
    PortfolioRun,
    SleeveContribution,
    deployed,
    normalise_to,
    positions,
    simulate_portfolio,
    targets_for,
)
from quantplatform.research.rotation import align, eligible_universe, max_drawdown, yearly_returns
from quantplatform.research.sleeve import Interval, long_mask

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import CACHE as MASK_CACHE
from m35_masks import load
from m36_positions import CACHE as POSITION_CACHE

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m36"


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
    drawdown = max_drawdown(run.equity_curve)
    annual = cagr(run.total_return, bars=run.bars, timeframe=TIMEFRAME)
    return {
        "total_return": run.total_return,
        "cagr": annual,
        "max_drawdown": drawdown,
        "calmar": calmar(annual, drawdown),
        "profit_factor": _profit_factor(run.episode_returns),
        "episodes": len(run.episode_returns),
        "bars": run.bars,
        "bars_held": run.bars_held,
        "exposure": Decimal(run.bars_held) / Decimal(run.bars) if run.bars else None,
        "turnover": run.turnover,
        "fees": run.cost_paid,
        "final_equity": run.final_equity,
        "top_asset_share": _share(run.by_asset),
        "top_sleeve_share": _share(run.by_sleeve) if len(run.by_sleeve) > 1 else None,
        "by_sleeve": {c.name: c.net_profit for c in run.by_sleeve},
        "by_asset": {c.name: c.net_profit for c in run.by_asset if c.episodes},
        "assets_touched": sum(1 for c in run.by_asset if c.episodes),
        "years": yearly_returns(run.equity_curve, run.initial_equity),
        "single_year_share": _single_year_share(run),
    }


def _masks_from_signals() -> dict[Holding, tuple[bool, ...]]:
    """Return the untouched signal timelines M34 and M35 ran on."""
    cache = json.loads(MASK_CACHE.read_text(encoding="utf-8"))
    return {
        (key.split("|")[0], key.split("|")[1]): tuple(c == "1" for c in packed)
        for key, packed in cache["masks"].items()
    }


def _masks_from_engine(
    grid: Sequence[datetime],
) -> tuple[dict[Holding, tuple[bool, ...]], dict[str, str]]:
    """Return the timelines the engine's positions occupied, and the pairs that never ran.

    An engine trade's ``opened_at`` and ``closed_at`` bracket a real position, so the mask is
    built from those directly. Where a stop cut a signal stretch into several trades, the mask
    shows several separate holdings -- which is the difference this phase exists to measure.

    A pair that failed to run is returned separately rather than as a mask. Its mask would be
    all-false, which is exactly what a market the sleeve never wanted also looks like, so
    folding the two together would let a crashed driver enter the portfolio as a quiet decision
    not to trade. The failure is carried out to the report instead.
    """
    cache = json.loads(POSITION_CACHE.read_text(encoding="utf-8"))
    out: dict[Holding, tuple[bool, ...]] = {}
    failures: dict[str, str] = {}
    for key, row in cache["pairs"].items():
        sleeve, market = key.split("|")
        if row["status"] != "succeeded":
            failures[key] = row.get("error") or row["status"]
            continue
        intervals = tuple(
            Interval(
                entered_at=datetime.fromisoformat(span["opened_at"]),
                exited_at=datetime.fromisoformat(span["closed_at"]),
            )
            for span in row["intervals"]
        )
        out[sleeve, market] = long_mask(intervals, grid)
    return out, failures


def _stretches(mask: Sequence[bool]) -> int:
    """Return how many separate held stretches a timeline contains."""
    return sum(1 for i, held in enumerate(mask) if held and (i == 0 or not mask[i - 1]))


def _pair_differences(
    signal_masks: dict[Holding, tuple[bool, ...]],
    engine_masks: dict[Holding, tuple[bool, ...]],
) -> list[dict[str, Any]]:
    """Compare each pair's signal timeline against the one Risk V2 actually produced.

    M34 established that the certified engine turns over 1.5 to 10.5 times as often as the
    signals alone, and that because position state is an input to these rules a stop can produce
    an entry the unstopped rule never takes. This measures both effects across every pair rather
    than on the one sample M34 looked at: ``engine_only`` bars are time the engine held while the
    signal did not, which is the direction that cannot be explained by a stop cutting a stretch
    short.
    """
    out: list[dict[str, Any]] = []
    for holding in sorted(set(signal_masks) & set(engine_masks)):
        signal, engine = signal_masks[holding], engine_masks[holding]
        both = sum(1 for s, e in zip(signal, engine, strict=True) if s and e)
        out.append(
            {
                "sleeve": holding[0],
                "market": holding[1],
                "signal_bars": sum(signal),
                "engine_bars": sum(engine),
                "overlap_bars": both,
                "engine_only_bars": sum(
                    1 for s, e in zip(signal, engine, strict=True) if e and not s
                ),
                "signal_only_bars": sum(
                    1 for s, e in zip(signal, engine, strict=True) if s and not e
                ),
                "signal_stretches": _stretches(signal),
                "engine_stretches": _stretches(engine),
            }
        )
    return out


def _passes_at(
    breadth: int,
    series: dict[str, Any],
    masks: dict[Holding, tuple[bool, ...]],
    universes: dict[int, tuple[frozenset[str], ...]],
    reference: Sequence[Decimal],
    cost: Decimal,
) -> tuple[BreadthPoint, dict[str, Any]]:
    """Run one breadth at the reference exposure and judge it against phase 1's conditions."""
    keys = [p.key for p in SLEEVES]
    raw = targets_for(
        masks, universes[breadth], Allocation(universe_size=breadth, sleeves=len(keys))
    )
    targets = normalise_to(raw, reference)
    base = card(simulate_portfolio(series, targets, cost_basis_points=cost, holdings=list(masks)))
    stress = {
        f"cost_x{m}": card(
            simulate_portfolio(series, targets, cost_basis_points=cost * m, holdings=list(masks))
        )
        for m in COST_STRESS_MULTIPLIERS
    }
    oos = card(
        simulate_portfolio(
            series, targets, cost_basis_points=cost, start=OOS_START, holdings=list(masks)
        )
    )
    point = BreadthPoint(
        breadth=breadth,
        annual=base["cagr"],
        max_drawdown=base["max_drawdown"],
        calmar_ratio=base["calmar"],
        annual_at_double_cost=stress["cost_x2"]["cagr"],
        annual_at_triple_cost=stress["cost_x3"]["cagr"],
        out_of_sample_return=oos["total_return"],
        single_year_share=base["single_year_share"],
        top_asset_share=base["top_asset_share"],
        top_sleeve_share=base["top_sleeve_share"],
    )
    return point, {"breadth": breadth, "base": base, **stress, "oos": oos, "passes": point.passes}


def _run_basis(
    series: dict[str, Any],
    masks: dict[Holding, tuple[bool, ...]],
    universes: dict[int, tuple[frozenset[str], ...]],
    reference: Sequence[Decimal],
    cost: Decimal,
) -> tuple[list[BreadthPoint], dict[str, Any]]:
    """Run every declared breadth on one basis, printing each as it lands.

    Phase 1 and phase 2 differ only in where the masks came from, so they share this. Running
    them through the same code is part of the claim: any difference in the numbers below is a
    difference in the position timelines, not in how the two were measured.
    """
    points: list[BreadthPoint] = []
    rows: dict[str, Any] = {}
    for breadth in BREADTHS:
        point, row = _passes_at(breadth, series, masks, universes, reference, cost)
        points.append(point)
        rows[str(breadth)] = row
        sys.stdout.write(
            f"  breadth {breadth:>2}  cagr {float(point.annual or 0):+7.2%} "
            f"dd {float(point.max_drawdown):6.2%} calmar {float(point.calmar_ratio or 0):5.2f}  "
            f"{'PASS' if point.passes else 'FAIL'}\n"
        )
        sys.stdout.flush()
    return points, rows


def _summarise_differences(diffs: Sequence[dict[str, Any]]) -> None:
    """Print how far the engine's own position timelines sit from the signal timelines."""
    if not diffs:
        return
    ratios = sorted(
        Decimal(d["engine_stretches"]) / Decimal(d["signal_stretches"])
        for d in diffs
        if d["signal_stretches"]
    )
    held = sorted(
        Decimal(d["engine_bars"]) / Decimal(d["signal_bars"]) for d in diffs if d["signal_bars"]
    )
    sys.stdout.write(
        f"  signal vs Risk V2 across {len(diffs)} pairs: "
        f"stretch ratio {float(ratios[0]):.1f}x-{float(ratios[-1]):.1f}x "
        f"(median {float(ratios[len(ratios) // 2]):.1f}x), "
        f"bars-held ratio median {float(held[len(held) // 2]):.2f}x, "
        f"{sum(1 for d in diffs if d['engine_only_bars'])} pairs held where no signal was\n"
    )


def _divergences(signal_base: dict[str, Any], engine_base: dict[str, Any]) -> list[Divergence]:
    """Return the measure-by-measure gap between the two bases at the declared breadth."""
    return [
        Divergence(
            measure=name,
            on_signals=Decimal(str(signal_base[name])) if signal_base[name] is not None else None,
            on_certified=(
                Decimal(str(engine_base[name])) if engine_base[name] is not None else None
            ),
        )
        for name in (
            "cagr",
            "max_drawdown",
            "calmar",
            "profit_factor",
            "episodes",
            "turnover",
            "fees",
            "exposure",
            "top_asset_share",
            "top_sleeve_share",
            "single_year_share",
        )
    ]


def main() -> int:
    """Judge phase 1, then rebuild the portfolio on Risk V2's own positions and compare."""
    symbols = list(pool_symbols())
    series = {raw: load(raw) for raw in symbols}
    grid = align(series)
    slots = positions(series, grid)
    keys = [p.key for p in SLEEVES]
    cost = ONE_WAY_COST_BASIS_POINTS
    universes = {
        breadth: eligible_universe(series, slots, window=LIQUIDITY_WINDOW, size=breadth)
        for breadth in BREADTHS
    }

    signal_masks = {h: m for h, m in _masks_from_signals().items() if h[0] in keys}
    reference = deployed(
        targets_for(
            signal_masks,
            universes[UNIVERSE_SIZE],
            Allocation(universe_size=UNIVERSE_SIZE, sleeves=len(keys)),
        )
    )

    sys.stdout.write("PHASE 1 -- operational breadths, signal basis (M35's numbers re-judged)\n")
    points, rows = _run_basis(series, signal_masks, universes, reference, cost)
    phase1 = phase_one_passes(tuple(points))
    sys.stdout.write(f"  phase 1: {'PASSES' if phase1 else 'FAILS'}\n\n")
    if not phase1:
        sys.stdout.write("phase 2 not run: phase 1 did not pass\n")
        return 0

    sys.stdout.write("PHASE 2 -- certified engine positions, Risk V2 deciding\n")
    built, failures = _masks_from_engine(grid)
    engine_masks = {h: m for h, m in built.items() if h[0] in keys}
    missing = sorted(set(signal_masks) - set(engine_masks))
    sys.stdout.write(
        f"  {len(engine_masks)} pairs from the engine; "
        f"{len(missing)} signal pairs absent (never eligible); "
        f"{len(failures)} pairs failed to run\n"
    )
    for key, reason in sorted(failures.items()):
        sys.stdout.write(f"    FAILED {key}: {reason}\n")

    started = time.time()
    certified_points, certified = _run_basis(series, engine_masks, universes, reference, cost)

    diffs = _pair_differences(signal_masks, engine_masks)
    _summarise_differences(diffs)

    declared = str(UNIVERSE_SIZE)
    signal_base = rows[declared]["base"]
    engine_base = certified[declared]["base"]
    divergences = _divergences(signal_base, engine_base)

    measured = Final36(
        basis=Basis.CERTIFIED,
        breadths_passed=phase_one_passes(tuple(certified_points)),
        annual=engine_base["cagr"],
        max_drawdown=engine_base["max_drawdown"],
        calmar_ratio=engine_base["calmar"],
        single_year_share=engine_base["single_year_share"],
        top_asset_share=engine_base["top_asset_share"],
        top_sleeve_share=engine_base["top_sleeve_share"],
        annual_at_double_cost=certified[declared]["cost_x2"]["cagr"],
        annual_at_triple_cost=certified[declared]["cost_x3"]["cagr"],
        out_of_sample_return=certified[declared]["oos"]["total_return"],
        signal_basis_passed=phase1,
    )
    passed, reasons = survives(measured)

    report = {
        "milestone": "m36",
        "timeframe": TIMEFRAME.value,
        "breadths": list(BREADTHS),
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "phase1_signal_basis": rows,
        "phase1_passes": phase1,
        "phase2_certified_basis": certified,
        "divergences": [
            {
                "measure": d.measure,
                "on_signals": d.on_signals,
                "on_certified": d.on_certified,
                "relative": d.relative,
            }
            for d in divergences
        ],
        "final_passes": passed,
        "final_failed": [r.value for r in reasons],
        "pairs_expected": len(signal_masks),
        "pairs_on_engine": len(engine_masks),
        "errors_by_pair": failures,
        "pair_differences": diffs,
        "coverage_complete": not failures,
    }
    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / "phase2_risk_v2_4h.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n-> {out.relative_to(ROOT)}\n")
    sys.stdout.write(
        "GO TO PAPER DESIGN: the edge survives Risk V2\n"
        if passed
        else f"NO-GO: {', '.join(r.value for r in reasons)}\n"
    )
    if failures:
        sys.stdout.write(
            f"VERDICT IS CONDITIONAL: {len(failures)} of {len(signal_masks)} pairs never ran, so "
            "their positions are absent from the portfolio above rather than measured at zero.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
