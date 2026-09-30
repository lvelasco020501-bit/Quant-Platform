"""Hold M34's signal driver to the certified engine's own trade list.

The portfolio in M34 is built from signal timelines produced by
:mod:`~quantplatform.research.sleeve`, not from engine runs, because sixty full 4h engine runs is
5.6 hours of O(n squared) bookkeeping. That trade is only acceptable if the driver demonstrably
says the same thing the engine says, so this script makes it demonstrate that.

The comparison is over a truncated window across every incumbent market and both frozen sleeves,
which is 8k bars a run instead of 20k -- the engine's cost falls sixfold and the check still
covers twelve independent series. One full-length run is done as well, to confirm the agreement
does not decay with length.

**What is compared, and why it is not a timestamp match.** The first version of this script
compared entry timestamps one-for-one and found them wildly apart -- and it was right to fail,
because the two produce different *numbers* of trades: B2 on SOL gives the driver 21 long
stretches and the engine 89 round trips. The cause is Risk V2's stop. It takes the engine flat
part-way through a stretch the strategy still wants to be long in, and the strategy then
re-enters on a later breakout inside that same stretch.

So the check is **containment**: every trade the engine opens must fall inside one of the
driver's long stretches. If it does, the two agree about when the rule speaks and differ only in
what risk management does about it -- which is exactly the declared gap. If a single engine trade
opened outside every driver stretch, the driver would be reading a signal the engine never saw,
and that would invalidate it.

Usage:
    uv run python scripts/m34_verify.py [--bars 8000] [--full]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.m29 import definition_for
from quantplatform.research.m34 import SLEEVES, TIMEFRAME
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sleeve import long_intervals
from quantplatform.strategies.research import build_research_registry

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m34"
DATA: Final[Path] = ROOT / "data/raw/m16/out"
MARKETS: Final[tuple[str, ...]] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "XRPUSDT",
)


def load(raw: str) -> tuple[MarketBar, ...]:
    """Read one incumbent market's validated 4h series."""
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
                    quote_volume=None,
                    trade_count=None,
                    source="m34_verify",
                    is_closed=True,
                )
            )
    return tuple(bars)


def compare(raw: str, key: str, bars: tuple[MarketBar, ...]) -> dict[str, Any]:
    """Run both paths over the same bars and report how far apart they are."""
    probe = next(p for p in SLEEVES if p.key == key)
    registry = build_research_registry()
    strategy = registry.create(probe.candidate.strategy_id, dict(probe.candidate.params))

    at = time.time()
    intervals = long_intervals(strategy, bars, pipelines=features_for)
    driver_seconds = time.time() - at

    definition = definition_for(raw, probe, TIMEFRAME, latching=False)
    factory = ExperimentEngineFactory(
        registry=registry, features_for=features_for, quote_asset="USDT"
    )
    at = time.time()
    result = ExperimentRunner().run(
        definition, bars=bars, factory=factory, code_revision="m34-verify"
    )
    engine_seconds = time.time() - at

    last = bars[-1].open_time
    contained = 0
    for trade in result.trades:
        for span in intervals:
            end = span.exited_at or last
            if span.entered_at <= trade.opened_at <= end:
                contained += 1
                break
    return {
        "asset": raw,
        "sleeve": key,
        "bars": len(bars),
        "driver_stretches": len(intervals),
        "engine_trades": len(result.trades),
        "engine_trades_contained": contained,
        "all_contained": contained == len(result.trades),
        "stop_multiplier": (round(len(result.trades) / len(intervals), 2) if intervals else None),
        "driver_seconds": round(driver_seconds, 1),
        "engine_seconds": round(engine_seconds, 1),
        "speedup": round(engine_seconds / driver_seconds, 1) if driver_seconds else None,
    }


def main() -> int:
    """Compare both paths across every incumbent and both sleeves."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=8000)
    parser.add_argument("--full", action="store_true", help="also compare one full-length series")
    args = parser.parse_args()

    HOME.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for key in (probe.key for probe in SLEEVES):
        for raw in MARKETS:
            bars = load(raw)[: args.bars]
            row = compare(raw, key, bars)
            rows.append(row)
            sys.stdout.write(
                f"  {key:3} {raw:9} {row['bars']:5d} bars  driver {row['driver_stretches']:3d} "
                f"stretches | engine {row['engine_trades']:3d} trades  "
                f"{row['engine_trades_contained']:3d} contained "
                f"{'OK' if row['all_contained'] else 'OUTSIDE!'}  "
                f"stop x{row['stop_multiplier']}  "
                f"{row['driver_seconds']:5.1f}s vs {row['engine_seconds']:6.1f}s "
                f"(x{row['speedup']})\n"
            )
            sys.stdout.flush()

    if args.full:
        bars = load("BTCUSDT")
        row = compare("BTCUSDT", "B2", bars)
        row["full_length"] = True
        rows.append(row)
        sys.stdout.write(
            f"  full BTCUSDT B2 {row['bars']} bars  driver {row['driver_stretches']} stretches | "
            f"engine {row['engine_trades']} trades  {row['engine_trades_contained']} contained  "
            f"{row['driver_seconds']}s vs {row['engine_seconds']}s (x{row['speedup']})\n"
        )

    agree = all(row["all_contained"] for row in rows)
    report = {
        "milestone": "m34",
        "phase": "driver_verification",
        "timeframe": TIMEFRAME.value,
        "rows": rows,
        "every_engine_trade_inside_a_driver_stretch": agree,
        "median_stop_multiplier": sorted(
            row["stop_multiplier"] for row in rows if row["stop_multiplier"]
        )[len(rows) // 2],
    }
    out = HOME / "verification.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"\n-> {out.relative_to(ROOT)}\n")
    sys.stdout.write(
        "every engine trade falls inside a driver stretch: the two agree on the signals\n"
        if agree
        else "DISAGREEMENT: an engine trade opened outside every driver stretch\n"
    )
    return 0 if agree else 1


if __name__ == "__main__":
    raise SystemExit(main())
