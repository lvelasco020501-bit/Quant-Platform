"""Rebuild the portfolio under every M37 ablation and attribute the churn to a mechanism.

Reads what ``m37_ablate.py`` extracted -- one set of real position intervals per variant per
sleeve-market pair, plus the exit tally that says what ended each position -- and runs the
declared allocation over each variant's intervals. The baseline variant is the control every
other row is measured against, because M37's six-market universe makes none of these levels
comparable with M36's thirty-market figures.

**Exposure is deliberately not normalised.** The allocator is byte-identical across variants:
same per-signal weight, same per-asset cap, same universe, same costs. Only the masks differ.
M35 had to hold aggregate exposure constant because changing breadth mechanically changed
position size and conflated two variables; nothing of the kind happens here, and normalising
would instead *hide* the effect being measured -- a risk mechanism that takes the account out
of the market earlier is supposed to show up as less time in the market. Exposure is reported
as its own column so that is visible rather than implicit.

Usage:
    uv run python scripts/m37_attribute.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m32 import LIQUIDITY_WINDOW
from quantplatform.research.m36 import SLEEVES, TIMEFRAME
from quantplatform.research.m37 import (
    ABLATIONS,
    ASSETS_M37,
    COST_STRESS_MULTIPLIERS,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    UNIVERSE_M37,
    MechanismRow,
    primary_causes,
)
from quantplatform.research.m37_probe import re_entries as count_re_entries
from quantplatform.research.portfolio import (
    Allocation,
    Holding,
    PortfolioRun,
    positions,
    simulate_portfolio,
    targets_for,
)
from quantplatform.research.rotation import align, eligible_universe, max_drawdown
from quantplatform.research.sleeve import Interval, long_mask

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import CACHE as MASK_CACHE
from m35_masks import load
from m37_ablate import CACHE as ABLATION_CACHE

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m37"


def _signal_masks(keys: list[str]) -> dict[Holding, tuple[bool, ...]]:
    """Return the untouched signal timelines, restricted to M37's universe and sleeves."""
    cache = json.loads(MASK_CACHE.read_text(encoding="utf-8"))
    out: dict[Holding, tuple[bool, ...]] = {}
    for packed_key, packed in cache["masks"].items():
        sleeve, market = packed_key.split("|")
        if sleeve in keys and market in ASSETS_M37:
            out[sleeve, market] = tuple(c == "1" for c in packed)
    return out


def _variant_masks(
    rows: dict[str, Any], variant: str, grid: Sequence[datetime]
) -> tuple[dict[Holding, tuple[bool, ...]], dict[str, str]]:
    """Return one variant's engine timelines, and any pair of it that failed to run."""
    masks: dict[Holding, tuple[bool, ...]] = {}
    failures: dict[str, str] = {}
    for key, row in rows.items():
        if row["variant"] != variant:
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
    return masks, failures


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


def _card(run: PortfolioRun) -> dict[str, Any]:
    """Return every figure one run offers, with no judgement applied."""
    drawdown = max_drawdown(run.equity_curve)
    annual = cagr(run.total_return, bars=run.bars, timeframe=TIMEFRAME)
    return {
        "cagr": annual,
        "max_drawdown": drawdown,
        "calmar": calmar(annual, drawdown),
        "total_return": run.total_return,
        "turnover": run.turnover,
        "fees": run.cost_paid,
        "episodes": len(run.episode_returns),
        "exposure": Decimal(run.bars_held) / Decimal(run.bars) if run.bars else None,
        "bars": run.bars,
        "single_year_share": _single_year_share(run),
    }


def _measure(
    masks: dict[Holding, tuple[bool, ...]],
    series: dict[str, Any],
    universe: tuple[frozenset[str], ...],
    allocation: Allocation,
) -> dict[str, Any]:
    """Run one basis at base cost, at both stress multipliers, and over the OOS window."""
    cost = ONE_WAY_COST_BASIS_POINTS
    targets = targets_for(masks, universe, allocation)
    holdings = list(masks)
    base = _card(simulate_portfolio(series, targets, cost_basis_points=cost, holdings=holdings))
    for multiplier in COST_STRESS_MULTIPLIERS:
        stressed = simulate_portfolio(
            series, targets, cost_basis_points=cost * multiplier, holdings=holdings
        )
        base[f"cagr_x{multiplier}"] = _card(stressed)["cagr"]
    oos = simulate_portfolio(
        series, targets, cost_basis_points=cost, start=OOS_START, holdings=holdings
    )
    base["oos_return"] = oos.total_return
    base["oos_drawdown"] = max_drawdown(oos.equity_curve)
    return base


def _print_header(signals: dict[str, Any]) -> None:
    """Print the table's header and the signal basis, which every variant is read against."""
    header = (
        f"{'VAR':5} {'CAGR':>8} {'DD':>7} {'CALMAR':>7} {'TURNOVER':>9} {'FEES':>10} "
        f"{'RE-ENT':>7} {'STOPS':>6} {'FORCED':>7} {'EXPO':>6} {'OOS':>8} "
        f"{'x2':>8} {'x3':>8}"
    )
    sys.stdout.write(f"{header}\n{'-' * len(header)}\n")
    sys.stdout.write(
        f"{'SIG':5} {float(signals['cagr'] or 0):+7.2%} "
        f"{float(signals['max_drawdown']):6.2%} {float(signals['calmar'] or 0):7.2f} "
        f"{float(signals['turnover']):9.1f} {float(signals['fees']):10.0f} "
        f"{'-':>7} {'-':>6} {'-':>7} {float(signals['exposure'] or 0):5.1%} "
        f"{float(signals['oos_return'] or 0):+7.2%} "
        f"{float(signals['cagr_x2'] or 0):+7.2%} {float(signals['cagr_x3'] or 0):+7.2%}\n"
    )


def main() -> int:
    """Measure every variant, print the attribution table, and name the primary causes."""
    cache = json.loads(ABLATION_CACHE.read_text(encoding="utf-8"))
    rows: dict[str, Any] = cache["pairs"]
    keys = [probe.key for probe in SLEEVES]
    series = {raw: load(raw) for raw in ASSETS_M37}
    grid = align(series)
    universe = eligible_universe(
        series, positions(series, grid), window=LIQUIDITY_WINDOW, size=UNIVERSE_M37
    )
    allocation = Allocation(universe_size=UNIVERSE_M37, sleeves=len(keys))
    signal_masks = _signal_masks(keys)

    sys.stdout.write(
        f"M37 ablation -- {len(ASSETS_M37)} markets, {len(keys)} sleeves, "
        f"breadth {UNIVERSE_M37}, {TIMEFRAME.value}\n"
        f"grid {len(grid)} bars  {grid[0].date()} -> {grid[-1].date()}\n\n"
    )

    signals = _measure(signal_masks, series, universe, allocation)
    report: dict[str, Any] = {
        "milestone": "m37",
        "timeframe": TIMEFRAME.value,
        "universe": list(ASSETS_M37),
        "breadth": UNIVERSE_M37,
        "exposure_normalised": False,
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "signals": signals,
        "variants": {},
    }

    _print_header(signals)

    measured: dict[str, MechanismRow] = {}
    for ablation in ABLATIONS:
        masks, failures = _variant_masks(rows, ablation.key, grid)
        if not masks:
            sys.stdout.write(f"{ablation.key:5} not extracted\n")
            continue
        card = _measure(masks, series, universe, allocation)
        codes: Counter[str] = Counter()
        kinds: Counter[str] = Counter()
        forced = 0
        for row in rows.values():
            if row["variant"] != ablation.key or row["status"] != "succeeded":
                continue
            codes.update(row["exits_by_code"])
            kinds.update(row["exits_by_stop_kind"])
            forced += row["forced_exits"]
        re_entries = sum(
            count_re_entries(mask, signal_masks[holding])
            for holding, mask in masks.items()
            if holding in signal_masks
        )
        measured[ablation.key] = MechanismRow(
            key=ablation.key,
            annual=card["cagr"],
            max_drawdown=card["max_drawdown"],
            calmar_ratio=card["calmar"],
            turnover=card["turnover"],
            fees=card["fees"],
            re_entries=re_entries,
            stops=codes.get("protective_stop", 0),
            out_of_sample_return=card["oos_return"],
            annual_at_double_cost=card["cagr_x2"],
            annual_at_triple_cost=card["cagr_x3"],
        )
        report["variants"][ablation.key] = {
            "label": ablation.label,
            "disables": [m.value for m in ablation.disables],
            "is_control": ablation.is_control,
            "exits_by_code": dict(codes),
            "exits_by_stop_kind": dict(kinds),
            "forced_exits": forced,
            "re_entries": re_entries,
            "failures": failures,
            **card,
        }
        sys.stdout.write(
            f"{ablation.key:5} {float(card['cagr'] or 0):+7.2%} "
            f"{float(card['max_drawdown']):6.2%} {float(card['calmar'] or 0):7.2f} "
            f"{float(card['turnover']):9.1f} {float(card['fees']):10.0f} "
            f"{re_entries:7d} {codes.get('protective_stop', 0):6d} {forced:7d} "
            f"{float(card['exposure'] or 0):5.1%} {float(card['oos_return'] or 0):+7.2%} "
            f"{float(card['cagr_x2'] or 0):+7.2%} {float(card['cagr_x3'] or 0):+7.2%}\n"
        )
        sys.stdout.flush()

    base = measured.get("BASE")
    if base is None:
        sys.stdout.write("\nno baseline: attribution not possible\n")
        return 1

    control = measured.get("H")
    identical = control is not None and control == base.model_copy(update={"key": "H"})
    sys.stdout.write(
        f"\ncontrol H reproduces the baseline: {identical}"
        f"{'' if identical else '  <-- HARNESS IS WRONG, no row below is trustworthy'}\n"
    )
    report["control_reproduces_baseline"] = identical

    signals_annual = signals["cagr"]
    causes = primary_causes(tuple(measured.values()), base=base, signals_annual=signals_annual)
    sys.stdout.write(f"primary cause(s) by the pre-declared rule: {causes or 'NONE'}\n")
    report["primary_causes"] = list(causes)
    report["phase_two_warranted"] = bool(causes)
    if not causes:
        sys.stdout.write(
            "no single mechanism clears both thresholds: Risk V2 is not dominated by one "
            "part of itself -> STOP and close, per the declaration\n"
        )

    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / "ablation_attribution_4h.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n-> {out.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
