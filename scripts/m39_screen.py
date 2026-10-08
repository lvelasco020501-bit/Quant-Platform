"""Screen M39's six declared variants through Risk V2 from their first measurement.

Each variant runs on all six markets through the certified engine with Risk V2 live, and the
resulting position intervals are assembled into one equal-weight portfolio. The same rule's
untouched signal timelines are assembled the same way, which is the diagnostic: the ratio
between the two annual returns is what this milestone is about, and nothing passes or fails on
the signal basis alone.

One measurement path for both bases, so a difference between them is a difference in what
decided the positions rather than in how they were measured. Exposure is not normalised, for
M37's reason -- a risk layer that leaves the market earlier should show up as less time in it.

1D bars make this cheap: about nine seconds an engine run, so thirty-six runs cost minutes.

Usage:
    uv run python scripts/m39_screen.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.m29 import cagr, calmar
from quantplatform.research.m37_probe import AblationRiskEngine, longest_flat_while_wanted
from quantplatform.research.m37_probe import re_entries as count_re_entries
from quantplatform.research.m39 import (
    ASSETS_M39,
    COST_STRESS_MULTIPLIERS,
    MIN_COMPATIBILITY_RATIO,
    ONE_WAY_COST_BASIS_POINTS,
    OOS_START,
    PREDICTED_WORST_COMPATIBILITY,
    TIMEFRAME,
    VARIANTS_M39,
    Measured,
    Variant,
    compatibility_ratio,
    survives,
)
from quantplatform.research.m39_definitions import definition_for
from quantplatform.research.portfolio import (
    Allocation,
    Holding,
    PortfolioRun,
    SleeveContribution,
    positions,
    simulate_portfolio,
    targets_for,
)
from quantplatform.research.rotation import align, max_drawdown, yearly_returns
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sleeve import Interval, long_intervals, long_mask
from quantplatform.strategies.research import build_research_registry

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DATA: Final[Path] = ROOT / "data/raw/m30/out"
HOME: Final[Path] = ROOT / "var/research/m39"
SLEEVE: Final[str] = "V"
"""One sleeve per run: a variant is screened alone, never blended with another."""


def load(raw: str) -> tuple[MarketBar, ...]:
    """Read one market's canonical 1D series, re-validating every row through the model."""
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
                    source="m39",
                    is_closed=True,
                )
            )
    return tuple(bars)


@dataclass(frozen=True)
class Panel:
    """The fixed context every variant is measured against."""

    series: dict[str, tuple[MarketBar, ...]]
    grid: Sequence[datetime]
    universe: tuple[frozenset[str], ...]
    allocation: Allocation


def _engine_run(raw: str, variant: Variant, bars: tuple[MarketBar, ...]) -> dict[str, Any]:
    """Run one market through the full chain with Risk V2 live."""
    at = time.time()
    captured: list[AblationRiskEngine] = []
    definition = definition_for(raw, variant)

    def _engine(_: Any) -> AblationRiskEngine:  # noqa: ANN401 - the factory's own signature
        engine = AblationRiskEngine(config=definition.risk)
        captured.append(engine)
        return engine

    factory = ExperimentEngineFactory(
        registry=build_research_registry(),
        features_for=features_for,
        quote_asset="USDT",
        risk_engine_for=_engine,
    )
    result = ExperimentRunner().run(
        definition, bars=bars, factory=factory, code_revision="m39-screen"
    )
    tally = captured[-1].tally if captured else None
    return {
        "market": raw,
        "status": result.status.value,
        "trades": len(result.trades),
        "seconds": round(time.time() - at, 1),
        "intervals": [
            {"opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat()}
            for t in sorted(result.trades, key=lambda t: t.opened_at)
        ],
        "exits_by_code": dict(tally.by_code) if tally else {},
        "forced_exits": tally.forced_exits if tally else 0,
        "error": result.error,
    }


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
        Decimal(sum(1 for v in returns.values() if v > 0)) / Decimal(len(returns))
        if returns
        else None
    )
    return best, up


def _card(masks: dict[Holding, tuple[bool, ...]], panel: Panel) -> dict[str, Any]:
    """Measure one basis: base cost, both stress multipliers, and out of sample."""
    targets = targets_for(masks, panel.universe, panel.allocation)
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
        "assets_positive": sum(1 for c in run.by_asset if c.net_profit > 0),
        "assets_touched": sum(1 for c in run.by_asset if c.episodes),
        "single_year_share": best_year,
        "years_positive_share": years_up,
        "by_asset": {c.name: c.net_profit for c in run.by_asset if c.episodes},
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


def _signal_masks(variant: Variant, panel: Panel) -> dict[Holding, tuple[bool, ...]]:
    """Return the variant's untouched signal timelines. Diagnostic only."""
    registry = build_research_registry()
    out: dict[Holding, tuple[bool, ...]] = {}
    for raw, bars in panel.series.items():
        strategy = registry.create(variant.strategy_id, dict(variant.params))
        intervals = long_intervals(strategy, bars, pipelines=features_for)
        out[SLEEVE, raw] = long_mask(intervals, panel.grid)
    return out


def _engine_masks(runs: list[dict[str, Any]], panel: Panel) -> dict[Holding, tuple[bool, ...]]:
    """Return the timelines Risk V2's own positions occupied."""
    out: dict[Holding, tuple[bool, ...]] = {}
    for row in runs:
        if row["status"] != "succeeded":
            continue
        intervals = tuple(
            Interval(
                entered_at=datetime.fromisoformat(span["opened_at"]),
                exited_at=datetime.fromisoformat(span["closed_at"]),
            )
            for span in row["intervals"]
        )
        out[SLEEVE, row["market"]] = long_mask(intervals, panel.grid)
    return out


def _one_variant(variant: Variant, panel: Panel) -> tuple[Measured, dict[str, Any]]:
    """Run one variant on every market, assemble both bases, and judge it."""
    runs = [_engine_run(raw, variant, bars) for raw, bars in panel.series.items()]
    failures = {r["market"]: r["error"] or r["status"] for r in runs if r["status"] != "succeeded"}
    engine = _engine_masks(runs, panel)
    signal = _signal_masks(variant, panel)
    risk_card = _card(engine, panel) if engine else None
    signal_card = _card(signal, panel)

    shared = [h for h in engine if h in signal]
    churn = sum(count_re_entries(engine[h], signal[h]) for h in shared)
    wanted = sum(sum(signal[h]) for h in shared)
    covered = sum(
        sum(1 for a, b in zip(signal[h], engine[h], strict=True) if a and b) for h in shared
    )
    shutout = max((longest_flat_while_wanted(engine[h], signal[h]) for h in shared), default=0)
    codes: Counter[str] = Counter()
    for row in runs:
        codes.update(row["exits_by_code"])

    measured = Measured(
        key=variant.key,
        annual=risk_card["cagr"] if risk_card else None,
        max_drawdown=risk_card["max_drawdown"] if risk_card else Decimal(0),
        calmar_ratio=risk_card["calmar"] if risk_card else None,
        profit_factor=risk_card["profit_factor"] if risk_card else None,
        trades=sum(r["trades"] for r in runs),
        turnover=risk_card["turnover"] if risk_card else Decimal(0),
        fees=risk_card["fees"] if risk_card else Decimal(0),
        forced_exits=sum(r["forced_exits"] for r in runs),
        re_entries=churn,
        held_share_of_wanted=(Decimal(covered) / Decimal(wanted) if wanted else None),
        out_of_sample_return=risk_card["oos_return"] if risk_card else None,
        annual_at_double_cost=risk_card["cagr_x2"] if risk_card else None,
        annual_at_triple_cost=risk_card["cagr_x3"] if risk_card else None,
        assets_positive=risk_card["assets_positive"] if risk_card else 0,
        top_asset_share=risk_card["top_asset_share"] if risk_card else None,
        single_year_share=risk_card["single_year_share"] if risk_card else None,
        years_positive_share=risk_card["years_positive_share"] if risk_card else None,
        signal_annual=signal_card["cagr"],
    )
    detail = {
        "key": variant.key,
        "family": variant.family.value,
        "strategy_id": variant.strategy_id,
        "params": dict(variant.params),
        "doubled": variant.doubled,
        "risk_v2": risk_card,
        "signal": signal_card,
        "exits_by_code": dict(codes),
        "longest_shutout_bars": shutout,
        "compatibility_ratio": compatibility_ratio(measured),
        "per_market": [{k: v for k, v in r.items() if k != "intervals"} for r in runs],
        "failures": failures,
        "seconds": round(sum(r["seconds"] for r in runs), 1),
    }
    return measured, detail


def _eligible(
    slots: dict[str, tuple[int | None, ...]],
    series: dict[str, tuple[MarketBar, ...]],
    grid: Sequence[datetime],
) -> tuple[frozenset[str], ...]:
    """Return, per grid slot, which markets have a bar there.

    Every market that exists at a slot is fundable: M39's universe is the six markets and the
    breadth is six, so there is no selection to make. A market simply is not there before it
    listed, which is the point-in-time part.
    """
    return tuple(
        frozenset(raw for raw in series if slots[raw][index] is not None)
        for index in range(len(grid))
    )


def verdict_of(worst_family: str) -> str:
    """Return whether the pre-declared prediction about the control held."""
    return "HELD" if worst_family == PREDICTED_WORST_COMPATIBILITY.value else "FAILED"


def main() -> int:
    """Screen every declared variant and print the table, then the shortlist."""
    series = {raw: load(raw) for raw in ASSETS_M39}
    grid = align(series)
    slots = positions(series, grid)
    panel = Panel(
        series=series,
        grid=grid,
        universe=_eligible(slots, series, grid),
        allocation=Allocation(universe_size=len(ASSETS_M39), sleeves=1),
    )
    sys.stdout.write(
        f"M39 screen -- {len(series)} markets, {TIMEFRAME.value}, grid {len(grid)} bars "
        f"{grid[0].date()} -> {grid[-1].date()}\n"
        f"Risk V2 live from the first run; the signal basis is a diagnostic\n\n"
    )

    header = (
        f"{'VAR':5} {'FAMILY':10} {'CAGR':>8} {'DD':>7} {'CALMAR':>7} {'PF':>5} {'TRADES':>7} "
        f"{'TURN':>7} {'FEES':>8} {'FORCED':>7} {'RE-ENT':>7} {'HELD%':>6} {'OOS':>8} "
        f"{'x2':>8} {'x3':>8} {'AST+':>5} {'TOPAST':>7} {'YR1':>6} {'YRS+':>5} {'SIG':>8} "
        f"{'RATIO':>6}"
    )
    sys.stdout.write(f"{header}\n{'-' * len(header)}\n")

    report: dict[str, Any] = {
        "milestone": "m39",
        "timeframe": TIMEFRAME.value,
        "universe": list(ASSETS_M39),
        "one_way_cost_basis_points": ONE_WAY_COST_BASIS_POINTS,
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "oos_start": OOS_START.isoformat(),
        "min_compatibility_ratio": MIN_COMPATIBILITY_RATIO,
        "predicted_worst_compatibility": PREDICTED_WORST_COMPATIBILITY.value,
        "generated_at": datetime.now(UTC).isoformat(),
        "variants": {},
    }

    shortlist: list[str] = []
    for variant in VARIANTS_M39:
        measured, detail = _one_variant(variant, panel)
        ok, failed = survives(measured)
        ratio = detail["compatibility_ratio"]
        sys.stdout.write(
            f"{variant.key:5} {variant.family.value:10} "
            f"{float(measured.annual or 0):+7.2%} {float(measured.max_drawdown):6.2%} "
            f"{float(measured.calmar_ratio or 0):7.2f} "
            f"{float(measured.profit_factor or 0):5.2f} {measured.trades:7d} "
            f"{float(measured.turnover):7.1f} {float(measured.fees):8.0f} "
            f"{measured.forced_exits:7d} {measured.re_entries:7d} "
            f"{float(measured.held_share_of_wanted or 0):6.1%} "
            f"{float(measured.out_of_sample_return or 0):+7.2%} "
            f"{float(measured.annual_at_double_cost or 0):+7.2%} "
            f"{float(measured.annual_at_triple_cost or 0):+7.2%} "
            f"{measured.assets_positive:5d} "
            f"{float(measured.top_asset_share or 0):6.1%} "
            f"{float(measured.single_year_share or 0):5.1%} "
            f"{float(measured.years_positive_share or 0):4.0%} "
            f"{float(measured.signal_annual or 0):+7.2%} "
            f"{'n/a' if ratio is None else f'{float(ratio):5.2f}':>6}\n"
        )
        sys.stdout.flush()
        detail["passes"] = ok
        detail["failed"] = [g.value for g in failed]
        report["variants"][variant.key] = detail
        if ok:
            shortlist.append(variant.key)

    sys.stdout.write("\nper-variant gate failures\n")
    for key, detail in report["variants"].items():
        if detail["failed"]:
            sys.stdout.write(f"  {key:5} {', '.join(detail['failed'])}\n")

    ratios = {
        k: d["compatibility_ratio"]
        for k, d in report["variants"].items()
        if d["compatibility_ratio"] is not None
    }
    if ratios:
        worst = min(ratios, key=lambda k: ratios[k])
        worst_family = report["variants"][worst]["family"]
        sys.stdout.write(
            f"\nworst compatibility ratio: {worst} ({worst_family}) at "
            f"{float(ratios[worst]):.2f}\n"
            f"predicted worst family: {PREDICTED_WORST_COMPATIBILITY.value} -> "
            f"prediction {verdict_of(worst_family)}\n"
        )
        report["worst_compatibility"] = {"key": worst, "family": worst_family}
        report["prediction_held"] = worst_family == PREDICTED_WORST_COMPATIBILITY.value

    report["shortlist"] = shortlist
    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / "screen_1d.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n-> {out.relative_to(ROOT)}\n")
    sys.stdout.write(
        f"SHORTLIST: {shortlist}\n"
        if shortlist
        else "NO-GO: no family passes 1D, so M39 stops here per the declaration\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
