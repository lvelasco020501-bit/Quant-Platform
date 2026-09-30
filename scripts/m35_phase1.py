"""M35 phase 1: walk the corrected breadth probe over M34's combined portfolio.

Aggregate deployed capital is held to the declared portfolio's own bar-by-bar path, so breadth is
the only thing that changes between the three runs. The per-asset cap plays no part: the
normalisation replaces every weight with an equal share of the reference exposure, so whatever
the cap did beforehand is overwritten -- which is exactly what makes the probe cap-free by
construction rather than by another declared number.

Usage:
    uv run python scripts/m35_phase1.py
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

# The mask cache and the loader live in the script that built the cache; importing them keeps
# one definition of where the series come from. sys.path is extended because scripts/ is not a
# package and this file may be run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import CACHE, load

from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m35 import (
    BREADTHS,
    COST_STRESS_MULTIPLIERS,
    LIQUIDITY_WINDOW,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    REFERENCE_BREADTH,
    SLEEVES,
    TIMEFRAME,
    Phase1,
    phase_one_passes,
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

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m35"


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
        "turnover": run.turnover,
        "fees": run.cost_paid,
        "final_equity": run.final_equity,
        "top_asset_share": _share(run.by_asset),
        "top_sleeve_share": _share(run.by_sleeve) if len(run.by_sleeve) > 1 else None,
        "by_sleeve": {c.name: c.net_profit for c in run.by_sleeve},
        "assets_touched": sum(1 for c in run.by_asset if c.episodes),
        "years": yearly_returns(run.equity_curve, run.initial_equity),
        "single_year_share": _single_year_share(run),
    }


def main() -> int:
    """Run the corrected probe at every declared breadth and judge phase 1."""
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    masks: dict[Holding, tuple[bool, ...]] = {
        (key.split("|")[0], key.split("|")[1]): tuple(c == "1" for c in packed)
        for key, packed in cache["masks"].items()
    }
    symbols = list(cache["markets"])
    series = {raw: load(raw) for raw in symbols}
    grid = align(series)
    slots = positions(series, grid)
    keys = [probe.key for probe in SLEEVES]
    mine = {h: m for h, m in masks.items() if h[0] in keys}
    sys.stdout.write(f"{len(series)} markets, grid {len(grid)}, sleeves {keys}\n\n")

    universes = {
        breadth: eligible_universe(series, slots, window=LIQUIDITY_WINDOW, size=breadth)
        for breadth in BREADTHS
    }
    reference_targets = targets_for(
        mine,
        universes[REFERENCE_BREADTH],
        Allocation(universe_size=REFERENCE_BREADTH, sleeves=len(keys)),
    )
    reference = deployed(reference_targets)
    sys.stdout.write(
        f"reference exposure path from breadth {REFERENCE_BREADTH}: "
        f"mean {float(sum(reference) / len(reference)):.2%}\n\n"
    )

    cost = ONE_WAY_COST_BASIS_POINTS
    started = time.time()
    entries: list[Phase1] = []
    rows: dict[str, Any] = {}
    for breadth in BREADTHS:
        raw_targets = targets_for(
            mine, universes[breadth], Allocation(universe_size=breadth, sleeves=len(keys))
        )
        targets = normalise_to(raw_targets, reference)
        forced = sum(1 for slot, want in enumerate(reference) if want > 0 and not targets[slot])
        run = simulate_portfolio(series, targets, cost_basis_points=cost, holdings=list(mine))
        base = card(run)
        entry: dict[str, Any] = {"breadth": breadth, "base": base, "bars_forced_to_cash": forced}
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
        sums = deployed(targets)
        invested = [s for s in sums if s > 0]
        counts = [len(t) for t in targets if t]
        entry["mean_deployed"] = sum(sums, start=Decimal(0)) / Decimal(len(sums))
        entry["deployed_when_invested"] = (
            sum(invested, start=Decimal(0)) / Decimal(len(invested)) if invested else Decimal(0)
        )
        entry["positions_when_invested"] = (
            Decimal(sum(counts)) / Decimal(len(counts)) if counts else None
        )
        record = Phase1(
            breadth=breadth,
            annual=base["cagr"],
            max_drawdown=base["max_drawdown"],
            calmar_ratio=base["calmar"],
            mean_deployed=entry["mean_deployed"],
            positions_when_invested=entry["positions_when_invested"],
            bars_forced_to_cash=forced,
        )
        entries.append(record)
        entry["stable"] = record.stable
        rows[str(breadth)] = entry
        sys.stdout.write(
            f"  breadth {breadth:>2}  cagr {float(base['cagr'] or 0):+7.2%} "
            f"dd {float(base['max_drawdown']):6.2%} calmar {float(base['calmar'] or 0):5.2f}  "
            f"deployed {float(entry['mean_deployed']):5.1%} "
            f"(invested {float(entry['deployed_when_invested']):5.1%}) "
            f"positions {float(entry['positions_when_invested'] or 0):4.1f}  "
            f"forced-cash {forced:5d}  {'stable' if record.stable else 'UNSTABLE'}\n"
        )
        sys.stdout.flush()

    passes = phase_one_passes(tuple(entries))
    report = {
        "milestone": "m35",
        "phase": "corrected_breadth_probe",
        "timeframe": TIMEFRAME.value,
        "breadths": list(BREADTHS),
        "reference_breadth": REFERENCE_BREADTH,
        "reference_mean_deployed": sum(reference) / len(reference),
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "breadth_results": rows,
        "phase_one_passes": passes,
    }
    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / "phase1_breadth_4h.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n-> {out.relative_to(ROOT)}\n")
    sys.stdout.write(
        "PHASE 1 PASSES: the advantage does not need exactly six markets\n"
        if passes
        else "PHASE 1 FAILS: at constant exposure, some breadth breaks the candidate\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
