"""Run every M37 ablation variant through the certified engine and cache what each one did.

Nine variants over two sleeves and six markets: a hundred and eight runs. For each one this
records the position intervals the engine actually occupied, how many trades it took, and --
through :class:`~quantplatform.research.m37_probe.AblationRiskEngine` -- what ended every
position, keyed both by the risk check that triggered it and by the kind of stop the position
was carrying at the time. That second breakdown is what separates the initial stop, the
break-even move and the trailing stop, which all three report the same exit reason.

The engine is O(n squared) in history length, so this costs about eight and a half hours of CPU
and runs on the declared ceiling of two workers. Results are written as each one lands, keyed by
variant, sleeve and market, so an interruption resumes rather than restarts and a single failed
pair is recorded as a row with a reason instead of taking the pool down with it.

Usage:
    uv run python scripts/m37_ablate.py [--workers 2] [--only BASE]
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
from quantplatform.research.m36_definitions import market_ref
from quantplatform.research.m37 import ABLATIONS, ASSETS_M37
from quantplatform.research.m37_alternatives import ALTERNATIVES
from quantplatform.research.m37_definitions import alternative_definition_for, definition_for
from quantplatform.research.m37_probe import AblationRiskEngine
from quantplatform.research.runner import ExperimentRunner
from quantplatform.strategies.research import build_research_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m35_masks import load

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CACHE: Final[Path] = ROOT / "var/research/m37/ablation_4h.json"


def _one(variant: str, sleeve: str, raw: str) -> dict[str, Any]:
    """Run one variant on one sleeve-market pair and return its intervals and exit tally.

    Every failure is caught and returned as data, for the reason M36 learned the hard way: an
    exception leaving a worker is raised in the parent by ``as_completed``, and the other
    worker's finished work goes with it.
    """
    at = time.time()
    captured: list[AblationRiskEngine] = []
    try:
        bars = load(raw)
        probe = next(p for p in SLEEVES if p.key == sleeve)
        ref = market_ref(raw, bars)
        timeframe = Timeframe(TIMEFRAME.value)
        # A phase-2 alternative is extracted by the same code path as an ablation, so the two
        # cannot differ in how they were measured -- only in the configuration they ran under.
        spec = next((a for a in ALTERNATIVES if a.key == variant), None)
        definition = (
            alternative_definition_for(ref, probe, timeframe, spec)
            if spec is not None
            else definition_for(
                ref, probe, timeframe, next(a for a in ABLATIONS if a.key == variant)
            )
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
            definition, bars=bars, factory=factory, code_revision="m37-ablation"
        )
    except Exception as error:  # a failed pair is a result, not a crash
        return {
            "variant": variant,
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
        "variant": variant,
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


def _write(done: dict[str, Any]) -> None:
    """Persist the cache after every landed pair, so progress survives an interruption."""
    CACHE.write_text(
        json.dumps(
            {
                "milestone": "m37",
                "timeframe": TIMEFRAME.value,
                "universe": list(ASSETS_M37),
                "variants": [a.key for a in ABLATIONS] + [a.key for a in ALTERNATIVES],
                "basis": "certified BacktestEngine, one mechanism ablated per variant",
                "pairs": done,
                "generated_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """Run every declared variant over every pair and cache what each did."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--only", default="", help="restrict to one variant, e.g. BASE")
    args = parser.parse_args()

    declared = [a.key for a in ABLATIONS] + [a.key for a in ALTERNATIVES]
    variants = [key for key in declared if not args.only or key == args.only]
    if args.only and not variants:
        sys.stdout.write(f"no declared variant named {args.only!r}; known: {declared}\n")
        return 1
    runs = [
        (variant, probe.key, raw) for variant in variants for probe in SLEEVES for raw in ASSETS_M37
    ]
    sys.stdout.write(
        f"{len(variants)} variants x {len(SLEEVES)} sleeves x {len(ASSETS_M37)} markets "
        f"= {len(runs)} runs on {args.workers} workers\n"
    )

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = (
        json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    )
    done: dict[str, Any] = dict(existing.get("pairs", {}))
    todo = [r for r in runs if f"{r[0]}|{r[1]}|{r[2]}" not in done]
    sys.stdout.write(f"{len(done)} already cached, {len(todo)} to run\n\n")
    sys.stdout.flush()

    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_one, *run): run for run in todo}
        for index, future in enumerate(as_completed(futures), start=1):
            variant, sleeve, raw = futures[future]
            row = future.result()
            done[f"{variant}|{sleeve}|{raw}"] = row
            sys.stdout.write(
                f"  [{index:4d}/{len(todo)}] {variant:5} {sleeve:3} {raw:9} "
                f"{row['status']:12} {row['trades']:4d} trades "
                f"{row['forced_exits']:4d} forced  ({row['seconds']:5.0f}s)\n"
            )
            sys.stdout.flush()
            _write(done)

    failed = [key for key, row in done.items() if row["status"] != "succeeded"]
    sys.stdout.write(
        f"\n{len(done)} runs cached in {time.time() - started:.0f}s -> {CACHE.relative_to(ROOT)}\n"
    )
    if failed:
        sys.stdout.write(f"FAILED: {failed}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
