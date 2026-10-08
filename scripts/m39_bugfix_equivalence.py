"""Show the stop-bound fix changes no existing result except a configuration at the bound.

The fix widens one comparison by at most one tick's worth of basis points, so by construction it
can only alter a run whose realised stop distance landed in that sliver beyond the configured
maximum. Every configuration this project has measured sits far inside: Risk V2 derives a 600
bps stop against a 2 000 bps maximum, and M37's candidate a 1 200 bps one. The argument is sound
but it is still an argument, so this re-runs real pairs under both configurations and compares
the engine's own trade counts against the caches produced before the fix.

Markets are chosen for cost, not for outcome: the three shortest series in the pool, both
sleeves, both configurations -- twelve runs rather than a hundred and twenty.

Usage:
    uv run python scripts/m39_bugfix_equivalence.py
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import Timeframe
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.m36 import SLEEVES, TIMEFRAME
from quantplatform.research.m36_definitions import definition_for as v2_definition_for
from quantplatform.research.m36_definitions import market_ref
from quantplatform.research.m37_alternatives import ALTERNATIVES
from quantplatform.research.m37_definitions import alternative_definition_for
from quantplatform.research.m38 import CANDIDATE_KEY
from quantplatform.research.runner import ExperimentRunner
from quantplatform.strategies.research import build_research_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import load

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
V2_CACHE: Final[Path] = ROOT / "var/research/m36/positions_4h.json"
V3_CACHE: Final[Path] = ROOT / "var/research/m38/risk_v3_4h.json"
MARKETS: Final[tuple[str, ...]] = ("LUNAUSDT", "MATICUSDT", "SOLUSDT")
"""The three shortest series in the pool. Chosen for runtime, before any result was seen."""


def _run(config: str, sleeve: str, raw: str) -> dict[str, Any]:
    """Re-run one pair under one configuration and report its trade count."""
    at = time.time()
    bars = load(raw)
    probe = next(p for p in SLEEVES if p.key == sleeve)
    ref = market_ref(raw, bars)
    timeframe = Timeframe(TIMEFRAME.value)
    definition = (
        v2_definition_for(ref, probe, timeframe, latching=False)
        if config == "v2"
        else alternative_definition_for(
            ref, probe, timeframe, next(a for a in ALTERNATIVES if a.key == CANDIDATE_KEY)
        )
    )
    factory = ExperimentEngineFactory(
        registry=build_research_registry(), features_for=features_for, quote_asset="USDT"
    )
    result = ExperimentRunner().run(
        definition, bars=bars, factory=factory, code_revision="m39-equivalence"
    )
    return {
        "config": config,
        "sleeve": sleeve,
        "market": raw,
        "trades": len(result.trades),
        "status": result.status.value,
        "seconds": round(time.time() - at, 1),
    }


def main() -> int:
    """Re-run the sample and compare every count against the pre-fix caches."""
    cached = {
        "v2": json.loads(V2_CACHE.read_text(encoding="utf-8"))["pairs"],
        "v3": json.loads(V3_CACHE.read_text(encoding="utf-8"))["pairs"],
    }
    runs = [
        (config, probe.key, raw) for config in ("v2", "v3") for probe in SLEEVES for raw in MARKETS
    ]
    sys.stdout.write(f"re-running {len(runs)} pairs against caches produced before the fix\n\n")

    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_run, *run): run for run in runs}
        for future in as_completed(futures):
            rows.append(future.result())

    same = differing = missing = 0
    for row in sorted(rows, key=lambda r: (r["config"], r["sleeve"], r["market"])):
        key = f"{row['sleeve']}|{row['market']}"
        before = cached[row["config"]].get(key)
        if before is None:
            verdict, missing = "NOT CACHED", missing + 1
        elif before["trades"] == row["trades"]:
            verdict, same = "identical", same + 1
        else:
            verdict, differing = f"DIFFERS (was {before['trades']})", differing + 1
        sys.stdout.write(
            f"  {row['config']:3} {row['sleeve']:3} {row['market']:10} "
            f"{row['trades']:4d} trades  {verdict}\n"
        )

    sys.stdout.write(f"\n{same} identical, {differing} differing, {missing} not cached\n")
    if differing:
        sys.stdout.write(
            "THE FIX CHANGED AN EXISTING RESULT: it was supposed to change only a "
            "configuration whose realised distance sat beyond its configured maximum\n"
        )
        return 1
    sys.stdout.write(
        "No existing result changed. The fix alters acceptance only where the realised "
        "distance exceeded the configured maximum by at most one tick, which no measured "
        "configuration does: Risk V2 stops at 600 bps and the M37 candidate at 1 200, "
        "against a 2 000 bps maximum.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
