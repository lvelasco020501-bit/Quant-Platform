"""Compute the frozen sleeves' signal timelines once and cache them for M35 to reuse.

The timelines depend only on a strategy and a market's bars -- not on breadth, allocation, cost
or window -- so recomputing them for every M35 pass would be 384 seconds of identical work each
time. They are written as one character per bar so the cache stays small enough to read and
diff.

Usage:
    uv run python scripts/m35_masks.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.research.m32 import ASSETS_M30
from quantplatform.research.m34 import SLEEVES, TIMEFRAME, pool_symbols
from quantplatform.research.rotation import align
from quantplatform.research.sleeve import long_intervals, long_mask
from quantplatform.strategies.research import build_research_registry

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CACHE: Final[Path] = ROOT / "var/research/m35/masks_4h.json"


def load(raw: str) -> tuple[MarketBar, ...]:
    """Read one market's canonical 4h series, re-validating every row through the model."""
    folder = "data/raw/m16/out" if raw in ASSETS_M30 else "data/raw/m32/out"
    path = next((ROOT / folder).glob(f"{raw}_{TIMEFRAME.value}_*.csv"))
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
                    source="m35",
                    is_closed=True,
                )
            )
    return tuple(bars)


def main() -> int:
    """Write one cache of every sleeve's timeline over every pool market."""
    symbols = list(pool_symbols())
    series = {raw: load(raw) for raw in symbols}
    grid = align(series)
    registry = build_research_registry()
    started = time.time()
    packed: dict[str, str] = {}
    stretches: dict[str, int] = {}
    for probe in SLEEVES:
        strategy = registry.create(probe.candidate.strategy_id, dict(probe.candidate.params))
        for raw in symbols:
            intervals = long_intervals(strategy, series[raw], pipelines=features_for)
            mask = long_mask(intervals, grid)
            key = f"{probe.key}|{raw}"
            packed[key] = "".join("1" if held else "0" for held in mask)
            stretches[key] = len(intervals)
            sys.stdout.write(f"  {key:16} {len(intervals):4d} stretches\n")
            sys.stdout.flush()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps(
            {
                "timeframe": TIMEFRAME.value,
                "grid": [stamp.isoformat() for stamp in grid],
                "sleeves": [probe.key for probe in SLEEVES],
                "markets": symbols,
                "stretches": stretches,
                "masks": packed,
                "seconds": round(time.time() - started, 1),
                "generated_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(
        f"\n{len(packed)} timelines in {time.time() - started:.0f}s -> {CACHE.relative_to(ROOT)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
