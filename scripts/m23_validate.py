"""Run M23: everything B2 needs before anyone may call it a paper candidate.

Research only. Reads the M16 4h dataset, writes under ``var/research/m23/``. Touches no paper
state, no production risk and no running session.

Jobs, in the order they are worth running — cheapest and most decisive first, so that a
machine that has to be stopped has already produced the answer that matters:

* ``oos``      — the declared out-of-sample window. Four short runs; the whole point of M23.
* ``is``       — the in-sample window, so the two halves can be compared like for like.
* ``deployed`` — the same rule under deployed Risk V2, latching breakers included.
* ``stress``   — the three declared cost scenarios.
* ``neigh``    — the four computed neighbours over each market's whole history.

**BTC's stress and deployed runs are not repeated.** M22 ran them on the identical rule,
window and costs, recorded them in its own ledger, and re-running them would buy a different
experiment id rather than different evidence. The report reads them from
``var/research/m22/BTCUSDT/B2/`` and says so.

**Compute.** Start at one worker. Raise to two only with swap and load healthy, and stop if
the machine begins paging — M22 lost two hours to a run that had degraded 2.7x before anyone
looked.

Usage::

    uv run python scripts/m23_validate.py [--workers 1] [--jobs oos,is] [--markets BTCUSDT]
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
    Loader,
    _factory,
    _s,
    card,
    holding,
    load,
    per_year,
)

from quantplatform.orchestration.research import code_revision
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.folds import WalkForwardPlan
from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.m16 import walk_forward_folds_for
from quantplatform.research.m22 import asset_for, walk_forward_summary
from quantplatform.research.m23 import (
    MARKETS,
    NEIGHBOURS,
    in_sample_window,
    neighbour_definition,
    oos_window,
    windowed_definition,
)
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import STRESS_LABELS, stress_scenarios
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import derive_stress_definition

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m23"


def _home(market: str, job: str) -> Path:
    home = OUT / market / job
    home.mkdir(parents=True, exist_ok=True)
    return home


def _within(market: str, start: object, end: object) -> tuple[Any, ...]:
    bars = load(market)
    return tuple(b for b in bars if start <= b.open_time < end)  # type: ignore[operator]


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


def _windowed(market: str, label: str) -> dict[str, Any]:
    started, revision = time.time(), code_revision(ROOT)
    window = oos_window(market) if label == "oos" else in_sample_window(market)
    home = _home(market, label)
    definition = windowed_definition(market, window, label=label)
    entry = _run(definition, _within(market, window.start, window.end), revision)
    ExperimentLedger(home / "ledger.jsonl").record(
        entry.pop("_result"), store=ResultStore(home / "results")
    )
    return _finish(
        home,
        {
            "milestone": "m23",
            "job": label,
            "market": market,
            "window": {"start": window.start, "end": window.end},
            "run": entry,
        },
        started,
    )


def job_oos(market: str) -> dict[str, Any]:
    """The declared out-of-sample window. Fixed before any of this ran."""
    return _windowed(market, "oos")


def job_in_sample(market: str) -> dict[str, Any]:
    """Everything before the boundary, so the two halves are comparable."""
    return _windowed(market, "is")


def job_deployed(market: str) -> dict[str, Any]:
    """The whole history under deployed Risk V2, latching breakers and all."""
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, "deployed")
    asset = asset_for(market)
    definition = windowed_definition(
        market,
        in_sample_window(market).model_copy(update={"end": oos_window(market).end}),
        label="deployed",
        latching=True,
    )
    entry = _run(definition, load(market), revision)
    ExperimentLedger(home / "ledger.jsonl").record(
        entry.pop("_result"), store=ResultStore(home / "results")
    )
    return _finish(
        home,
        {
            "milestone": "m23",
            "job": "deployed",
            "market": market,
            "start": asset.start,
            "run": entry,
        },
        started,
    )


def job_stress(market: str) -> dict[str, Any]:
    """The three declared cost scenarios over the whole history. Only costs move."""
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, "stress")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    reference = windowed_definition(
        market,
        in_sample_window(market).model_copy(update={"end": oos_window(market).end}),
        label="full",
    )
    bars, entries = load(market), []
    for label, scenario in zip(STRESS_LABELS, stress_scenarios(reference), strict=True):
        entry = _run(derive_stress_definition(reference, scenario), bars, revision)
        ledger.record(
            entry.pop("_result"),
            store=store,
            derived_from=reference.experiment_id,
            variation_kind=VariationKind.STRESS,
        )
        entries.append({"label": label, **entry})
    return _finish(
        home,
        {"milestone": "m23", "job": "stress", "market": market, "stress": entries},
        started,
    )


def job_neighbours(market: str) -> dict[str, Any]:
    """The four computed neighbours. A fragility measurement, never a search."""
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, "neigh")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    reference = windowed_definition(
        market,
        in_sample_window(market).model_copy(update={"end": oos_window(market).end}),
        label="full",
    )
    bars, entries = load(market), []
    for neighbour in NEIGHBOURS:
        definition = neighbour_definition(market, neighbour)
        entry = _run(definition, bars, revision)
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
    return _finish(
        home,
        {"milestone": "m23", "job": "neigh", "market": market, "neighbours": entries},
        started,
    )


def job_walk_forward(market: str) -> dict[str, Any]:
    """Train on one year, test the next, over the whole history. Nothing is fitted."""
    started, revision = time.time(), code_revision(ROOT)
    home = _home(market, "wf")

    reference = windowed_definition(
        market,
        in_sample_window(market).model_copy(update={"end": oos_window(market).end}),
        label="full",
    )
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
    return _finish(
        home,
        {
            "milestone": "m23",
            "job": "wf",
            "market": market,
            "folds": folds,
            **walk_forward_summary(folds),
        },
        started,
    )


JOBS: dict[str, Callable[[str], dict[str, Any]]] = {
    "oos": job_oos,
    "is": job_in_sample,
    "deployed": job_deployed,
    "stress": job_stress,
    "wf": job_walk_forward,
    "neigh": job_neighbours,
}
ORDER: tuple[str, ...] = ("oos", "is", "deployed", "wf", "stress", "neigh")


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run the requested jobs, cheapest and most decisive first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=1, help="Start at 1; see module doc.")
    parser.add_argument("--jobs", default=",".join(ORDER))
    parser.add_argument("--markets", default=",".join(MARKETS))
    args = parser.parse_args()

    kinds = [k for k in ORDER if k in args.jobs.split(",")]
    markets = [m for m in MARKETS if m in args.markets.split(",")]
    planned = [(kind, market) for kind in kinds for market in markets]

    started, done, failed = time.time(), 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[str, str]] = {
            pool.submit(JOBS[kind], market): (kind, market) for kind, market in planned
        }
        for future in as_completed(futures):
            kind, market = futures[future]
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {kind} {market}: {type(exc).__name__}: {exc}")
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
                f"[{time.time() - started:6.0f}s] {done:3}/{len(planned)} {kind:9} {market:8} "
                f"({evidence['seconds']:6.0f}s) {summary}"
            )
    _say(
        f"\nfinished {done}/{len(planned)} jobs, {failed} failed, "
        f"after {time.time() - started:.0f}s"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
