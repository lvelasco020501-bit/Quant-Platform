"""Run M24: two frozen rules, one protocol, one code path.

Research only. Reads the M16 4h dataset, writes under ``var/research/m24/``. Touches no paper
state, no production risk and no running session.

Every job takes a contender key, so B2 and regime_trend run through the same functions with
the same arguments. That is the whole design: a comparison decided by its measuring
instrument is not a comparison.

**What is re-run and what is reused.** M23 already ran B2 on BTC, ETH and BNB under
definitions this milestone verified to be identical field for field — only the name and the
derived experiment id differ. Those results are read rather than recomputed, which saves
about two and a half hours of a machine that has spent this session close to its swap limit.
The reuse is checked rather than asserted: ``--jobs verify`` re-runs one B2 cell under M24's
own name and the report refuses to proceed unless it reproduces M23's number exactly.

B2's SOL stress and neighbours were never run — M23 stopped once SOL's verdict was determined
— so they are run here, because a gap in one contender and not the other would be exactly the
asymmetry this milestone exists to remove.

Usage::

    uv run python scripts/m24_compare.py --contender RT [--workers 1] [--jobs oos,is]
    uv run python scripts/m24_compare.py --contender B2 --jobs verify --markets BTCUSDT
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

from m22_screen import Loader, _factory, _s, card, holding, load, per_year

from quantplatform.orchestration.research import code_revision
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.folds import WalkForwardPlan, WindowSpec
from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.m16 import walk_forward_folds_for
from quantplatform.research.m22 import asset_for, walk_forward_summary
from quantplatform.research.m24 import (
    MARKETS,
    Contender,
    contender_definition,
    contender_for,
    full_window,
    in_sample_window,
    neighbour_definition,
    neighbours_for,
    oos_window,
)
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import STRESS_LABELS, stress_scenarios
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import derive_stress_definition

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m24"


def _home(key: str, market: str, job: str) -> Path:
    home = OUT / key / market / job
    home.mkdir(parents=True, exist_ok=True)
    return home


def _bars(market: str, window: WindowSpec) -> tuple[Any, ...]:
    return tuple(b for b in load(market) if window.contains(b.open_time))


def _run(
    definition: ExperimentDefinition, bars: tuple[Any, ...], revision: str | None
) -> dict[str, Any]:
    result = ExperimentRunner().run(
        definition, bars=bars, factory=_factory(), code_revision=revision
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


def _header(contender: Contender, market: str, job: str, revision: str | None) -> dict[str, Any]:
    return {
        "milestone": "m24",
        "contender": contender.key,
        "strategy_id": contender.strategy_id,
        "params": dict(contender.params),
        "market": market,
        "job": job,
        "code_revision": revision,
    }


def _windowed(key: str, market: str, label: str) -> dict[str, Any]:
    started, revision = time.time(), code_revision(ROOT)
    contender = contender_for(key)
    window = oos_window(market) if label in {"oos", "verify"} else in_sample_window(market)
    home = _home(key, market, label)
    definition = contender_definition(market, contender, window, label=label)
    entry = _run(definition, _bars(market, window), revision)
    ExperimentLedger(home / "ledger.jsonl").record(
        entry.pop("_result"), store=ResultStore(home / "results")
    )
    evidence = _header(contender, market, label, revision)
    evidence["window"] = {"start": window.start, "end": window.end}
    evidence["run"] = entry
    return _finish(home, evidence, started)


def job_oos(key: str, market: str) -> dict[str, Any]:
    """M23's declared out-of-sample window, unchanged."""
    return _windowed(key, market, "oos")


def job_verify(key: str, market: str) -> dict[str, Any]:
    """The same window again under M24's own name, to prove a reused result is reusable."""
    return _windowed(key, market, "verify")


def job_in_sample(key: str, market: str) -> dict[str, Any]:
    """Everything before the boundary."""
    return _windowed(key, market, "is")


def job_deployed(key: str, market: str) -> dict[str, Any]:
    """The whole history under deployed Risk V2, latching breakers and all."""
    started, revision = time.time(), code_revision(ROOT)
    contender = contender_for(key)
    home = _home(key, market, "deployed")
    window = full_window(market)
    definition = contender_definition(market, contender, window, label="deployed", latching=True)
    entry = _run(definition, _bars(market, window), revision)
    ExperimentLedger(home / "ledger.jsonl").record(
        entry.pop("_result"), store=ResultStore(home / "results")
    )
    evidence = _header(contender, market, "deployed", revision)
    evidence["run"] = entry
    return _finish(home, evidence, started)


def job_stress(key: str, market: str) -> dict[str, Any]:
    """The three declared cost scenarios. Only costs move."""
    started, revision = time.time(), code_revision(ROOT)
    contender = contender_for(key)
    home = _home(key, market, "stress")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    window = full_window(market)
    reference = contender_definition(market, contender, window, label="full")
    bars, entries = _bars(market, window), []
    for label, scenario in zip(STRESS_LABELS, stress_scenarios(reference), strict=True):
        entry = _run(derive_stress_definition(reference, scenario), bars, revision)
        ledger.record(
            entry.pop("_result"),
            store=store,
            derived_from=reference.experiment_id,
            variation_kind=VariationKind.STRESS,
        )
        entries.append({"label": label, **entry})
    evidence = _header(contender, market, "stress", revision)
    evidence["stress"] = entries
    return _finish(home, evidence, started)


def job_neighbours(key: str, market: str) -> dict[str, Any]:
    """The four neighbours this contender's axes produce. Fragility, never a search."""
    started, revision = time.time(), code_revision(ROOT)
    contender = contender_for(key)
    home = _home(key, market, "neigh")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    window = full_window(market)
    reference = contender_definition(market, contender, window, label="full")
    bars, entries = _bars(market, window), []
    for neighbour in neighbours_for(contender):
        entry = _run(neighbour_definition(market, contender, neighbour), bars, revision)
        ledger.record(
            entry.pop("_result"),
            store=store,
            derived_from=reference.experiment_id,
            variation_kind=VariationKind.SENSITIVITY,
        )
        entries.append(
            {
                "key": neighbour.key,
                "axis": neighbour.axis,
                "params": dict(neighbour.params),
                **entry,
            }
        )
    evidence = _header(contender, market, "neigh", revision)
    evidence["neighbours"] = entries
    return _finish(home, evidence, started)


def job_walk_forward(key: str, market: str) -> dict[str, Any]:
    """Train on one year, test the next. Nothing is fitted."""
    started, revision = time.time(), code_revision(ROOT)
    contender = contender_for(key)
    home = _home(key, market, "wf")
    reference = contender_definition(market, contender, full_window(market), label="full")
    outcome = WalkForwardRunner().run(
        reference,
        WalkForwardPlan(
            base_experiment_id=reference.experiment_id,
            folds=walk_forward_folds_for(asset_for(market)),
        ),
        loader=Loader(load(market)),
        factory=_factory(),
        store=ResultStore(home / "results"),
        ledger=ExperimentLedger(home / "ledger.jsonl"),
        code_revision=revision,
    )
    folds = [
        {
            "index": run.entry.fold_index,
            "role": run.result.definition.role.value,
            "start": run.result.definition.dataset.start,
            "status": run.result.status.value,
            "card": card(run.result),
        }
        for run in outcome.folds
    ]
    evidence = _header(contender, market, "wf", revision)
    evidence["folds"] = folds
    evidence.update(walk_forward_summary(folds))
    return _finish(home, evidence, started)


JOBS: dict[str, Callable[[str, str], dict[str, Any]]] = {
    "oos": job_oos,
    "verify": job_verify,
    "is": job_in_sample,
    "deployed": job_deployed,
    "wf": job_walk_forward,
    "stress": job_stress,
    "neigh": job_neighbours,
}
ORDER: tuple[str, ...] = ("verify", "oos", "is", "deployed", "wf", "stress", "neigh")


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run the requested jobs for one contender, cheapest and most decisive first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--contender", required=True, choices=("B2", "RT"))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--jobs", default="oos,is,deployed,wf,stress,neigh")
    parser.add_argument("--markets", default=",".join(MARKETS))
    args = parser.parse_args()

    kinds = [k for k in ORDER if k in args.jobs.split(",")]
    markets = [m for m in MARKETS if m in args.markets.split(",")]
    planned = [(kind, market) for kind in kinds for market in markets]

    started, done, failed = time.time(), 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[str, str]] = {
            pool.submit(JOBS[kind], args.contender, market): (kind, market)
            for kind, market in planned
        }
        for future in as_completed(futures):
            kind, market = futures[future]
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {args.contender} {kind} {market}: {type(exc).__name__}: {exc}")
                continue
            done += 1
            run = evidence.get("run")
            summary = ""
            if run and run["card"]:
                c = run["card"]
                summary = (
                    f"net {Decimal(str(c['total_return'])) * 100:.2f}% "
                    f"dd {Decimal(str(c['max_drawdown'])) * 100:.2f}% trades {c['trades']}"
                )
            _say(
                f"[{time.time() - started:6.0f}s] {done:3}/{len(planned)} {args.contender} "
                f"{kind:9} {market:8} ({evidence['seconds']:6.0f}s) {summary}"
            )
    _say(
        f"\nfinished {done}/{len(planned)} jobs, {failed} failed, "
        f"after {time.time() - started:.0f}s"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
