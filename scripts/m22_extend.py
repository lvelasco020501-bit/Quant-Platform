"""Run M22 phase two: the shortlist only, on four markets, with costs stressed and Risk V2 on.

Phase one asked which families are worth compute. This asks whether the ones that survived
hold up where it matters, and it runs **only** the configurations phase one carried forward —
which is the whole reason phase one exists.

Four things per surviving configuration:

* ``cross``    — BNB and SOL over their own histories, same parameters, nothing refitted.
* ``stress``   — the three declared cost scenarios, on every market it is run on.
* ``deployed`` — the same run under deployed Risk V2, latching breakers included.
                 :func:`quantplatform.research.sprint.judge` refuses PAPER CANDIDATE without it.
* ``bench``    — both benchmarks and the incumbent on the new markets, so "better than what we
                 have" stays a like-for-like comparison there too.

The verdict is then :func:`~quantplatform.research.sprint.judge` on assembled evidence, per
market. It is a mechanical function of thresholds fixed in M13; nothing here may soften it,
and a milestone that produces no PAPER CANDIDATE reports that rather than lowering a bar.

Usage::

    uv run python scripts/m22_extend.py --keys T1,M2 [--workers 2] [--jobs cross,stress]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path
from typing import Any

from m22_screen import (
    _factory,
    _s,
    _screen,
    _walk_forward,
    card,
    holding,
    load,
    per_year,
)

from quantplatform.orchestration.research import code_revision
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.m16 import DATA_END
from quantplatform.research.m22 import (
    CANDIDATES_M22,
    EXTENSION_ASSETS,
    REFERENCES,
    SCREEN_ASSETS,
    Variant,
    asset_for,
    deployed_definition,
    reference_definition,
    study_definition,
)
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import (
    STRESS_LABELS,
    Evidence,
    Scorecard,
    judge,
    stress_scenarios,
)
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import derive_stress_definition

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m22"
ALL_MARKETS: tuple[str, ...] = (*SCREEN_ASSETS, *EXTENSION_ASSETS)


def _home(market: str, key: str, job: str) -> Path:
    home = OUT / market / key / job
    home.mkdir(parents=True, exist_ok=True)
    return home


def _variant(key: str) -> Variant:
    return next(v for v in CANDIDATES_M22 if v.key == key)


def _run(definition: ExperimentDefinition, market: str, revision: str | None) -> dict[str, Any]:
    result = ExperimentRunner().run(
        definition, bars=load(market), factory=_factory(), code_revision=revision
    )
    return {
        "experiment_id": definition.experiment_id,
        "name": definition.name,
        "status": result.status.value,
        "error": result.error,
        "card": card(result),
        "holding": holding(result),
        "per_year": per_year(result),
        "_result": result,
    }


def _finish(home: Path, evidence: dict[str, Any], started: float) -> dict[str, Any]:
    evidence["seconds"] = round(time.time() - started, 1)
    stripped = {k: v for k, v in evidence.items() if not k.startswith("_")}
    (home / "evidence.json").write_text(json.dumps(_s(stripped), indent=2), encoding="utf-8")
    return stripped


def job_cross(market: str, key: str) -> dict[str, Any]:
    """One surviving configuration on one extension market: full history, then walk-forward."""
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, key, "cross")
    definition = study_definition(market, _variant(key))
    entry = _run(definition, market, revision)
    ExperimentLedger(home / "ledger.jsonl").record(
        entry.pop("_result"), store=ResultStore(home / "results")
    )
    evidence = {
        "milestone": "m22",
        "phase": "cross",
        "market": market,
        "key": key,
        "run": entry,
        "walk_forward": _walk_forward(definition, asset_for(market), home, revision),
        "shows_signal": _screen(entry),
    }
    return _finish(home, evidence, started)


def job_stress(market: str, key: str) -> dict[str, Any]:
    """The three declared cost scenarios. Only costs move; the rule and the window do not."""
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, key, "stress")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    reference = study_definition(market, _variant(key))
    entries = []
    for label, scenario in zip(STRESS_LABELS, stress_scenarios(reference), strict=True):
        definition = derive_stress_definition(reference, scenario)
        entry = _run(definition, market, revision)
        ledger.record(
            entry.pop("_result"),
            store=store,
            derived_from=reference.experiment_id,
            variation_kind=VariationKind.STRESS,
        )
        entries.append({"label": label, **entry})
    return _finish(
        home,
        {"milestone": "m22", "phase": "stress", "market": market, "key": key, "stress": entries},
        started,
    )


def job_deployed(market: str, key: str) -> dict[str, Any]:
    """The same configuration under deployed Risk V2, latching breakers and all."""
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, key, "deployed")
    definition = deployed_definition(market, _variant(key))
    entry = _run(definition, market, revision)
    ExperimentLedger(home / "ledger.jsonl").record(
        entry.pop("_result"), store=ResultStore(home / "results")
    )
    return _finish(
        home,
        {"milestone": "m22", "phase": "deployed", "market": market, "key": key, "run": entry},
        started,
    )


def job_bench(market: str, key: str) -> dict[str, Any]:
    """Both benchmarks and the incumbent on an extension market. ``key`` is ignored."""
    del key
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, "_references", "bench")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    runs = {}
    for reference in REFERENCES:
        entry = _run(reference_definition(market, reference), market, revision)
        ledger.record(entry.pop("_result"), store=store)
        runs[reference.strategy_id] = entry
    return _finish(
        home, {"milestone": "m22", "phase": "bench", "market": market, "runs": runs}, started
    )


JOBS: dict[str, Callable[[str, str], dict[str, Any]]] = {
    "cross": job_cross,
    "stress": job_stress,
    "deployed": job_deployed,
    "bench": job_bench,
}


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run the requested jobs for the shortlist, longest history first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", required=True, help="Comma-separated surviving configurations.")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--jobs", default="cross,stress,deployed,bench")
    parser.add_argument("--markets", default=",".join(ALL_MARKETS))
    args = parser.parse_args()

    keys = [k for k in args.keys.split(",") if k]
    kinds = [k for k in args.jobs.split(",") if k]
    markets = [m for m in args.markets.split(",") if m]

    planned: list[tuple[float, str, str, str]] = []
    for market in markets:
        span = (DATA_END - asset_for(market).start).days / 365.0
        for kind in kinds:
            if kind == "bench":
                # References are run once per market, not once per surviving configuration.
                if market in EXTENSION_ASSETS:
                    planned.append((3.0 * span**2, kind, market, "_references"))
                continue
            if kind == "cross" and market in SCREEN_ASSETS:
                continue  # phase one already ran these markets
            weight = 3.0 if kind == "stress" else 1.0
            planned.extend((weight * span**2, kind, market, key) for key in keys)
    planned.sort(key=lambda job: -job[0])

    started, done, failed = time.time(), 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[str, str, str]] = {
            pool.submit(JOBS[kind], market, key): (kind, market, key)
            for _, kind, market, key in planned
        }
        for future in as_completed(futures):
            kind, market, key = futures[future]
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {kind} {market} {key}: {type(exc).__name__}: {exc}")
                continue
            done += 1
            _say(
                f"[{time.time() - started:6.0f}s] {done:3}/{len(planned)} {kind:9} "
                f"{market:8} {key:12} ({evidence['seconds']:6.0f}s)"
            )
    _say(
        f"\nfinished {done}/{len(planned)} jobs, {failed} failed, "
        f"after {time.time() - started:.0f}s"
    )
    return 0 if failed == 0 else 1


def verdict_for(market: str, key: str) -> dict[str, Any]:
    """Assemble M13's evidence record for one configuration and return what judge() says.

    Reads only what the jobs above wrote. Anything a job did not produce stays ``None``, and
    ``None`` fails its check rather than being treated as absent — which is what stops a
    missing run from quietly buying a better verdict.
    """
    phase = "cross" if market in EXTENSION_ASSETS else None
    base = json.loads(
        (
            (OUT / market / key / "cross" / "evidence.json")
            if phase
            else (OUT / market / key / "evidence.json")
        ).read_text(encoding="utf-8")
    )
    scorecard = base["run"]["card"]
    walk = base["walk_forward"]

    def _read(job: str) -> dict[str, Any] | None:
        path = OUT / market / key / job / "evidence.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    stress = _read("stress")
    worst = (
        min(
            Decimal(str(entry["card"]["total_return"]))
            for entry in stress["stress"]
            if entry["card"] is not None
        )
        if stress
        else None
    )
    deployed = _read("deployed")
    deployed_return = (
        Decimal(str(deployed["run"]["card"]["total_return"]))
        if deployed and deployed["run"]["card"]
        else None
    )
    bench = _read_bench(market)
    evidence = Evidence(
        full=Scorecard.model_validate(scorecard),
        out_of_sample_return=None,
        walk_forward_positive_share=(
            Decimal(str(walk["positive_share"])) if walk["positive_share"] is not None else None
        ),
        walk_forward_median_return=(
            Decimal(str(walk["median_return"])) if walk["median_return"] is not None else None
        ),
        stress_worst_return=worst,
        neighbours_min_profit_factor=None,
        benchmark_best_return=bench,
        deployed_return=deployed_return,
    )
    return {"market": market, "key": key, "verdict": judge(evidence).value}


def _read_bench(market: str) -> Decimal | None:
    """Return the better of the two benchmarks' returns on this market, if both were run."""
    returns: list[Decimal] = []
    for key in ("bench_ema", "bench_breakout"):
        path = OUT / market / key / "evidence.json"
        if path.exists():
            scorecard = json.loads(path.read_text(encoding="utf-8"))["run"]["card"]
            if scorecard is not None:
                returns.append(Decimal(str(scorecard["total_return"])))
    combined = OUT / market / "_references" / "bench" / "evidence.json"
    if combined.exists():
        for name, entry in json.loads(combined.read_text(encoding="utf-8"))["runs"].items():
            if name != "regime_trend" and entry["card"] is not None:
                returns.append(Decimal(str(entry["card"]["total_return"])))
    return max(returns) if returns else None


if __name__ == "__main__":
    sys.exit(main())
