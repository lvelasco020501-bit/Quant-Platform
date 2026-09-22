"""Run M18: policies C, E and G where the breaker actually fires.

Research only. Reads M16's canonical 4h CSVs and writes evidence under ``var/research/m18/``.
Touches no paper state, no production risk and no running session.

Each job is one market under one policy, and runs four things:

* the cost ladder at the canonical 10% limit — S2 (fees x3, slippage x5, 10 bps) and S3
  (fees x5, slippage x10, 25 bps);
* the mechanism probe at a 5% limit and real costs, where the breaker engages often enough
  to watch a market reopen repeatedly;
* the same probe with fees a quarter higher, so continuity is measured in the regime where
  halts actually happen rather than only where they never do.

Every run records each halt with the equity and reference it began and ended on, which is
what makes a ratchet — a chain of restarts each lower than the last — visible at all.

Usage::

    uv run python scripts/m18_study.py [--workers 3] [--only XRPUSDT] [--policies C,G]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
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
from quantplatform.research.m17 import fee_multiplied, study_definition
from quantplatform.research.m18 import (
    PROBE_DRAWDOWN,
    PROBE_FEE_MULTIPLIERS,
    SCENARIOS,
    flapping_share,
    ratchet_chains,
    scenario_for,
    studied_policies,
    with_threshold,
)
from quantplatform.research.recovery import RecoveryLatchRiskEngine, RecoveryPolicy
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import Scorecard
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import derive_stress_definition
from quantplatform.strategies.research import build_research_registry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/m16/out"
DEPLOYED = ROOT / "tests/fixtures/deployed_risk_v2_definition.json"
OUT = ROOT / "var/research/m18"
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
    """Return a run's scorecard plus the figures this milestone compares."""
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


def _traded_after(result: ExperimentResult, episodes: list[dict[str, Any]]) -> int:
    """Return how many halts were followed by a trade before the next halt began."""
    opened = sorted(trade.opened_at for trade in result.trades)
    starts = [episode["began"] for episode in episodes]
    effective = 0
    for episode in episodes:
        release = episode.get("ended")
        if release is None:
            continue
        later = [start for start in starts if start > release]
        limit = min(later) if later else None
        if any(when >= release and (limit is None or when < limit) for when in opened):
            effective += 1
    return effective


def run_once(
    definition: ExperimentDefinition,
    policy: RecoveryPolicy,
    bars: tuple[MarketBar, ...],
    *,
    label: str,
    window: WindowSpec,
    ledger: ExperimentLedger,
    store: ResultStore,
    revision: str | None,
) -> dict[str, Any]:
    """Run one definition under one recovery rule and measure how the halts behaved."""
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
    episodes = engine.episodes if engine is not None else []
    chains = ratchet_chains(episodes)
    ended = [e for e in episodes if e.get("ended") is not None]
    return {
        "label": label,
        "experiment_id": definition.experiment_id,
        "name": definition.name,
        "status": result.status.value,
        "error": result.error,
        "threshold": policy.drawdown_pct,
        "card": card(result),
        "halts": len(episodes),
        "reopenings": len(ended),
        "effective_reopenings": _traded_after(result, episodes),
        "blocked_time_share": (
            blocked_time_share(engine.stats, start=window.start, end=window.end) if engine else None
        ),
        "longest_halt_days": max(
            (
                ((e["ended"] or window.end) - e["began"]).total_seconds() / SECONDS_PER_DAY
                for e in episodes
            ),
            default=0.0,
        ),
        "flapping_share": flapping_share(episodes),
        "ratchet_chains": chains,
        "longest_ratchet_chain": max((c["halts"] for c in chains), default=0),
        "worst_chain_decline": max(
            (c["decline"] for c in chains if c["decline"] is not None), default=None
        ),
        "episodes": episodes,
    }


def job(asset_index: int, policy_index: int) -> dict[str, Any]:
    """One market under one policy: the cost ladder, then the tight-threshold probe."""
    started = time.time()
    asset = ASSETS[asset_index]
    policy = studied_policies()[policy_index]
    bars, base, revision = load(asset), load_definition(DEPLOYED), code_revision(ROOT)
    home = OUT / asset.raw / policy.key
    home.mkdir(parents=True, exist_ok=True)
    ledger, store = ExperimentLedger(home / "ledger.jsonl"), ResultStore(home / "results")
    window = WindowSpec(start=asset.start, end=DATA_END)
    canonical = study_definition(asset, STRATEGY, base=base, policy=policy)

    runs: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        definition = derive_stress_definition(canonical, scenario_for(canonical, scenario))
        runs.append(
            run_once(
                definition,
                policy,
                bars,
                label=scenario.label,
                window=window,
                ledger=ledger,
                store=store,
                revision=revision,
            )
        )

    probe = with_threshold(policy, PROBE_DRAWDOWN)
    probed = study_definition(asset, STRATEGY, base=base, policy=probe)
    for multiplier in PROBE_FEE_MULTIPLIERS:
        definition = probed if multiplier == 1 else fee_multiplied(probed, multiplier)
        runs.append(
            run_once(
                definition,
                probe,
                bars,
                label=f"probe 5% drawdown, fees x{multiplier.normalize()}",
                window=window,
                ledger=ledger,
                store=store,
                revision=revision,
            )
        )

    evidence = {
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


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run every requested job in parallel, longest history first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--only", default="")
    parser.add_argument("--policies", default="")
    args = parser.parse_args()
    markets = {name for name in args.only.split(",") if name}
    keys = {key for key in args.policies.split(",") if key}

    policies = studied_policies()
    jobs: list[tuple[float, int, int]] = []
    for asset_index, asset in enumerate(ASSETS):
        if markets and asset.raw not in markets:
            continue
        span = (DATA_END - asset.start).days / 365.0
        for policy_index, policy in enumerate(policies):
            if keys and policy.key not in keys:
                continue
            # Cost grows with the square of the history: the engine revalidates it every bar.
            jobs.append((span**2, asset_index, policy_index))
    jobs.sort(key=lambda entry: -entry[0])

    started, done, failed = time.time(), 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[int, int]] = {
            pool.submit(job, asset_index, policy_index): (asset_index, policy_index)
            for _, asset_index, policy_index in jobs
        }
        for future in as_completed(futures):
            asset_index, policy_index = futures[future]
            label = f"{ASSETS[asset_index].raw} {policies[policy_index].key}"
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {label}: {type(exc).__name__}: {exc}")
                continue
            done += 1
            halts = sum(run["halts"] for run in evidence["runs"])
            _say(
                f"[{time.time() - started:6.0f}s] {done:3}/{len(jobs)} {label:14} "
                f"({evidence['seconds']:.0f}s, {halts} halts)"
            )
    _say(f"\nfinished {done}/{len(jobs)} jobs, {failed} failed, after {time.time() - started:.0f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
