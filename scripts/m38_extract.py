"""Extract the frozen Risk V3 candidate's positions over the full point-in-time universe.

Sixty pairs: thirty markets by two sleeves, the same set M36 ran Risk V2 over. Only the
candidate is extracted. Risk V2 on this universe is M36's own extraction -- the same
configuration object, which this script verifies by equality before doing any work rather than
trusting the claim -- and the signal basis is M35's cached timelines, so neither is re-run.

The engine is O(n squared) in history length, so this costs about three and three quarter hours
of CPU and runs on the declared ceiling of two workers. Results are written as each pair lands,
so an interruption resumes rather than restarts, and a single failed pair is recorded as a row
with a reason instead of taking the pool down with it.

Usage:
    uv run python scripts/m38_extract.py [--workers 2]
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
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.m36 import SLEEVES, TIMEFRAME
from quantplatform.research.m36_definitions import definition_for as m36_definition_for
from quantplatform.research.m36_definitions import market_ref
from quantplatform.research.m37_alternatives import ALTERNATIVES
from quantplatform.research.m37_definitions import alternative_definition_for, baseline_risk
from quantplatform.research.m37_probe import AblationRiskEngine
from quantplatform.research.m38 import CANDIDATE_KEY
from quantplatform.research.runner import ExperimentRunner
from quantplatform.strategies.research import build_research_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import load

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CACHE: Final[Path] = ROOT / "var/research/m38/risk_v3_4h.json"
V2_CACHE: Final[Path] = ROOT / "var/research/m36/positions_4h.json"


def _verify_v2_is_reusable() -> list[str]:
    """Return the markets M36 covered, having checked its risk configuration is the deployed one.

    M38 reads Risk V2 rather than re-running it, which is only legitimate if M36's runs used the
    configuration M38 calls Risk V2. That is checked here by building one M36 definition and
    comparing its risk object to the deployed baseline -- an equality, not a comment.

    Raises:
        ValueError: If the cached extraction was produced under a different configuration, or
            is incomplete. Either would make the comparison a comparison of two different
            things wearing one name.
    """
    cache = json.loads(V2_CACHE.read_text(encoding="utf-8"))
    rows = cache["pairs"]
    failed = [key for key, row in rows.items() if row["status"] != "succeeded"]
    if failed:
        msg = f"M36's extraction has {len(failed)} unsuccessful pairs: {failed[:5]}"
        raise ValueError(msg)
    markets = sorted({row["market"] for row in rows.values()})
    probe = next(iter(SLEEVES))
    sample = m36_definition_for(
        market_ref(markets[0], load(markets[0])),
        probe,
        Timeframe(TIMEFRAME.value),
        latching=False,
    )
    if sample.risk != baseline_risk(Timeframe(TIMEFRAME.value)):
        msg = (
            "M36's definitions do not carry the configuration M38 calls Risk V2, so its cached "
            "positions cannot stand in for a Risk V2 run"
        )
        raise ValueError(msg)
    return markets


def _one(sleeve: str, raw: str) -> dict[str, Any]:
    """Run the candidate for one pair and return its intervals and exit tally."""
    at = time.time()
    captured: list[AblationRiskEngine] = []
    try:
        bars = load(raw)
        spec = next(a for a in ALTERNATIVES if a.key == CANDIDATE_KEY)
        probe = next(p for p in SLEEVES if p.key == sleeve)
        definition = alternative_definition_for(
            market_ref(raw, bars), probe, Timeframe(TIMEFRAME.value), spec
        )

        def _engine(_: ExperimentDefinition) -> AblationRiskEngine:
            """Build the recording engine on the definition's own risk configuration."""
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
            definition, bars=bars, factory=factory, code_revision="m38-risk-v3"
        )
    except Exception as error:  # a failed pair is a result, not a crash
        return {
            "sleeve": sleeve,
            "market": raw,
            "status": "driver_error",
            "bars": 0,
            "seconds": round(time.time() - at, 1),
            "intervals": [],
            "trades": 0,
            "exits_by_code": {},
            "exits_by_stop_kind": {},
            "forced_exits": 0,
            "error": f"{type(error).__name__}: {error}",
        }
    tally = captured[-1].tally if captured else None
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
        "exits_by_code": dict(tally.by_code) if tally else {},
        "exits_by_stop_kind": dict(tally.by_stop_kind) if tally else {},
        "forced_exits": tally.forced_exits if tally else 0,
        "error": result.error,
    }


def _write(done: dict[str, Any], markets: list[str]) -> None:
    """Persist the cache after every landed pair."""
    CACHE.write_text(
        json.dumps(
            {
                "milestone": "m38",
                "timeframe": TIMEFRAME.value,
                "candidate": CANDIDATE_KEY,
                "basis": "certified BacktestEngine, M37's frozen Risk V3 candidate",
                "universe": markets,
                "pairs": done,
                "generated_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """Extract the candidate over every pair M36 covered, and cache what it did."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    markets = _verify_v2_is_reusable()
    sys.stdout.write(
        f"M36's Risk V2 extraction verified reusable: {len(markets)} markets, "
        f"{len(markets) * len(SLEEVES)} pairs\n"
    )
    runs = [(probe.key, raw) for probe in SLEEVES for raw in markets]

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = (
        json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    )
    done: dict[str, Any] = dict(existing.get("pairs", {}))
    todo = [run for run in runs if f"{run[0]}|{run[1]}" not in done]
    sys.stdout.write(f"{len(done)} already cached, {len(todo)} to run\n\n")
    sys.stdout.flush()

    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_one, *run): run for run in todo}
        for index, future in enumerate(as_completed(futures), start=1):
            sleeve, raw = futures[future]
            row = future.result()
            done[f"{sleeve}|{raw}"] = row
            sys.stdout.write(
                f"  [{index:3d}/{len(todo)}] {sleeve:3} {raw:11} {row['status']:12} "
                f"{row['trades']:4d} trades {row['forced_exits']:4d} forced "
                f"({row['seconds']:5.0f}s)\n"
            )
            sys.stdout.flush()
            _write(done, markets)

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
