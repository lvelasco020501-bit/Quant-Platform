"""Run M17: five recovery rules over M16's six markets, at three fee settings each.

Research only. Reads M16's canonical 4h CSVs under ``data/raw/m16/out/`` and writes evidence
under ``var/research/m17/``. Touches no paper state, no production risk and no running session.

Every run is continuous over the market's own history, as in M16. The three fee settings are
the continuity probe: a policy whose trade count jumps when fees move by a quarter is the
defect this milestone exists to remove, not a policy worth having.

Jobs, each in its own directory so no two processes ever append to one ledger:

* ``policy`` — one (market, policy): the three fee settings, continuously.
* ``stress`` — one (market, policy): M16's three cost scenarios, for the final comparison.

Usage::

    uv run python scripts/m17_study.py [--workers 6] [--only XRPUSDT] [--policies C,G]
                                       [--jobs policy,stress]
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
from quantplatform.research.folds import WindowSpec
from quantplatform.research.latch_policy import blocked_time_share
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.m16 import ASSETS, DATA_END, STRATEGY, TIMEFRAME, Asset
from quantplatform.research.m17 import FEE_MULTIPLIERS, POLICIES, fee_multiplied, study_definition
from quantplatform.research.recovery import RecoveryLatchRiskEngine, RecoveryPolicy
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import STRESS_LABELS, Scorecard, stress_scenarios
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import derive_stress_definition
from quantplatform.strategies.research import build_research_registry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/m16/out"
DEPLOYED = ROOT / "tests/fixtures/deployed_risk_v2_definition.json"
OUT = ROOT / "var/research/m17"
SECONDS_PER_DAY = 86_400


@cache
def load(asset: Asset) -> tuple[MarketBar, ...]:
    """Read M16's canonical 4h CSV for ``asset``. Every bar is re-validated by the harness."""
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


def card(result: ExperimentResult | None) -> dict[str, Any] | None:
    """Return a run's scorecard plus the figures the comparison is built from."""
    if result is None or result.performance is None:
        return None
    performance = result.performance
    out = Scorecard.from_performance(performance).model_dump()
    out["gross_profit"] = performance.trades.gross_profit
    out["gross_loss"] = performance.trades.gross_loss
    out["max_consecutive_losses"] = performance.trades.max_consecutive_losses
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


def _year_split(result: ExperimentResult | None) -> list[dict[str, Any]]:
    """Return each calendar year's return and trade count, cut from the continuous run."""
    if result is None or result.performance is None:
        return []
    opening = result.performance.initial_equity
    out: list[dict[str, Any]] = []
    for year in sorted({point.at.year for point in result.equity_curve}):
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


def _halts(engine: RecoveryLatchRiskEngine, *, end: datetime) -> list[dict[str, Any]]:
    """Return every halt the policy imposed, and how long each one lasted in days."""
    out = []
    for began, finished in engine.stats.pauses:
        closed = finished if finished is not None else end
        out.append(
            {
                "from": began,
                "to": finished,
                "days": round((closed - began).total_seconds() / SECONDS_PER_DAY, 1),
            }
        )
    return out


def run_once(
    definition: ExperimentDefinition,
    policy: RecoveryPolicy,
    bars: tuple[MarketBar, ...],
    *,
    window: WindowSpec,
    ledger: ExperimentLedger,
    store: ResultStore,
    revision: str | None,
) -> dict[str, Any]:
    """Run one definition under one recovery rule, keeping the rule's own account of it."""
    captured: list[RecoveryLatchRiskEngine] = []

    def risk_engine_for(d: ExperimentDefinition) -> RecoveryLatchRiskEngine:
        engine = RecoveryLatchRiskEngine(config=d.risk, policy=policy)
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
    engine = captured[-1] if captured else None
    halts = _halts(engine, end=window.end) if engine is not None else []
    return {
        "experiment_id": definition.experiment_id,
        "name": definition.name,
        "status": result.status.value,
        "error": result.error,
        "card": card(result),
        "per_year": _year_split(result),
        "blocked": engine.stats.blocked if engine else None,
        "assessed": engine.stats.assessed if engine else None,
        "blocked_time_share": (
            blocked_time_share(engine.stats, start=window.start, end=window.end) if engine else None
        ),
        "halts": halts,
        "halt_count": len(halts),
        "longest_halt_days": max((h["days"] for h in halts), default=0.0),
    }


def _home(asset: Asset, policy: RecoveryPolicy, job: str) -> Path:
    home = OUT / asset.raw / policy.key / job
    home.mkdir(parents=True, exist_ok=True)
    return home


def job_policy(asset_index: int, policy_index: int) -> dict[str, Any]:
    """One market under one recovery rule, at each of the three fee settings."""
    started = time.time()
    asset, policy = ASSETS[asset_index], POLICIES[policy_index]
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, policy, "policy")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    window = WindowSpec(start=asset.start, end=DATA_END)
    definition = study_definition(asset, STRATEGY, base=base, policy=policy)
    runs = []
    for multiplier in FEE_MULTIPLIERS:
        priced = definition if multiplier == 1 else fee_multiplied(definition, multiplier)
        entry = run_once(
            priced,
            policy,
            bars,
            window=window,
            ledger=ledger,
            store=store,
            revision=revision,
        )
        entry["fee_multiplier"] = multiplier
        runs.append(entry)
    evidence = {
        "job": "policy",
        "symbol": asset.symbol,
        "raw": asset.raw,
        "policy": policy.key,
        "policy_label": policy.label,
        "start": asset.start,
        "code_revision": revision,
        "runs": runs,
        "seconds": round(time.time() - started, 1),
    }
    (home / "evidence.json").write_text(json.dumps(_s(evidence), indent=2), encoding="utf-8")
    return evidence


def job_stress(asset_index: int, policy_index: int) -> dict[str, Any]:
    """M16's three cost scenarios, for one market under one recovery rule."""
    started = time.time()
    asset, policy = ASSETS[asset_index], POLICIES[policy_index]
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = _home(asset, policy, "stress")
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    window = WindowSpec(start=asset.start, end=DATA_END)
    reference = study_definition(asset, STRATEGY, base=base, policy=policy)
    entries = []
    for label, scenario in zip(STRESS_LABELS, stress_scenarios(reference), strict=True):
        definition = derive_stress_definition(reference, scenario)
        entry = run_once(
            definition,
            policy,
            bars,
            window=window,
            ledger=ledger,
            store=store,
            revision=revision,
        )
        entry["label"] = label
        entries.append(entry)
    evidence = {
        "job": "stress",
        "symbol": asset.symbol,
        "raw": asset.raw,
        "policy": policy.key,
        "start": asset.start,
        "code_revision": revision,
        "stress": entries,
        "seconds": round(time.time() - started, 1),
    }
    (home / "evidence.json").write_text(json.dumps(_s(evidence), indent=2), encoding="utf-8")
    return evidence


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run every requested job in parallel, longest history first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="")
    parser.add_argument("--policies", default="")
    parser.add_argument("--jobs", default="policy")
    args = parser.parse_args()
    markets = {name for name in args.only.split(",") if name}
    keys = {key for key in args.policies.split(",") if key}
    kinds = {kind for kind in args.jobs.split(",") if kind}

    runners: dict[str, Callable[..., dict[str, Any]]] = {"policy": job_policy, "stress": job_stress}
    jobs: list[tuple[float, str, int, int]] = []
    for asset_index, asset in enumerate(ASSETS):
        if markets and asset.raw not in markets:
            continue
        span = (DATA_END - asset.start).days / 365.0
        for policy_index, policy in enumerate(POLICIES):
            if keys and policy.key not in keys:
                continue
            for kind in kinds:
                # Cost grows with the square of the history: the engine revalidates it every bar.
                jobs.append((span**2, kind, asset_index, policy_index))
    jobs.sort(key=lambda job: -job[0])

    started, done, failed = time.time(), 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[str, int, int]] = {
            pool.submit(runners[kind], asset_index, policy_index): (kind, asset_index, policy_index)
            for _, kind, asset_index, policy_index in jobs
        }
        for future in as_completed(futures):
            kind, asset_index, policy_index = futures[future]
            label = f"{ASSETS[asset_index].raw} {POLICIES[policy_index].key}"
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {kind} {label}: {type(exc).__name__}: {exc}")
                continue
            done += 1
            _say(
                f"[{time.time() - started:6.0f}s] {done:3}/{len(jobs)} {kind:6} {label:14} "
                f"({evidence['seconds']:.0f}s)"
            )
    _say(f"\nfinished {done}/{len(jobs)} jobs, {failed} failed, after {time.time() - started:.0f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
