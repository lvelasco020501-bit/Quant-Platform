"""Extract each sleeve-market pair's real position intervals from the certified engine.

This is what makes M36 different from M30 through M35. Those built portfolios on timelines that
no stop had ever touched; this runs ``BacktestEngine`` with Risk V2 live -- stops set and
triggered, position state, the re-entries that state produces, order rejection and the
non-latching drawdown breakers -- and takes the intervals the engine's *positions* actually
occupied.

The engine is O(n squared) in history length, measured at 3.5s, 13.7s and 55.4s for 2k, 4k and 8k
bars, so a full 4h series costs about 220 seconds and fifty-four of them about three and a quarter
hours. Two workers, which is the declared ceiling for this machine. Results are written as each
one lands so the cache survives an interruption and progress can be read while it runs.

Usage:
    uv run python scripts/m36_positions.py [--workers 2] [--only B2]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import Timeframe
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.m32 import LIQUIDITY_WINDOW, UNIVERSE_SIZE
from quantplatform.research.m36 import SLEEVES, TIMEFRAME, pool_symbols
from quantplatform.research.m36_definitions import definition_for, market_ref
from quantplatform.research.portfolio import positions as slot_index
from quantplatform.research.rotation import align, eligible_universe
from quantplatform.research.runner import ExperimentRunner
from quantplatform.strategies.research import build_research_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import load

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CACHE: Final[Path] = ROOT / "var/research/m36/positions_4h.json"


def _one(sleeve: str, raw: str) -> dict[str, Any]:
    """Run the certified engine for one pair and return its real position intervals.

    Every failure is caught and returned as data. The first attempt at this phase let one pair's
    exception propagate out of the worker, and ``as_completed`` then raised it in the parent
    while the other worker kept computing to completion -- twenty-two minutes of real work
    discarded because a different pair could not resolve. A long unattended run must degrade to
    a partial result with a reason attached, not to nothing.
    """
    at = time.time()
    try:
        bars = load(raw)
        probe = next(p for p in SLEEVES if p.key == sleeve)
        registry = build_research_registry()
        factory = ExperimentEngineFactory(
            registry=registry, features_for=features_for, quote_asset="USDT"
        )
        result = ExperimentRunner().run(
            definition_for(
                market_ref(raw, bars), probe, Timeframe(TIMEFRAME.value), latching=False
            ),
            bars=bars,
            factory=factory,
            code_revision="m36-positions",
        )
    except Exception as error:
        return {
            "sleeve": sleeve,
            "market": raw,
            "status": "driver_error",
            "bars": 0,
            "seconds": round(time.time() - at, 1),
            "intervals": [],
            "trades": 0,
            "error": f"{type(error).__name__}: {error}",
        }
    return {
        "sleeve": sleeve,
        "market": raw,
        "status": result.status.value,
        "bars": len(bars),
        "seconds": round(time.time() - at, 1),
        "intervals": [
            {"opened_at": trade.opened_at.isoformat(), "closed_at": trade.closed_at.isoformat()}
            for trade in sorted(result.trades, key=lambda t: t.opened_at)
        ],
        "trades": len(result.trades),
        "error": result.error,
    }


def main() -> int:
    """Run every declared pair through the engine and cache the intervals."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--only", default="", help="restrict to one sleeve, e.g. B2")
    args = parser.parse_args()

    symbols = list(pool_symbols())
    series = {raw: load(raw) for raw in symbols}
    grid = align(series)
    universe = eligible_universe(
        series, slot_index(series, grid), window=LIQUIDITY_WINDOW, size=UNIVERSE_SIZE
    )
    eligible = sorted({asset for slot in universe for asset in slot})
    sleeves = [p.key for p in SLEEVES if not args.only or p.key == args.only]
    pairs = [(sleeve, raw) for sleeve in sleeves for raw in eligible]
    sys.stdout.write(
        f"{len(eligible)} markets ever eligible at breadth {UNIVERSE_SIZE}; "
        f"{len(pairs)} pairs on {args.workers} workers\n"
    )
    sys.stdout.flush()

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = (
        json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    )
    done: dict[str, Any] = dict(existing.get("pairs", {}))
    todo = [(s, r) for s, r in pairs if f"{s}|{r}" not in done]
    sys.stdout.write(f"{len(done)} already cached, {len(todo)} to run\n\n")

    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_one, sleeve, raw): (sleeve, raw) for sleeve, raw in todo}
        for index, future in enumerate(as_completed(futures), start=1):
            sleeve, raw = futures[future]
            row = future.result()
            done[f"{sleeve}|{raw}"] = row
            sys.stdout.write(
                f"  [{index:3d}/{len(todo)}] {sleeve:3} {raw:11} {row['status']:10} "
                f"{row['trades']:4d} trades  ({row['seconds']:5.0f}s)\n"
            )
            sys.stdout.flush()
            CACHE.write_text(
                json.dumps(
                    {
                        "milestone": "m36",
                        "timeframe": TIMEFRAME.value,
                        "basis": "certified BacktestEngine, Risk V2 non-latching reference",
                        "eligible": eligible,
                        "grid": [stamp.isoformat() for stamp in grid],
                        "pairs": done,
                        "generated_at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    failed = [key for key, row in done.items() if row["status"] != "succeeded"]
    sys.stdout.write(
        f"\n{len(done)} pairs cached in {time.time() - started:.0f}s -> {CACHE.relative_to(ROOT)}\n"
    )
    if failed:
        sys.stdout.write(f"FAILED: {failed}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
