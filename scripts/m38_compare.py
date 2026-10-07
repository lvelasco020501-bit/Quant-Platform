"""Compare Risk V2, the frozen Risk V3 candidate, and the signal basis on the full universe.

Three bases, one measurement path. The allocator, the universe, the costs, the stress
multipliers and the out-of-sample window are the same objects every milestone since M34 has
used, and all three bases run through the same functions -- so a difference between two rows is
a difference in what decided the positions, not in how they were measured.

Risk V2 is read from M36's extraction and the signal basis from M35's cache; only Risk V3 was
extracted by M38. Exposure is deliberately not normalised, for M37's reason: the allocator is
identical across bases and a risk layer that takes the account out of the market earlier is
supposed to show up as less time in the market.

Usage:
    uv run python scripts/m38_compare.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import Timeframe
from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m37_alternatives import ALTERNATIVES
from quantplatform.research.m37_definitions import alternative_risk, baseline_risk
from quantplatform.research.m37_probe import longest_flat_while_wanted
from quantplatform.research.m37_probe import re_entries as count_re_entries
from quantplatform.research.m38 import (
    BREADTHS,
    CANDIDATE_KEY,
    COST_STRESS_MULTIPLIERS,
    LIQUIDITY_WINDOW,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    SAFETY_INVARIANTS,
    SLEEVES,
    TIMEFRAME,
    Basis,
    BreadthResult,
    invariants_for,
    passes,
    pool_symbols,
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
from quantplatform.research.rotation import align, eligible_universe, max_drawdown, yearly_returns
from quantplatform.research.sleeve import Interval, long_mask

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import CACHE as MASK_CACHE
from m35_masks import load
from m38_extract import CACHE as V3_CACHE
from m38_extract import V2_CACHE

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m38"


@dataclass(frozen=True)
class Panel:
    """The fixed measurement context every basis is read against, as one frozen record."""

    grid: Sequence[datetime]
    series: dict[str, Any]
    universes: dict[int, tuple[frozenset[str], ...]]
    signal_masks: dict[Holding, tuple[bool, ...]]
    sleeves: list[str]


def _signal_masks(keys: list[str], grid: Sequence[datetime]) -> dict[Holding, tuple[bool, ...]]:
    """Return the cached signal timelines, projected onto this grid *by timestamp*.

    By timestamp and not by position, for the reason M37 found the hard way: a mask cached on a
    different market set's grid misaligns every slot after the first instant the two grids do
    not share. Here the grids do coincide -- both are the thirty-market pool -- and the
    projection makes that a checked fact rather than a hope.
    """
    cache = json.loads(MASK_CACHE.read_text(encoding="utf-8"))
    stamps: list[str] = cache["grid"]
    wanted = [stamp.isoformat() for stamp in grid]
    out: dict[Holding, tuple[bool, ...]] = {}
    for packed_key, packed in cache["masks"].items():
        sleeve, market = packed_key.split("|")
        if sleeve not in keys:
            continue
        held = dict(zip(stamps, packed, strict=True))
        out[sleeve, market] = tuple(held[stamp] == "1" for stamp in wanted)
    return out


def _engine_masks(
    path: Path, grid: Sequence[datetime], keys: list[str]
) -> tuple[dict[Holding, tuple[bool, ...]], dict[str, str], Counter[str], Counter[str], int]:
    """Return one extraction's timelines, its failures, and what ended its positions."""
    cache = json.loads(path.read_text(encoding="utf-8"))
    masks: dict[Holding, tuple[bool, ...]] = {}
    failures: dict[str, str] = {}
    codes: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    forced = 0
    for key in sorted(cache["pairs"]):
        row = cache["pairs"][key]
        if row["sleeve"] not in keys:
            continue
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
        masks[row["sleeve"], row["market"]] = long_mask(intervals, grid)
        codes.update(row.get("exits_by_code", {}))
        kinds.update(row.get("exits_by_stop_kind", {}))
        forced += row.get("forced_exits", 0)
    return masks, failures, codes, kinds, forced


def _share(contributions: Sequence[SleeveContribution]) -> Decimal | None:
    """Return the share of net profit the best contributor accounts for."""
    values = [c.net_profit for c in contributions]
    total = sum(values, start=Decimal(0))
    return max(values) / total if total > 0 and values else None


def _profit_factor(returns: tuple[Decimal, ...]) -> Decimal | None:
    """Return gross profit over gross loss across closed episodes."""
    won = sum((r for r in returns if r > 0), start=Decimal(0))
    lost = sum((-r for r in returns if r < 0), start=Decimal(0))
    return None if lost == 0 else won / lost


def _yearly(run: PortfolioRun) -> tuple[Decimal | None, Decimal | None]:
    """Return the best year's share of net profit and the share of years that closed up."""
    if not run.equity_curve:
        return None, None
    opening = run.initial_equity
    deltas: list[Decimal] = []
    for year in sorted({point.at.year for point in run.equity_curve}):
        closing = [p.equity for p in run.equity_curve if p.at.year == year][-1]
        deltas.append(closing - opening)
        opening = closing
    total = sum(deltas, start=Decimal(0))
    best = max(deltas) / total if total > 0 and deltas else None
    returns = yearly_returns(run.equity_curve, run.initial_equity)
    up = (
        Decimal(sum(1 for value in returns.values() if value > 0)) / Decimal(len(returns))
        if returns
        else None
    )
    return best, up


def _measure(masks: dict[Holding, tuple[bool, ...]], panel: Panel, breadth: int) -> dict[str, Any]:
    """Run one basis at one breadth: base cost, both stress multipliers, and out of sample."""
    allocation = Allocation(universe_size=breadth, sleeves=len(panel.sleeves))
    targets = targets_for(masks, panel.universes[breadth], allocation)
    holdings = list(masks)
    cost = ONE_WAY_COST_BASIS_POINTS
    run = simulate_portfolio(panel.series, targets, cost_basis_points=cost, holdings=holdings)
    drawdown = max_drawdown(run.equity_curve)
    annual = cagr(run.total_return, bars=run.bars, timeframe=TIMEFRAME)
    best_year, years_up = _yearly(run)
    card: dict[str, Any] = {
        "cagr": annual,
        "max_drawdown": drawdown,
        "calmar": calmar(annual, drawdown),
        "profit_factor": _profit_factor(run.episode_returns),
        "total_return": run.total_return,
        "turnover": run.turnover,
        "fees": run.cost_paid,
        "episodes": len(run.episode_returns),
        "exposure": Decimal(run.bars_held) / Decimal(run.bars) if run.bars else None,
        "top_asset_share": _share(run.by_asset),
        "top_sleeve_share": _share(run.by_sleeve) if len(run.by_sleeve) > 1 else None,
        "assets_touched": sum(1 for c in run.by_asset if c.episodes),
        "single_year_share": best_year,
        "years_positive_share": years_up,
        "by_sleeve": {c.name: c.net_profit for c in run.by_sleeve},
    }
    for multiplier in COST_STRESS_MULTIPLIERS:
        stressed = simulate_portfolio(
            panel.series, targets, cost_basis_points=cost * multiplier, holdings=holdings
        )
        card[f"cagr_x{multiplier}"] = cagr(
            stressed.total_return, bars=stressed.bars, timeframe=TIMEFRAME
        )
    oos = simulate_portfolio(
        panel.series, targets, cost_basis_points=cost, start=OOS_START, holdings=holdings
    )
    card["oos_return"] = oos.total_return
    card["oos_drawdown"] = max_drawdown(oos.equity_curve)
    return card


def _churn(masks: dict[Holding, tuple[bool, ...]], panel: Panel) -> tuple[int, Decimal | None, int]:
    """Return re-entries, the held share of what the signal wanted, and the longest shutout."""
    shared = [holding for holding in masks if holding in panel.signal_masks]
    if not shared:
        return 0, None, 0
    churn = sum(count_re_entries(masks[h], panel.signal_masks[h]) for h in shared)
    wanted = sum(sum(panel.signal_masks[h]) for h in shared)
    covered = sum(
        sum(1 for a, b in zip(panel.signal_masks[h], masks[h], strict=True) if a and b)
        for h in shared
    )
    shutout = max(
        (longest_flat_while_wanted(masks[h], panel.signal_masks[h]) for h in shared), default=0
    )
    return churn, (Decimal(covered) / Decimal(wanted) if wanted else None), shutout


def _result(
    basis: Basis, breadth: int, card: dict[str, Any], *, stops: int, churn: tuple[int, Any, int]
) -> BreadthResult:
    """Fold one measured card into the declared record."""
    return BreadthResult(
        basis=basis,
        breadth=breadth,
        annual=card["cagr"],
        max_drawdown=card["max_drawdown"],
        calmar_ratio=card["calmar"],
        profit_factor=card["profit_factor"],
        turnover=card["turnover"],
        fees=card["fees"],
        stops=stops,
        re_entries=churn[0],
        held_share_of_wanted=churn[1],
        out_of_sample_return=card["oos_return"],
        annual_at_double_cost=card["cagr_x2"],
        annual_at_triple_cost=card["cagr_x3"],
        single_year_share=card["single_year_share"],
        top_asset_share=card["top_asset_share"],
        top_sleeve_share=card["top_sleeve_share"],
        years_positive_share=card["years_positive_share"],
    )


def _row(label: str, card: dict[str, Any], result: BreadthResult) -> str:
    """Return one printed table row."""
    return (
        f"{label:9} {float(card['cagr'] or 0):+7.2%} {float(card['max_drawdown']):6.2%} "
        f"{float(card['calmar'] or 0):6.2f} {float(card['profit_factor'] or 0):5.2f} "
        f"{float(card['turnover']):8.1f} {float(card['fees']):9.0f} "
        f"{result.stops:6d} {result.re_entries:6d} "
        f"{float(result.held_share_of_wanted or 0):6.1%} "
        f"{float(card['oos_return'] or 0):+7.2%} {float(card['cagr_x2'] or 0):+7.2%} "
        f"{float(card['cagr_x3'] or 0):+7.2%} "
        f"{float(card['top_asset_share'] or 0):6.1%} "
        f"{float(card['top_sleeve_share'] or 0):6.1%} "
        f"{float(card['years_positive_share'] or 0):5.0%}"
    )


def main() -> int:
    """Measure all three bases at both declared breadths and judge the candidate."""
    keys = [probe.key for probe in SLEEVES]
    series = {raw: load(raw) for raw in pool_symbols()}
    grid = align(series)
    slots = positions(series, grid)
    panel = Panel(
        grid=grid,
        series=series,
        universes={
            breadth: eligible_universe(series, slots, window=LIQUIDITY_WINDOW, size=breadth)
            for breadth in BREADTHS
        },
        signal_masks=_signal_masks(keys, grid),
        sleeves=keys,
    )

    v2_masks, v2_fail, v2_codes, _, v2_forced = _engine_masks(V2_CACHE, grid, keys)
    v3_masks, v3_fail, v3_codes, v3_kinds, v3_forced = _engine_masks(V3_CACHE, grid, keys)
    sys.stdout.write(
        f"M38 -- {len(series)} markets, {len(keys)} sleeves, {TIMEFRAME.value}, "
        f"grid {len(grid)} bars {grid[0].date()} -> {grid[-1].date()}\n"
        f"signal pairs {len(panel.signal_masks)}  V2 pairs {len(v2_masks)} "
        f"(failed {len(v2_fail)})  V3 pairs {len(v3_masks)} (failed {len(v3_fail)})\n"
    )
    if v2_fail or v3_fail:
        for key, reason in sorted({**v2_fail, **v3_fail}.items()):
            sys.stdout.write(f"  FAILED {key}: {reason}\n")

    deployed = baseline_risk(Timeframe(TIMEFRAME.value))
    candidate = alternative_risk(
        next(a for a in ALTERNATIVES if a.key == CANDIDATE_KEY), Timeframe(TIMEFRAME.value)
    )
    held = invariants_for(candidate, deployed)
    missing = sorted(i.value for i in SAFETY_INVARIANTS - held)
    sys.stdout.write(
        f"\nsafety invariants held by the candidate: {len(held)}/{len(SAFETY_INVARIANTS)}"
        f"{'' if not missing else '  MISSING: ' + ', '.join(missing)}\n"
        f"safety invariants held by Risk V2:        "
        f"{len(invariants_for(deployed, deployed))}/{len(SAFETY_INVARIANTS)}\n\n"
    )

    header = (
        f"{'BASIS':9} {'CAGR':>8} {'DD':>7} {'CALMAR':>7} {'PF':>6} {'TURNOVER':>9} "
        f"{'FEES':>10} {'STOPS':>7} {'RE-ENT':>7} {'HELD%':>7} {'OOS':>8} {'x2':>8} "
        f"{'x3':>8} {'TOPAST':>7} {'TOPSLV':>7} {'YRS+':>6}"
    )
    report: dict[str, Any] = {
        "milestone": "m38",
        "timeframe": TIMEFRAME.value,
        "candidate": CANDIDATE_KEY,
        "universe": sorted(series),
        "breadths": list(BREADTHS),
        "exposure_normalised": False,
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "safety_invariants_held": sorted(i.value for i in held),
        "safety_invariants_required": sorted(i.value for i in SAFETY_INVARIANTS),
        "v2_exits_by_code": dict(v2_codes),
        "v3_exits_by_code": dict(v3_codes),
        "v3_exits_by_stop_kind": dict(v3_kinds),
        "v2_forced_exits": v2_forced,
        "v3_forced_exits": v3_forced,
        "failures": {**v2_fail, **v3_fail},
        "breadths_detail": {},
        "verdict": {},
    }

    overall = True
    for breadth in BREADTHS:
        sys.stdout.write(f"BREADTH {breadth}\n{header}\n{'-' * len(header)}\n")
        cards: dict[Basis, dict[str, Any]] = {}
        results: dict[Basis, BreadthResult] = {}
        for basis, masks, stops in (
            (Basis.SIGNAL, panel.signal_masks, 0),
            (Basis.RISK_V2, v2_masks, v2_codes.get("protective_stop", 0)),
            (Basis.RISK_V3, v3_masks, v3_codes.get("protective_stop", 0)),
        ):
            card = _measure(masks, panel, breadth)
            churn = (0, Decimal(1), 0) if basis is Basis.SIGNAL else _churn(masks, panel)
            result = _result(basis, breadth, card, stops=stops, churn=churn)
            cards[basis] = card
            results[basis] = result
            sys.stdout.write(_row(basis.value, card, result) + "\n")
            sys.stdout.flush()

        ok, failed = passes(results[Basis.RISK_V3], results[Basis.RISK_V2], invariants_held=held)
        overall = overall and ok
        sys.stdout.write(
            f"\n  breadth {breadth}: {'PASSES' if ok else 'FAILS'}"
            f"{'' if ok else ' -> ' + ', '.join(g.value for g in failed)}\n\n"
        )
        report["breadths_detail"][str(breadth)] = {basis.value: cards[basis] for basis in cards}
        report["verdict"][str(breadth)] = {
            "passes": ok,
            "failed": [g.value for g in failed],
        }

    report["all_breadths_pass"] = overall
    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / "risk_v3_full_universe_4h.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    verdict = (
        "GO: the Risk V3 candidate survives the full universe"
        if overall
        else "NO-GO: the candidate does not survive the full universe"
    )
    sys.stdout.write(f"-> {out.relative_to(ROOT)}\n{verdict}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
