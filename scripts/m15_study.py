"""Run M15: four strategies, three timeframes, four risk policies, 2020-2026.

Research only. Reads the M15 canonical CSVs under ``data/raw/m15/out/`` and writes evidence under
``var/research/m15/``. Touches no paper state, no production risk and no running session.

**Why the work is cut into calendar years.** The backtest engine validates its whole history on
every bar, so a run's cost grows with the square of its length: one 1h run over six years would
take hours. So every timeframe is run year by year — which is also exactly the per-subperiod
view the milestone asks for — and 4h and 1d are additionally run continuously over the whole
period, for drawdown and compounding that no sum of years can show.

Jobs, each writing to its own directory (so no two processes ever append to one ledger):

* ``year``  — one (strategy, timeframe, year): policies A, C, D; the three cost stresses and
  the two declared neighbours, both on the research reference.
* ``wf``    — one (strategy, timeframe): walk-forward on the reference, train on a year and
  test the next, six folds. Its windows double as the reference's yearly results.
* ``full``  — one (strategy, timeframe) at 4h or 1d: all four policies over the whole period.

Usage::

    uv run python scripts/m15_study.py [--workers 6] [--only regime_trend] [--timeframes 4h,1d]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import (
    ExperimentEngineFactory,
    code_revision,
    load_definition,
)
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.folds import WalkForwardPlan, WindowSpec
from quantplatform.research.latch_policy import (
    LatchPolicy,
    LatchPolicyRiskEngine,
    blocked_time_share,
)
from quantplatform.research.ledger import ExperimentLedger, VariationKind
from quantplatform.research.m15 import (
    DATA_END,
    DATA_START,
    POLICIES,
    STUDY_STRATEGIES,
    TIMEFRAMES,
    YEARLY_WINDOWS,
    study_definition,
    walk_forward_folds,
)
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import STRESS_LABELS, Scorecard, stress_scenarios
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import derive_stress_definition
from quantplatform.strategies.research import build_research_registry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/m15/out"
DEPLOYED = ROOT / "tests/fixtures/deployed_risk_v2_definition.json"
OUT = ROOT / "var/research/m15"
REFERENCE_KEY = "REF"
YEAR_JOB_ARITY = 3
"""A yearly job is identified by (strategy, timeframe, year); the other jobs by two of those."""


@cache
def load(timeframe: Timeframe) -> tuple[MarketBar, ...]:
    """Read the canonical CSV for ``timeframe``. Every bar is re-validated by the harness."""
    path = DATA / f"BTCUSDT_{timeframe.value}_{DATA_START.date()}_{DATA_END.date()}.csv"
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
                    trade_count=int(row["trade_count"]) if row["trade_count"] else None,
                    source="csv_historical",
                    is_closed=True,
                )
            )
    return tuple(bars)


def within(bars: tuple[MarketBar, ...], window: WindowSpec) -> tuple[MarketBar, ...]:
    """Return the bars whose open time falls inside ``window``."""
    return tuple(bar for bar in bars if window.contains(bar.open_time))


class Loader:
    """Serves walk-forward windows from bars already in memory."""

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        self._bars = bars

    def __call__(
        self, *, symbol: str, market_type: MarketType, timeframe: Timeframe, window: WindowSpec
    ) -> tuple[MarketBar, ...]:
        """Return the window's bars; the dataset holds one symbol and one market."""
        del symbol, market_type, timeframe
        return within(self._bars, window)


def card(result: ExperimentResult | None) -> dict[str, Any] | None:
    """Return a run's scorecard plus the gross figures needed to add years together."""
    if result is None or result.performance is None:
        return None
    performance = result.performance
    out = Scorecard.from_performance(performance).model_dump()
    out["gross_profit"] = performance.trades.gross_profit
    out["gross_loss"] = performance.trades.gross_loss
    return out


def _s(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _s(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_s(v) for v in value]
    return value


def _plain() -> ExperimentEngineFactory:
    return ExperimentEngineFactory(
        registry=build_research_registry(), features_for=features_for, quote_asset="USDT"
    )


def run_policy(
    definition: ExperimentDefinition,
    policy: LatchPolicy,
    bars: tuple[MarketBar, ...],
    *,
    window: WindowSpec,
    ledger: ExperimentLedger,
    store: ResultStore,
    revision: str | None,
) -> dict[str, Any]:
    """Run one definition under ``policy``, keeping the policy's own account of what it did."""
    captured: list[LatchPolicyRiskEngine] = []

    def risk_engine_for(d: ExperimentDefinition) -> LatchPolicyRiskEngine:
        engine = LatchPolicyRiskEngine(config=d.risk, policy=policy)
        captured.append(engine)
        return engine

    factory = ExperimentEngineFactory(
        registry=build_research_registry(),
        features_for=features_for,
        quote_asset="USDT",
        risk_engine_for=risk_engine_for,
    )
    result = ExperimentRunner().run(definition, bars=bars, factory=factory, code_revision=revision)
    ledger.record(result, store=store)
    stats = captured[-1].stats if captured else None
    return {
        "experiment_id": definition.experiment_id,
        "status": result.status.value,
        "error": result.error,
        "card": card(result),
        "assessed": stats.assessed if stats else None,
        "blocked": stats.blocked if stats else None,
        "blocked_time_share": (
            blocked_time_share(stats, start=window.start, end=window.end) if stats else None
        ),
        "pauses": len(stats.pauses) if stats else None,
        "trips": sorted({(r.value, at) for r, at in stats.trips}) if stats else [],
    }


def job_year(strategy_index: int, timeframe_value: str, year_index: int) -> dict[str, Any]:
    """Policies A, C and D, plus stress and neighbours on the reference, for one year."""
    started = time.time()
    candidate = STUDY_STRATEGIES[strategy_index]
    timeframe = Timeframe(timeframe_value)
    window = YEARLY_WINDOWS[year_index]
    bars = within(load(timeframe), window)
    base = load_definition(DEPLOYED)
    revision = code_revision(ROOT)
    home = OUT / candidate.strategy_id / timeframe.value / str(window.start.year)
    home.mkdir(parents=True, exist_ok=True)
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    evidence: dict[str, Any] = {
        "job": "year",
        "strategy_id": candidate.strategy_id,
        "timeframe": timeframe.value,
        "year": window.start.year,
        "bars": len(bars),
        "code_revision": revision,
        "policies": {},
    }
    for policy in POLICIES:
        if policy.key == REFERENCE_KEY:
            continue  # the reference's yearly result is the walk-forward window of that year
        definition = study_definition(
            candidate, base=base, timeframe=timeframe, policy=policy, window=window
        )
        evidence["policies"][policy.key] = run_policy(
            definition, policy, bars, window=window, ledger=ledger, store=store, revision=revision
        )

    reference = study_definition(
        candidate, base=base, timeframe=timeframe, policy=POLICIES[0], window=window
    )
    runner, plain = ExperimentRunner(), _plain()
    evidence["stress"] = []
    for label, scenario in zip(STRESS_LABELS, stress_scenarios(reference), strict=True):
        definition = derive_stress_definition(reference, scenario)
        result = runner.run(definition, bars=bars, factory=plain, code_revision=revision)
        ledger.record(
            result,
            store=store,
            derived_from=reference.experiment_id,
            variation_kind=VariationKind.STRESS,
        )
        evidence["stress"].append({"label": label, "card": card(result)})
    evidence["neighbours"] = []
    for params in candidate.neighbours:
        definition = reference.model_copy(
            update={"strategy": reference.strategy.model_copy(update={"params": params})}
        )
        result = runner.run(definition, bars=bars, factory=plain, code_revision=revision)
        ledger.record(
            result,
            store=store,
            derived_from=reference.experiment_id,
            variation_kind=VariationKind.SENSITIVITY,
        )
        evidence["neighbours"].append({"params": dict(params), "card": card(result)})
    evidence["seconds"] = round(time.time() - started, 1)
    payload = _s(evidence)
    (home / "evidence.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return evidence


def job_walk_forward(strategy_index: int, timeframe_value: str) -> dict[str, Any]:
    """Six folds on the reference: train one year, test the next. Nothing is fitted."""
    started = time.time()
    candidate = STUDY_STRATEGIES[strategy_index]
    timeframe = Timeframe(timeframe_value)
    base = load_definition(DEPLOYED)
    revision = code_revision(ROOT)
    home = OUT / candidate.strategy_id / timeframe.value / "wf"
    home.mkdir(parents=True, exist_ok=True)
    reference = study_definition(
        candidate, base=base, timeframe=timeframe, policy=POLICIES[0], window=None
    )
    outcome = WalkForwardRunner().run(
        reference,
        WalkForwardPlan(base_experiment_id=reference.experiment_id, folds=walk_forward_folds()),
        loader=Loader(load(timeframe)),
        factory=_plain(),
        store=ResultStore(home / "results"),
        ledger=ExperimentLedger(home / "ledger.jsonl"),
        code_revision=revision,
    )
    evidence = {
        "job": "wf",
        "strategy_id": candidate.strategy_id,
        "timeframe": timeframe.value,
        "code_revision": revision,
        "aborted": outcome.aborted,
        "abort_reason": outcome.abort_reason,
        "folds": [
            {
                "index": run.entry.fold_index,
                "role": run.result.definition.role.value,
                "start": run.result.definition.dataset.start,
                "card": card(run.result),
                "status": run.result.status.value,
                "error": run.result.error,
            }
            for run in outcome.folds
        ],
        "seconds": round(time.time() - started, 1),
    }
    (home / "evidence.json").write_text(json.dumps(_s(evidence), indent=2), encoding="utf-8")
    return evidence


def job_full(strategy_index: int, timeframe_value: str) -> dict[str, Any]:
    """All four policies, continuously over the whole period (4h and 1d only)."""
    started = time.time()
    candidate = STUDY_STRATEGIES[strategy_index]
    timeframe = Timeframe(timeframe_value)
    bars = load(timeframe)
    base = load_definition(DEPLOYED)
    revision = code_revision(ROOT)
    home = OUT / candidate.strategy_id / timeframe.value / "full"
    home.mkdir(parents=True, exist_ok=True)
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    window = WindowSpec(start=DATA_START, end=DATA_END)
    evidence: dict[str, Any] = {
        "job": "full",
        "strategy_id": candidate.strategy_id,
        "timeframe": timeframe.value,
        "bars": len(bars),
        "code_revision": revision,
        "policies": {},
    }
    for policy in POLICIES:
        definition = study_definition(
            candidate, base=base, timeframe=timeframe, policy=policy, window=None
        )
        evidence["policies"][policy.key] = run_policy(
            definition, policy, bars, window=window, ledger=ledger, store=store, revision=revision
        )
    evidence["seconds"] = round(time.time() - started, 1)
    (home / "evidence.json").write_text(json.dumps(_s(evidence), indent=2), encoding="utf-8")
    return evidence


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


_WEIGHT = {Timeframe.H1: 16.0, Timeframe.H4: 1.0, Timeframe.D1: 0.1}


def main() -> int:
    """Run every requested job in parallel, slowest first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="")
    parser.add_argument("--timeframes", default=",".join(tf.value for tf in TIMEFRAMES))
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    frames = [Timeframe(v) for v in args.timeframes.split(",") if v]
    picked = [i for i, c in enumerate(STUDY_STRATEGIES) if not wanted or c.strategy_id in wanted]

    jobs: list[tuple[float, str, tuple[Any, ...]]] = []
    for tf in frames:
        for i in picked:
            jobs.append((_WEIGHT[tf] * 12, "wf", (i, tf.value)))
            if tf is not Timeframe.H1:
                jobs.append((_WEIGHT[tf] * 4 * len(YEARLY_WINDOWS) ** 2, "full", (i, tf.value)))
            for y in range(len(YEARLY_WINDOWS)):
                jobs.append((_WEIGHT[tf] * 8, "year", (i, tf.value, y)))
    jobs.sort(key=lambda job: -job[0])
    runners: dict[str, Callable[..., dict[str, Any]]] = {
        "wf": job_walk_forward,
        "full": job_full,
        "year": job_year,
    }

    started, done, failed = time.time(), 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[str, tuple[Any, ...]]] = {
            pool.submit(runners[kind], *spec): (kind, spec) for _, kind, spec in jobs
        }
        for future in as_completed(futures):
            kind, spec = futures[future]
            name = STUDY_STRATEGIES[spec[0]].strategy_id
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {kind} {name} {spec[1:]}: {type(exc).__name__}: {exc}")
                continue
            done += 1
            _say(
                f"[{time.time() - started:6.0f}s] {done:3}/{len(jobs)} {kind:4} {name:14} "
                f"{spec[1]:3} {spec[2] if len(spec) == YEAR_JOB_ARITY else '':>2} "
                f"({evidence['seconds']:.0f}s)"
            )
    _say(f"\nfinished {done}/{len(jobs)} jobs, {failed} failed, after {time.time() - started:.0f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
