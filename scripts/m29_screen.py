"""M29 phase 1 at 1d: run every declared configuration and record what the gates need.

One worker, by instruction and by arithmetic — a daily run costs about five seconds, so the
whole screen fits in the time a 4h screen would spend on two cells.

Five passes per configuration, all of them declared in ``research/m29.py`` before any of this
ran. Nothing here decides anything: it measures, and :func:`~quantplatform.research.m29.survives`
judges afterwards from the JSON this writes.

    base        full history, research risk — the rule measured rather than the breakers
    cost x2     the declared cost stress, and the one that matters most
    cost x3     reported so the distance to the cliff is visible rather than binary
    oos         2024-01-01 onward, M23's window, reused unchanged
    deployed    latching Risk V2, which no PAPER CANDIDATE can be had without

Usage:
    uv run python scripts/m29_screen.py [--only BTCUSDT,ETHUSDT]
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
from quantplatform.research.m29 import (
    ASSETS,
    CANDIDATES_M29,
    COST_STRESS_MULTIPLIERS,
    Probe,
    cagr,
    calmar,
    definition_for,
)
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import Scorecard
from quantplatform.strategies.research import build_research_registry

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DATA: Final[Path] = ROOT / "data/raw/m29/out"
HOME: Final[Path] = ROOT / "var/research/m29"
TIMEFRAME: Final[Timeframe] = Timeframe.D1
MIN_OOS_BARS: Final[int] = 30
"""Below this the out-of-sample window is too short for any figure from it to mean much."""

OOS_START: Final[datetime] = datetime(2024, 1, 1, tzinfo=UTC)
"""M23's out-of-sample boundary, reused unchanged. Redrawing it for a new milestone would
make every prior verdict incomparable and hand this one a window chosen after the fact."""


def load(raw: str) -> tuple[MarketBar, ...]:
    """Read one market's derived daily series, re-validating every row through the model."""
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
                    source="m29_daily",
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

    Fee and slippage are fields of the execution policy the risk configuration carries, so
    the whole policy is rebuilt rather than patched — ``RiskConfiguration`` validates its own
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


def card(result: ExperimentResult | None) -> dict[str, Any] | None:
    """Return one run's scorecard, plus the annualised figures the gates read."""
    if result is None or result.performance is None:
        return None
    out: dict[str, Any] = Scorecard.from_performance(result.performance).model_dump()
    bars = result.performance.bars_processed if hasattr(result.performance, "bars_processed") else 0
    out["bars"] = bars or len(result.equity_curve)
    annual = cagr(Decimal(str(out["total_return"])), bars=out["bars"], timeframe=TIMEFRAME)
    out["cagr"] = annual
    out["calmar"] = calmar(annual, Decimal(str(out["max_drawdown"])))
    out["exposure"] = out.get("time_in_market")
    return out


def per_year(result: ExperimentResult | None) -> list[dict[str, Any]]:
    """Return each calendar year's return and trade count, cut from the continuous run."""
    if result is None or result.performance is None:
        return []
    opening = result.performance.initial_equity
    out: list[dict[str, Any]] = []
    for year in sorted({point.at.year for point in result.equity_curve}):
        points = [p for p in result.equity_curve if p.at.year == year]
        closing = points[-1].equity if points else opening
        out.append(
            {
                "year": year,
                "return": (closing / opening - 1) if opening else None,
                "trades": sum(1 for t in result.trades if t.closed_at.year == year),
            }
        )
        opening = closing
    return out


def run(definition: ExperimentDefinition, bars: tuple[MarketBar, ...]) -> ExperimentResult | None:
    """Execute one experiment, returning the result or ``None`` if it failed."""
    result = ExperimentRunner().run(
        definition, bars=bars, factory=_factory(), code_revision="m29-screen-1d"
    )
    return result if result.status.value == "succeeded" else None


def cell(raw: str, probe: Probe, bars: tuple[MarketBar, ...]) -> dict[str, Any]:
    """Run every declared pass for one configuration on one market."""
    base_def = definition_for(raw, probe, TIMEFRAME, latching=False)
    entry: dict[str, Any] = {
        "asset": raw,
        "key": probe.key,
        "family": probe.family.value,
        "horizon": probe.horizon.value,
        "strategy_id": probe.candidate.strategy_id,
        "params": dict(probe.candidate.params),
        "experiment_id": base_def.experiment_id,
    }

    result = run(base_def, bars)
    entry["base"] = card(result)
    entry["per_year"] = per_year(result)

    for multiplier in COST_STRESS_MULTIPLIERS:
        entry[f"cost_x{multiplier}"] = card(run(at_cost(base_def, multiplier), bars))

    oos = tuple(bar for bar in bars if bar.open_time >= OOS_START)
    entry["oos"] = card(run(base_def, oos)) if len(oos) > MIN_OOS_BARS else None

    entry["deployed"] = card(run(definition_for(raw, probe, TIMEFRAME, latching=True), bars))
    return entry


def main() -> int:
    """Run the whole 1d screen, one cell at a time, and write one report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    assets = [raw for raw in ASSETS if not wanted or raw in wanted]

    HOME.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cells: list[dict[str, Any]] = []
    total = len(assets) * len(CANDIDATES_M29)
    done = 0

    for raw in assets:
        bars = load(raw)
        sys.stdout.write(f"{raw}: {len(bars)} daily bars\n")
        sys.stdout.flush()
        for probe in CANDIDATES_M29:
            at = time.time()
            entry = cell(raw, probe, bars)
            done += 1
            base = entry["base"]
            summary = (
                f"ret {float(base['total_return']):+7.2%} dd {float(base['max_drawdown']):6.2%} "
                f"trades {base['trades']:4d} "
                f"calmar {float(base['calmar']):5.2f}"
                if base and base.get("calmar") is not None
                else "no result"
                if base is None
                else f"ret {float(base['total_return']):+7.2%} trades {base['trades']:4d}"
            )
            sys.stdout.write(
                f"  [{done:3d}/{total}] {raw:8} {entry['key']:3} {entry['family']:15} "
                f"{summary}  ({time.time() - at:.1f}s)\n"
            )
            sys.stdout.flush()
            cells.append(entry)

    report = {
        "milestone": "m29",
        "phase": "screen",
        "timeframe": TIMEFRAME.value,
        "oos_start": OOS_START.isoformat(),
        "cost_stress_multipliers": list(COST_STRESS_MULTIPLIERS),
        "generated_at": datetime.now(UTC).isoformat(),
        "seconds": round(time.time() - started, 1),
        "cells": cells,
    }
    out = HOME / "screen_1d.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n{len(cells)} cells in {report['seconds']}s -> {out.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
