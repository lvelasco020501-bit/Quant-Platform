"""Run the M13 strategy discovery sprint and write its evidence.

Research only. Reads the M10c dataset and the deployed Risk V2 definition, writes under
``var/research/m13/``, and touches no paper state, no VPS and no running session.

Everything this runs was declared in :mod:`quantplatform.research.sprint` before the first
result existed. This is scenario B only — the research variant, not Risk V2. Scenario A is
``m13_deployed.py``; verdicts are made in ``m13_report.py`` and nowhere else.

Usage::

    uv run python scripts/m13_sprint.py [--workers 6] [--only momentum_roc,ema_slope] [--smoke]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.core.enums import MarketType, SignalAction, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import (
    ExperimentEngineFactory,
    code_revision,
    load_definition,
)
from quantplatform.research.definition import ExperimentRole
from quantplatform.research.folds import WindowSpec
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sensitivity import (
    SensitivityPlan,
    SensitivityRunner,
    SensitivityVariation,
)
from quantplatform.research.sprint import (
    CANDIDATES,
    IN_SAMPLE_WINDOW,
    OUT_OF_SAMPLE_WINDOW,
    STRESS_LABELS,
    Family,
    Scorecard,
    SprintCandidate,
    definition_for,
    narrowed,
    stress_scenarios,
    walk_forward_plan,
)
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import StressPlan, StressRunner
from quantplatform.strategies.research import build_research_registry

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/raw/m10c/BTCUSDT_1h_2025-09-01_2026-09-01.csv"
DEPLOYED = ROOT / "var/research/m10c/def_breakout_v2.json"
OUT = ROOT / "var/research/m13"
SMOKE_BARS = 2000


def load_bars(*, smoke: bool = False) -> tuple[MarketBar, ...]:
    """Read the canonical M10c CSV into closed bars. Every bar is re-validated by the harness."""
    bars: list[MarketBar] = []
    with DATASET.open(encoding="utf-8") as handle:
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
                    trade_count=int(row["trade_count"]),
                    source="csv_historical",
                    is_closed=True,
                )
            )
    return tuple(bars[:SMOKE_BARS] if smoke else bars)


def in_window(bars: tuple[MarketBar, ...], window: WindowSpec) -> tuple[MarketBar, ...]:
    """Return the bars whose open time falls inside a half-open window."""
    return tuple(bar for bar in bars if window.contains(bar.open_time))


class SliceLoader:
    """Serves each walk-forward window from bars already in memory."""

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        self._bars = bars

    def __call__(
        self, *, symbol: str, market_type: MarketType, timeframe: Timeframe, window: WindowSpec
    ) -> tuple[MarketBar, ...]:
        """Return the window's bars; the dataset has one symbol, market and timeframe."""
        del symbol, market_type, timeframe
        return in_window(self._bars, window)


def _s(value: object) -> object:
    """Make a value JSON-safe, keeping decimals exact as strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _s(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_s(item) for item in value]
    return value


def _card(result: ExperimentResult | None) -> dict[str, Any] | None:
    if result is None or result.performance is None:
        return None
    return Scorecard.from_performance(result.performance).model_dump()


def run_candidate(index: int, *, smoke: bool) -> dict[str, Any]:
    """Run every plan for one candidate and return its evidence. Executed in a worker."""
    candidate: SprintCandidate = CANDIDATES[index]
    started = time.time()
    bars = load_bars(smoke=smoke)
    registry = build_research_registry()
    factory = ExperimentEngineFactory(
        registry=registry, features_for=features_for, quote_asset="USDT"
    )
    revision = code_revision(ROOT)
    deployed = load_definition(DEPLOYED)
    version = registry.metadata_for(candidate.strategy_id).version
    full = definition_for(candidate, base=deployed, strategy_version=version)

    home = OUT / candidate.strategy_id
    home.mkdir(parents=True, exist_ok=True)
    store = ResultStore(home / "results")
    ledger = ExperimentLedger(home / "ledger.jsonl")
    runner = ExperimentRunner()
    evidence: dict[str, Any] = {
        "strategy_id": candidate.strategy_id,
        "family": candidate.family.value,
        "params": dict(candidate.params),
        "experiment_id": full.experiment_id,
        "code_revision": revision,
    }

    # 1. Full year, plus the three cost scenarios. The stress baseline *is* the full-year run.
    plan = StressPlan(base_experiment_id=full.experiment_id, scenarios=stress_scenarios(full))
    stress = StressRunner(runner=runner).run(
        full, plan, bars=bars, factory=factory, store=store, ledger=ledger, code_revision=revision
    )
    evidence["full"] = _card(stress.baseline.result)
    evidence["full_status"] = stress.baseline.result.status.value
    evidence["full_error"] = stress.baseline.result.error
    evidence["stress"] = [
        {"label": label, "card": _card(run.result)}
        for label, run in zip(STRESS_LABELS, stress.scenarios, strict=False)
    ]
    evidence["stress_aborted"] = stress.aborted

    # 2. In-sample and out-of-sample, declared in the protocol.
    for key, window, role in (
        ("in_sample", IN_SAMPLE_WINDOW, ExperimentRole.IN_SAMPLE),
        ("out_of_sample", OUT_OF_SAMPLE_WINDOW, ExperimentRole.OUT_OF_SAMPLE),
    ):
        claimed = ExperimentRole.BENCHMARK if candidate.family is Family.BENCHMARK else role
        definition = narrowed(full, window, claimed)
        result = runner.run(
            definition, bars=in_window(bars, window), factory=factory, code_revision=revision
        )
        ledger.record(result, store=store)
        evidence[key] = _card(result)

    # 3. Walk-forward over M10c's four windows.
    outcome = WalkForwardRunner(runner=runner).run(
        full,
        walk_forward_plan(full),
        loader=SliceLoader(bars),
        factory=factory,
        store=store,
        ledger=ledger,
        code_revision=revision,
    )
    folds = [
        {
            "index": run.entry.fold_index,
            "role": run.result.definition.role.value,
            "card": _card(run.result),
            "status": run.result.status.value,
        }
        for run in outcome.folds
    ]
    evidence["walk_forward_folds"] = folds
    evidence["walk_forward_aborted"] = outcome.aborted
    if not outcome.aborted:
        summary = outcome.summarise(require_complete=False)
        evidence["walk_forward"] = summary.model_dump()

    # 4. Sensitivity: the two neighbours declared in the protocol, never a search.
    if candidate.neighbours:
        sens_plan = SensitivityPlan(
            base_experiment_id=full.experiment_id,
            variations=tuple(SensitivityVariation(params=n) for n in candidate.neighbours),
        )
        sens = SensitivityRunner(runner=runner).run(
            full,
            sens_plan,
            bars=bars,
            factory=factory,
            store=store,
            ledger=ledger,
            code_revision=revision,
        )
        evidence["neighbours"] = [
            {"params": dict(n), "card": _card(run.result)}
            for n, run in zip(candidate.neighbours, sens.variations, strict=False)
        ]

    # 5. Diagnostics the harness summary does not carry: who closed the trades, and when the
    #    *deployed* profile's latching breaker would have stopped this strategy.
    raw = factory(full).run(bars)
    exit_signals = sum(1 for s in raw.signals if s.action is SignalAction.EXIT_LONG)
    sells = sum(
        1
        for d in raw.decisions
        if d.approved_order is not None and d.approved_order.side.value == "sell"
    )
    evidence["exits"] = {
        "trades": len(raw.trades),
        "approved_sells": sells,
        "strategy_exit_signals": exit_signals,
        "overlay_exits_approx": max(sells - exit_signals, 0),
    }
    deployed_def = full.model_copy(update={"risk": deployed.risk})
    latched = factory(deployed_def).run(bars)
    first_latch = next(
        (
            d.decided_at
            for d in latched.decisions
            if any("latched" in reason for reason in d.rejection_reasons)
        ),
        None,
    )
    evidence["deployed_profile"] = {
        "card": Scorecard.from_performance(latched.performance).model_dump()
        if latched.performance is not None
        else None,
        "first_latch_at": first_latch,
        "latched_rejections": sum(
            1 for d in latched.decisions if any("latched" in r for r in d.rejection_reasons)
        ),
    }
    evidence["seconds"] = round(time.time() - started, 1)
    payload: dict[str, Any] = _s(evidence)  # type: ignore[assignment]
    (home / "evidence.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _say(text: str) -> None:
    """Write one progress line and flush it, so a background run can be followed."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run the requested candidates in parallel, then judge them by the protocol."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    wanted = {name for name in args.only.split(",") if name}
    indices = [i for i, c in enumerate(CANDIDATES) if not wanted or c.strategy_id in wanted]
    # Research candidates carry the sensitivity sweep, so start them first.
    indices.sort(key=lambda i: CANDIDATES[i].family is Family.BENCHMARK)
    OUT.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict[str, Any]] = {}
    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_candidate, i, smoke=args.smoke): i for i in indices}
        for future in as_completed(futures):
            name = CANDIDATES[futures[future]].strategy_id
            # Only the worker's result is guarded. Reporting a finished candidate must never be
            # able to relabel it FAILED, which is exactly what a wider try once did.
            try:
                results[name] = future.result()
            except Exception as exc:  # a crashed candidate is reported, never hidden
                _say(f"{name}: FAILED {type(exc).__name__}: {exc}")
                continue
            full = results[name].get("full") or {}
            _say(
                f"[{time.time() - started:7.0f}s] {name:24} done in "
                f"{results[name]['seconds']:6.0f}s  return={full.get('total_return')} "
                f"trades={full.get('trades')}"
            )

    _say(f"\nfinished {len(results)}/{len(indices)} candidates after {time.time() - started:.0f}s")
    _say("next: scripts/m13_deployed.py (scenario A), then scripts/m13_report.py (verdicts)")
    return 0 if len(results) == len(indices) else 1


if __name__ == "__main__":
    sys.exit(main())
