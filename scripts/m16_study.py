"""Run M16: one strategy, one timeframe, one risk policy, six markets.

Research only. Reads the canonical 4h CSVs under ``data/raw/m16/out/`` and writes evidence
under ``var/research/m16/``. Touches no paper state, no production risk and no running session.

Unlike M15, every run here is **continuous** over the asset's own history rather than cut into
calendar years: at 4h a whole-period run is affordable, and a continuous run is the only one
whose drawdown and compounding are real. Stability by year is taken afterwards by splitting
that run's equity curve at each year end.

Jobs, each in its own directory so no two processes ever append to one ledger:

* ``c``       — policy C over the asset's whole history. The run everything else is compared to.
* ``a``       — the same, under deployed Risk V2. The verdict refuses PAPER CANDIDATE without it.
* ``bench``   — both benchmarks under policy C, so "better than the benchmarks" is like for like.
* ``stress``  — the three cost scenarios, derived from the policy C run.
* ``neigh``   — the two declared neighbours, at policy C. Robustness, not selection.
* ``common``  — policy C over the window every asset shares, where SOL begins.
* ``wf``      — walk-forward on policy C: train a year, test the next. Nothing is fitted.

Usage::

    uv run python scripts/m16_study.py [--workers 6] [--only BTCUSDT,ETHUSDT]
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
from quantplatform.research.m16 import (
    ASSETS,
    BENCHMARKS,
    COMMON_START,
    DATA_END,
    DEPLOYED_POLICY,
    POLICY,
    STRATEGY,
    TIMEFRAME,
    Asset,
    study_definition,
    walk_forward_folds_for,
)
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import STRESS_LABELS, Scorecard, stress_scenarios
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import derive_stress_definition
from quantplatform.strategies.research import build_research_registry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/m16/out"
DEPLOYED = ROOT / "tests/fixtures/deployed_risk_v2_definition.json"
OUT = ROOT / "var/research/m16"


@cache
def load(asset: Asset) -> tuple[MarketBar, ...]:
    """Read the canonical 4h CSV for ``asset``. Every bar is re-validated by the harness."""
    name = f"{asset.raw}_{TIMEFRAME.value}_{asset.start.date()}_{DATA_END.date()}.csv"
    bars: list[MarketBar] = []
    with (DATA / name).open(encoding="utf-8") as handle:
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
        """Hold one asset's bars for the runner to slice."""
        self._bars = bars

    def __call__(
        self, *, symbol: str, market_type: MarketType, timeframe: Timeframe, window: WindowSpec
    ) -> tuple[MarketBar, ...]:
        """Return the window's bars; each dataset holds one symbol and one market."""
        del symbol, market_type, timeframe
        return within(self._bars, window)


def card(result: ExperimentResult | None) -> dict[str, Any] | None:
    """Return a run's scorecard plus the gross figures the pooled sample is built from."""
    if result is None or result.performance is None:
        return None
    performance = result.performance
    out = Scorecard.from_performance(performance).model_dump()
    out["gross_profit"] = performance.trades.gross_profit
    out["gross_loss"] = performance.trades.gross_loss
    out["max_consecutive_losses"] = performance.trades.max_consecutive_losses
    out["commission_paid"] = performance.commission_paid
    out["slippage_paid"] = performance.slippage_paid
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


def _home(asset: Asset, job: str) -> Path:
    home = OUT / asset.raw / job
    home.mkdir(parents=True, exist_ok=True)
    return home


def _year_split(result: ExperimentResult | None) -> list[dict[str, Any]]:
    """Return each calendar year's return and trade count, cut from the continuous run."""
    if result is None or result.performance is None:
        return []
    opening = result.performance.initial_equity
    years = sorted({point.at.year for point in result.equity_curve})
    out: list[dict[str, Any]] = []
    for year in years:
        points = [p for p in result.equity_curve if p.at.year == year]
        closing = points[-1].equity if points else opening
        out.append(
            {
                "year": year,
                "return": (closing / opening - 1) if opening else None,
                "trades": sum(1 for t in result.trades if t.closed_at.year == year),
            }
        )
        opening = closing
    return out


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
        "name": definition.name,
        "status": result.status.value,
        "error": result.error,
        "card": card(result),
        "per_year": _year_split(result),
        "blocked": stats.blocked if stats else None,
        "assessed": stats.assessed if stats else None,
        "blocked_time_share": (
            blocked_time_share(stats, start=window.start, end=window.end) if stats else None
        ),
        "trips": sorted({(r.value, at) for r, at in stats.trips}) if stats else [],
    }


def _evidence(
    asset: Asset, job: str, revision: str | None, extra: dict[str, object]
) -> dict[str, Any]:
    """Return the header every job's evidence file carries, plus that job's own findings."""
    return {
        "job": job,
        "symbol": asset.symbol,
        "raw": asset.raw,
        "start": asset.start,
        "code_revision": revision,
        **extra,
    }


def _finish(home: Path, evidence: dict[str, Any], started: float) -> dict[str, Any]:
    evidence["seconds"] = round(time.time() - started, 1)
    (home / "evidence.json").write_text(json.dumps(_s(evidence), indent=2), encoding="utf-8")
    return evidence


def job_policy(asset_index: int, policy_key: str) -> dict[str, Any]:
    """Policy C (or deployed A) over the asset's whole history, continuously."""
    started = time.time()
    asset = ASSETS[asset_index]
    policy = POLICY if policy_key == POLICY.key else DEPLOYED_POLICY
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, policy_key.lower())
    window = WindowSpec(start=asset.start, end=DATA_END)
    definition = study_definition(asset, STRATEGY, base=base, policy=policy, window=None)
    entry = run_policy(
        definition,
        policy,
        bars,
        window=window,
        ledger=ExperimentLedger(home / "ledger.jsonl"),
        store=ResultStore(home / "results"),
        revision=revision,
    )
    return _finish(home, _evidence(asset, policy_key.lower(), revision, {"run": entry}), started)


def job_c(asset_index: int) -> dict[str, Any]:
    """Policy C — the configuration under test."""
    return job_policy(asset_index, POLICY.key)


def job_a(asset_index: int) -> dict[str, Any]:
    """Deployed Risk V2, which the verdict needs before it will say PAPER CANDIDATE."""
    return job_policy(asset_index, DEPLOYED_POLICY.key)


def job_common(asset_index: int) -> dict[str, Any]:
    """Policy C over the window every asset shares, so the six can be compared side by side."""
    started = time.time()
    asset = ASSETS[asset_index]
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, "common")
    window = WindowSpec(start=COMMON_START, end=DATA_END)
    definition = study_definition(asset, STRATEGY, base=base, policy=POLICY, window=window)
    entry = run_policy(
        definition,
        POLICY,
        within(bars, window),
        window=window,
        ledger=ExperimentLedger(home / "ledger.jsonl"),
        store=ResultStore(home / "results"),
        revision=revision,
    )
    return _finish(home, _evidence(asset, "common", revision, {"run": entry}), started)


def job_bench(asset_index: int) -> dict[str, Any]:
    """Both benchmarks, on the same market under the same policy and costs."""
    started = time.time()
    asset = ASSETS[asset_index]
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, "bench")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    window = WindowSpec(start=asset.start, end=DATA_END)
    runs = {}
    for benchmark in BENCHMARKS:
        definition = study_definition(asset, benchmark, base=base, policy=POLICY, window=None)
        runs[benchmark.strategy_id] = run_policy(
            definition,
            POLICY,
            bars,
            window=window,
            ledger=ledger,
            store=store,
            revision=revision,
        )
    return _finish(home, _evidence(asset, "bench", revision, {"runs": runs}), started)


def job_stress(asset_index: int) -> dict[str, Any]:
    """The three cost scenarios, derived from this asset's policy C run."""
    started = time.time()
    asset = ASSETS[asset_index]
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, "stress")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    reference = study_definition(asset, STRATEGY, base=base, policy=POLICY, window=None)
    runner, plain = ExperimentRunner(), _plain()
    entries = []
    for label, scenario in zip(STRESS_LABELS, stress_scenarios(reference), strict=True):
        definition = derive_stress_definition(reference, scenario)
        result = runner.run(definition, bars=bars, factory=plain, code_revision=revision)
        ledger.record(
            result,
            store=store,
            derived_from=reference.experiment_id,
            variation_kind=VariationKind.STRESS,
        )
        entries.append({"label": label, "card": card(result), "status": result.status.value})
    return _finish(home, _evidence(asset, "stress", revision, {"stress": entries}), started)


def job_neighbours(asset_index: int) -> dict[str, Any]:
    """The two neighbours declared in M13 — a robustness check, never a search."""
    started = time.time()
    asset = ASSETS[asset_index]
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, "neigh")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    reference = study_definition(asset, STRATEGY, base=base, policy=POLICY, window=None)
    runner, plain = ExperimentRunner(), _plain()
    entries = []
    for params in STRATEGY.neighbours:
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
        entries.append({"params": dict(params), "card": card(result)})
    return _finish(home, _evidence(asset, "neigh", revision, {"neighbours": entries}), started)


def job_walk_forward(asset_index: int) -> dict[str, Any]:
    """Train on one year, test the next, over this asset's own history. Nothing is fitted."""
    started = time.time()
    asset = ASSETS[asset_index]
    base, revision = load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, "wf")
    reference = study_definition(asset, STRATEGY, base=base, policy=POLICY, window=None)
    outcome = WalkForwardRunner().run(
        reference,
        WalkForwardPlan(
            base_experiment_id=reference.experiment_id, folds=walk_forward_folds_for(asset)
        ),
        loader=Loader(load(asset)),
        factory=_plain(),
        store=ResultStore(home / "results"),
        ledger=ExperimentLedger(home / "ledger.jsonl"),
        code_revision=revision,
    )
    folds = [
        {
            "index": run.entry.fold_index,
            "role": run.result.definition.role.value,
            "start": run.result.definition.dataset.start,
            "card": card(run.result),
            "status": run.result.status.value,
        }
        for run in outcome.folds
    ]
    evidence = _evidence(
        asset,
        "wf",
        revision,
        {"aborted": outcome.aborted, "abort_reason": outcome.abort_reason, "folds": folds},
    )
    return _finish(home, evidence, started)


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run every requested job in parallel, longest history first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="")
    parser.add_argument("--jobs", default="", help="Comma-separated job kinds; default all.")
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    kinds = {kind for kind in args.jobs.split(",") if kind}
    picked = [i for i, a in enumerate(ASSETS) if not wanted or a.raw in wanted]

    runners: dict[str, Callable[..., dict[str, Any]]] = {
        "c": job_c,
        "a": job_a,
        "bench": job_bench,
        "stress": job_stress,
        "neigh": job_neighbours,
        "common": job_common,
        "wf": job_walk_forward,
    }
    weights = {"c": 1.0, "a": 1.0, "bench": 2.0, "stress": 3.0, "neigh": 2.0, "common": 0.6}
    jobs: list[tuple[float, str, int]] = []
    for index in picked:
        span = (DATA_END - ASSETS[index].start).days / 365.0
        for kind in runners:
            if kinds and kind not in kinds:
                continue
            # Cost grows with the square of the history: the engine revalidates it every bar.
            jobs.append((weights.get(kind, 0.5) * span**2, kind, index))
    jobs.sort(key=lambda job: -job[0])

    started, done, failed = time.time(), 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[str, int]] = {
            pool.submit(runners[kind], index): (kind, index) for _, kind, index in jobs
        }
        for future in as_completed(futures):
            kind, index = futures[future]
            raw = ASSETS[index].raw
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {kind} {raw}: {type(exc).__name__}: {exc}")
                continue
            done += 1
            _say(
                f"[{time.time() - started:6.0f}s] {done:3}/{len(jobs)} {kind:6} {raw:8} "
                f"({evidence['seconds']:.0f}s)"
            )
    _say(f"\nfinished {done}/{len(jobs)} jobs, {failed} failed, after {time.time() - started:.0f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
