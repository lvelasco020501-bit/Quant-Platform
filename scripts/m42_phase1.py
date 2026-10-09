"""M42 Phase 1 — equal weight against inverse volatility, over evidence already on disk.

No ``BacktestEngine`` run. The positions are M38's sixty-pair Risk V2 masks, the universe is
M32's point-in-time liquidity rule, the costs and the out-of-sample window are M30's, and both
breadths are M36's. The only thing that differs between the two rows is the arithmetic deciding
how much of the account stands behind each already-decided position.

**One measurement path, and it is the path M38 used.** The mask reader, the attribution helpers
and the yearly split are imported from ``m42_compare``'s neighbour rather than rewritten, and
the equal-weight arm is checked against M38's own published Risk V2 card before anything is
judged. If the baseline does not reproduce, the comparison is not measuring what it claims and
the script refuses to continue.

Usage:
    uv run python scripts/m42_phase1.py
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m42 import (
    BREADTHS,
    COST_STRESS_MULTIPLIERS,
    EXPOSURE_MATCH_TOLERANCE,
    LIQUIDITY_WINDOW,
    MIN_CAGR_CONSERVATION,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    SLEEVES,
    TIMEFRAME,
    VOLATILITY_FEATURE,
    VOLATILITY_WINDOW,
    Arm,
    ArmResult,
    passes,
    pool_symbols,
)
from quantplatform.research.portfolio import (
    Allocation,
    EquityPoint,
    Holding,
    deployed,
    inverse_vol_targets,
    positions,
    realised_volatility,
    simulate_portfolio,
    targets_for,
)
from quantplatform.research.rotation import align, eligible_universe, max_drawdown

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import load
from m38_compare import _engine_masks, _profit_factor, _share, _yearly
from m38_extract import V2_CACHE

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m42"
VOL_CACHE: Final[Path] = HOME / "volatility_4h.json"
M38_REPORT: Final[Path] = ROOT / "var/research/m38/risk_v3_full_universe_4h.json"
ZERO: Final[Decimal] = Decimal(0)
TERCILES: Final[tuple[str, ...]] = ("quietest", "middle", "noisiest")
REPRODUCTION_TOLERANCE: Final[Decimal] = Decimal("1e-20")
"""How far the equal-weight arm may sit from M38's published Risk V2 card.

Twenty orders of magnitude below a basis point: it admits a different Decimal rounding path and
nothing else. A wider tolerance would let the baseline be a different portfolio wearing M38's
name, which is exactly the failure this check exists to catch.

It earned its keep on the first run: three attribution fields disagreed in the sixth
significant digit, and the cause turned out to be that ``_rebalance`` visited holdings in set
order, so the split of each bar's cost between them depended on the interpreter's hash seed.
The run's return, drawdown, turnover and total fees were unaffected -- they are the same
factors in a different sequence -- which is exactly why nothing had ever noticed. The order is
now sorted and M38's report was regenerated under it, so the comparison is against deterministic
numbers rather than against one process's accident. **The tolerance was not widened.**"""


# --- Volatility, computed once and cached ---------------------------------------------------------


def _volatility(
    series: Mapping[str, Any], slots: Mapping[str, Sequence[int | None]]
) -> dict[str, tuple[Decimal | None, ...]]:
    """Return the declared volatility per market per slot, reading the cache if it exists."""
    if VOL_CACHE.exists():
        cached = json.loads(VOL_CACHE.read_text(encoding="utf-8"))
        if cached["feature"] == VOLATILITY_FEATURE and sorted(cached["columns"]) == sorted(series):
            sys.stdout.write(f"volatility read from {VOL_CACHE.relative_to(ROOT)}\n")
            return {
                asset: tuple(None if value is None else Decimal(value) for value in column)
                for asset, column in cached["columns"].items()
            }
    sys.stdout.write(f"computing {VOLATILITY_FEATURE} for {len(series)} markets...\n")
    sys.stdout.flush()
    columns = realised_volatility(series, slots, window=VOLATILITY_WINDOW)
    HOME.mkdir(parents=True, exist_ok=True)
    VOL_CACHE.write_text(
        json.dumps(
            {
                "milestone": "m42",
                "feature": VOLATILITY_FEATURE,
                "window": VOLATILITY_WINDOW,
                "columns": {
                    asset: [None if value is None else str(value) for value in column]
                    for asset, column in columns.items()
                },
                "generated_at": datetime.now(UTC).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return columns


# --- Measurement ----------------------------------------------------------------------------------


def _drawdown_window(curve: Sequence[EquityPoint]) -> tuple[datetime, datetime] | None:
    """Return the instants bounding the deepest drawdown, so it can be attributed."""
    if not curve:
        return None
    peak = curve[0]
    worst = ZERO
    bounds: tuple[datetime, datetime] | None = None
    for point in curve:
        if point.equity > peak.equity:
            peak = point
        if peak.equity > ZERO:
            fall = (peak.equity - point.equity) / peak.equity
            if fall > worst:
                worst = fall
                bounds = (peak.at, point.at)
    return bounds


def _measure(
    targets: Sequence[Mapping[Holding, Decimal]],
    series: Mapping[str, Any],
    holdings: Sequence[Holding],
) -> dict[str, Any]:
    """Run one allocation at base cost, both stress multipliers and out of sample."""
    cost = ONE_WAY_COST_BASIS_POINTS
    run = simulate_portfolio(series, targets, cost_basis_points=cost, holdings=holdings)
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
            series, targets, cost_basis_points=cost * multiplier, holdings=holdings
        )
        card[f"cagr_x{multiplier}"] = cagr(
            stressed.total_return, bars=stressed.bars, timeframe=TIMEFRAME
        )
    oos = simulate_portfolio(
        series, targets, cost_basis_points=cost, start=OOS_START, holdings=holdings
    )
    card["oos_return"] = oos.total_return
    card["oos_drawdown"] = max_drawdown(oos.equity_curve)
    bounds = _drawdown_window(run.equity_curve)
    card["drawdown_window"] = [bound.isoformat() for bound in bounds] if bounds else None
    if bounds:
        inside = simulate_portfolio(
            series,
            targets,
            cost_basis_points=cost,
            start=bounds[0],
            end=bounds[1],
            holdings=holdings,
        )
        losers = sorted(inside.by_asset, key=lambda c: c.net_profit)
        card["drawdown_by_asset"] = {c.name: c.net_profit for c in losers[:5]}
    return card


def _result(
    arm: Arm,
    breadth: int,
    card: dict[str, Any],
    *,
    unfunded_bars: int = 0,
    capital_deficit: Decimal = ZERO,
) -> ArmResult:
    """Fold one measured card into the declared record."""
    return ArmResult(
        arm=arm,
        breadth=breadth,
        annual=card["cagr"],
        max_drawdown=card["max_drawdown"],
        calmar_ratio=card["calmar"],
        profit_factor=card["profit_factor"],
        turnover=card["turnover"],
        fees=card["fees"],
        exposure=card["exposure"],
        out_of_sample_return=card["oos_return"],
        annual_at_double_cost=card["cagr_x2"],
        annual_at_triple_cost=card["cagr_x3"],
        single_year_share=card["single_year_share"],
        years_positive_share=card["years_positive_share"],
        top_asset_share=card["top_asset_share"],
        top_sleeve_share=card["top_sleeve_share"],
        unfunded_bars=unfunded_bars,
        capital_deficit=capital_deficit,
    )


# --- Where the weight went ------------------------------------------------------------------------


def _by_volatility_tercile(
    targets: Sequence[Mapping[Holding, Decimal]],
    volatility: Mapping[str, Sequence[Decimal | None]],
) -> dict[str, Decimal]:
    """Return the share of deployed capital that sat in each volatility tercile.

    Terciles are cross-sectional and taken inside each bar among the markets actually funded
    there, so the answer is "of the money at work that day, how much was in its quietest
    third", which is the question inverse-vol is supposed to change.
    """
    totals = dict.fromkeys(TERCILES, ZERO)
    for slot, weights in enumerate(targets):
        by_asset: dict[str, Decimal] = {}
        for (_, asset), weight in weights.items():
            by_asset[asset] = by_asset.get(asset, ZERO) + weight
        ranked = sorted(
            (value, asset)
            for asset, weight in by_asset.items()
            if weight > ZERO and (value := volatility[asset][slot]) is not None
        )
        if not ranked:
            continue
        size = len(ranked)
        for index, (_, asset) in enumerate(ranked):
            bucket = TERCILES[min(index * 3 // size, 2)]
            totals[bucket] += by_asset[asset]
    grand = sum(totals.values(), start=ZERO)
    return {name: (value / grand if grand > ZERO else ZERO) for name, value in totals.items()}


def _concentration(
    targets: Sequence[Mapping[Holding, Decimal]],
    volatility: Mapping[str, Sequence[Decimal | None]],
) -> dict[str, Decimal | None]:
    """Return how concentrated the book was in capital and in risk, averaged over funded bars.

    Two Herfindahl indices over the same bars. The capital one squares each market's share of
    the money at work; the risk one squares its share of ``weight x volatility``. Equal weight
    flattens the first and leaves the second to the market; inverse-vol is the proposal that
    the second is the one worth flattening, so reporting only one of them would hide the whole
    mechanism being tested.
    """
    capital: list[Decimal] = []
    risk: list[Decimal] = []
    for slot, weights in enumerate(targets):
        by_asset: dict[str, Decimal] = {}
        for (_, asset), weight in weights.items():
            by_asset[asset] = by_asset.get(asset, ZERO) + weight
        funded = {asset: weight for asset, weight in by_asset.items() if weight > ZERO}
        if not funded:
            continue
        total = sum(funded.values(), start=ZERO)
        capital.append(sum(((weight / total) ** 2 for weight in funded.values()), start=ZERO))
        exposures = {
            asset: weight * value
            for asset, weight in funded.items()
            if (value := volatility[asset][slot]) is not None
        }
        pool = sum(exposures.values(), start=ZERO)
        if pool > ZERO:
            risk.append(sum(((value / pool) ** 2 for value in exposures.values()), start=ZERO))
    return {
        "capital_herfindahl": sum(capital, start=ZERO) / Decimal(len(capital)) if capital else None,
        "risk_herfindahl": sum(risk, start=ZERO) / Decimal(len(risk)) if risk else None,
    }


# --- The baseline has to be the baseline ----------------------------------------------------------

_REPRODUCED: Final[tuple[str, ...]] = (
    "cagr",
    "max_drawdown",
    "calmar",
    "profit_factor",
    "turnover",
    "fees",
    "exposure",
    "oos_return",
    "cagr_x2",
    "cagr_x3",
    "top_asset_share",
    "top_sleeve_share",
    "single_year_share",
    "years_positive_share",
)


def _check_baseline_reproduces(card: dict[str, Any], breadth: int) -> dict[str, str]:
    """Return any field where the equal-weight arm disagrees with M38's own Risk V2 card.

    The equal-weight arm is not a new portfolio: it is the row M38 already published, recomputed
    here so that both arms come off one path. Checking it is what distinguishes "inverse-vol
    changed the result" from "something else in this script changed the result".
    """
    published = json.loads(M38_REPORT.read_text(encoding="utf-8"))
    theirs = published["breadths_detail"][str(breadth)]["risk_v2"]
    drift: dict[str, str] = {}
    for field in _REPRODUCED:
        mine = card[field]
        if mine is None or theirs.get(field) is None:
            if (mine is None) != (theirs.get(field) is None):
                drift[field] = f"{mine} vs {theirs.get(field)}"
            continue
        if abs(Decimal(str(mine)) - Decimal(theirs[field])) > REPRODUCTION_TOLERANCE:
            drift[field] = f"{mine} vs {theirs[field]}"
    return drift


# --- Reporting ------------------------------------------------------------------------------------

HEADER: Final[str] = (
    f"{'ARM':13} {'CAGR':>8} {'DD':>7} {'CALMAR':>7} {'PF':>6} {'TURNOVER':>9} {'FEES':>10} "
    f"{'EXPO':>6} {'OOS':>8} {'x2':>8} {'x3':>8} {'TOPAST':>7} {'TOPSLV':>7} {'YR1':>6} "
    f"{'YRS+':>6}"
)


def _row(arm: Arm, card: dict[str, Any]) -> str:
    """Return one printed table row."""
    return (
        f"{arm.value:13} {float(card['cagr'] or 0):+7.2%} {float(card['max_drawdown']):6.2%} "
        f"{float(card['calmar'] or 0):6.2f} {float(card['profit_factor'] or 0):5.2f} "
        f"{float(card['turnover']):8.1f} {float(card['fees']):9.0f} "
        f"{float(card['exposure'] or 0):5.1%} {float(card['oos_return'] or 0):+7.2%} "
        f"{float(card['cagr_x2'] or 0):+7.2%} {float(card['cagr_x3'] or 0):+7.2%} "
        f"{float(card['top_asset_share'] or 0):6.1%} "
        f"{float(card['top_sleeve_share'] or 0):6.1%} "
        f"{float(card['single_year_share'] or 0):5.0%} "
        f"{float(card['years_positive_share'] or 0):5.0%}"
    )


def _describe(arm: Arm, card: dict[str, Any]) -> None:
    """Print where one arm put its weight, in capital terms and in risk terms."""
    sys.stdout.write(
        f"  {arm.value:13} capital by vol tercile "
        f"quiet {float(card['quietest']):5.1%} mid {float(card['middle']):5.1%} "
        f"noisy {float(card['noisiest']):5.1%}   "
        f"HHI capital {float(card['capital_herfindahl'] or 0):.4f} "
        f"risk {float(card['risk_herfindahl'] or 0):.4f}\n"
    )
    if card.get("drawdown_by_asset"):
        worst = ", ".join(
            f"{name} {float(value):+.0f}" for name, value in card["drawdown_by_asset"].items()
        )
        sys.stdout.write(f"  {arm.value:13} drawdown window losers: {worst}\n")


def _one_breadth(
    breadth: int,
    *,
    series: Mapping[str, Any],
    slots: Mapping[str, Sequence[int | None]],
    masks: Mapping[Holding, tuple[bool, ...]],
    volatility: Mapping[str, Sequence[Decimal | None]],
    sleeves: int,
) -> tuple[bool, tuple[str, ...], dict[str, Any]] | None:
    """Measure both arms at one breadth. ``None`` means the baseline did not reproduce."""
    eligible = eligible_universe(series, slots, window=LIQUIDITY_WINDOW, size=breadth)
    allocation = Allocation(universe_size=breadth, sleeves=sleeves)
    equal = targets_for(masks, eligible, allocation)
    reference = deployed(equal)
    plan = inverse_vol_targets(
        masks, eligible, allocation, volatility=volatility, reference=reference
    )
    shortfall = sum((value for value in plan.deficit if value > ZERO), start=ZERO)
    drift = max((abs(value) for value in plan.deficit), default=ZERO)
    holdings = sorted(masks)

    sys.stdout.write(f"BREADTH {breadth}\n{HEADER}\n{'-' * len(HEADER)}\n")
    cards: dict[Arm, dict[str, Any]] = {}
    results: dict[Arm, ArmResult] = {}
    for arm, targets in ((Arm.EQUAL_WEIGHT, equal), (Arm.INVERSE_VOL, plan.targets)):
        card = _measure(targets, series, holdings)
        card.update(_by_volatility_tercile(targets, volatility))
        card.update(_concentration(targets, volatility))
        cards[arm] = card
        results[arm] = (
            _result(arm, breadth, card)
            if arm is Arm.EQUAL_WEIGHT
            else _result(
                arm,
                breadth,
                card,
                unfunded_bars=plan.unfunded_bars,
                capital_deficit=shortfall,
            )
        )
        sys.stdout.write(_row(arm, card) + "\n")
        sys.stdout.flush()

    baseline_drift = _check_baseline_reproduces(cards[Arm.EQUAL_WEIGHT], breadth)
    if baseline_drift:
        sys.stdout.write(
            "\n  REFUSING TO JUDGE: the equal-weight arm does not reproduce M38's Risk V2 "
            f"card: {baseline_drift}\n"
        )
        return None
    sys.stdout.write("\n  equal weight reproduces M38's published Risk V2 card exactly\n")
    sys.stdout.write(
        f"  exposure match: worst bar off by {float(drift):.2e} "
        f"(tolerance {float(EXPOSURE_MATCH_TOLERANCE):.0e}), "
        f"{plan.unfunded_bars} bars in cash for want of a volatility\n"
    )
    for arm, card in cards.items():
        _describe(arm, card)

    ok, failed = passes(results[Arm.INVERSE_VOL], results[Arm.EQUAL_WEIGHT])
    sys.stdout.write(
        f"\n  breadth {breadth}: inverse-vol {'PASSES' if ok else 'FAILS'}"
        f"{'' if ok else ' -> ' + ', '.join(gate.value for gate in failed)}\n\n"
    )
    return ok, tuple(gate.value for gate in failed), {arm.value: cards[arm] for arm in cards}


def main() -> int:
    """Measure both allocations at both breadths and judge the inverse-vol arm."""
    keys = [probe.key for probe in SLEEVES]
    series = {raw: load(raw) for raw in pool_symbols()}
    grid = align(series)
    slots = positions(series, grid)
    masks, failures, _, _, _ = _engine_masks(V2_CACHE, grid, keys)
    sys.stdout.write(
        f"M42 Phase 1 -- {len(series)} markets, {len(keys)} sleeves, {TIMEFRAME.value}, "
        f"grid {len(grid)} bars {grid[0].date()} -> {grid[-1].date()}\n"
        f"Risk V2 pairs {len(masks)} (failed {len(failures)}); "
        f"volatility {VOLATILITY_FEATURE} (window {VOLATILITY_WINDOW} = M32's {LIQUIDITY_WINDOW})\n"
    )
    for key, reason in sorted(failures.items()):
        sys.stdout.write(f"  FAILED {key}: {reason}\n")

    volatility = _volatility(series, slots)
    missing = sum(1 for column in volatility.values() for value in column if value is None)
    sys.stdout.write(
        f"slots without a usable volatility: {missing} of "
        f"{len(grid) * len(series)} market-slots\n\n"
    )

    report: dict[str, Any] = {
        "milestone": "m42",
        "phase": 1,
        "timeframe": TIMEFRAME.value,
        "universe": sorted(series),
        "breadths": list(BREADTHS),
        "basis": "Risk V2 masks from M36/M38; no engine run",
        "volatility_feature": VOLATILITY_FEATURE,
        "volatility_window": VOLATILITY_WINDOW,
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "min_cagr_conservation": MIN_CAGR_CONSERVATION,
        "generated_at": datetime.now(UTC).isoformat(),
        "breadths_detail": {},
        "verdict": {},
    }

    overall = True
    for breadth in BREADTHS:
        measured = _one_breadth(
            breadth,
            series=series,
            slots=slots,
            masks=masks,
            volatility=volatility,
            sleeves=len(keys),
        )
        if measured is None:
            return 1
        ok, failed, detail = measured
        overall = overall and ok
        report["breadths_detail"][str(breadth)] = detail
        report["verdict"][str(breadth)] = {"passes": ok, "failed": list(failed)}

    report["all_breadths_pass"] = overall
    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / "inverse_vol_phase1_4h.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"-> {out.relative_to(ROOT)}\n"
        + (
            "PHASE 1 PASSES: open Phase 2\n"
            if overall
            else "PHASE 1 FAILS: stop, as M41 declared. No engine run, no ninth family.\n"
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
