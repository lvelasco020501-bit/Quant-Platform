"""Run M14: latch policies (Part A) and timeframes (Part B), and write their evidence.

Research only. Reads the M10c hourly dataset and the deployed definition, builds 4h and daily
bars from the hourly ones, writes under ``var/research/m14/``. Touches no paper state, no
production risk configuration and no running session.

One job per (strategy, timeframe). Each runs the full year under every latch policy and the
research reference, then walk-forward, in/out-of-sample, stress and the declared neighbours
under the reference. Everything it runs was declared in :mod:`quantplatform.research.m14`.

Usage::

    uv run python scripts/m14_study.py [--workers 6] [--only regime_trend] [--timeframes 4h,1d]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m13_sprint import DEPLOYED, ROOT, SliceLoader, _card, _s, in_window, load_bars

from quantplatform.core.enums import Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import (
    ExperimentEngineFactory,
    code_revision,
    load_definition,
)
from quantplatform.research.definition import ExperimentDefinition, ExperimentRole
from quantplatform.research.latch_policy import (
    LatchPolicy,
    LatchPolicyRiskEngine,
    blocked_time_share,
)
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.m14 import (
    POLICIES,
    REFERENCE,
    STUDY_STRATEGIES,
    TIMEFRAMES,
    WALK_FORWARD_TIMEFRAMES,
    study_definition,
)
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.resample import resample_bars
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sensitivity import (
    SensitivityPlan,
    SensitivityRunner,
    SensitivityVariation,
)
from quantplatform.research.sprint import (
    IN_SAMPLE_WINDOW,
    OUT_OF_SAMPLE_WINDOW,
    STRESS_LABELS,
    Family,
    narrowed,
    stress_scenarios,
    walk_forward_plan,
)
from quantplatform.research.store import ResultStore
from quantplatform.research.stress import StressPlan, StressRunner
from quantplatform.strategies.research import build_research_registry

OUT = ROOT / "var/research/m14"


def bars_for(timeframe: Timeframe) -> tuple[MarketBar, ...]:
    """Return the dataset at ``timeframe``, built exactly from the hourly bars."""
    hourly = load_bars()
    return hourly if timeframe is Timeframe.H1 else resample_bars(hourly, timeframe)


def _factory(captured: list[LatchPolicyRiskEngine], policy: LatchPolicy) -> ExperimentEngineFactory:
    """Build engines whose risk engine runs ``policy``, keeping each one for its stats."""

    def risk_engine_for(definition: ExperimentDefinition) -> LatchPolicyRiskEngine:
        engine = LatchPolicyRiskEngine(config=definition.risk, policy=policy)
        captured.append(engine)
        return engine

    return ExperimentEngineFactory(
        registry=build_research_registry(),
        features_for=features_for,
        quote_asset="USDT",
        risk_engine_for=risk_engine_for,
    )


def run_job(strategy_index: int, timeframe_value: str) -> dict[str, Any]:
    """Run every declared plan for one strategy on one timeframe. Executed in a worker."""
    started = time.time()
    candidate = STUDY_STRATEGIES[strategy_index]
    timeframe = Timeframe(timeframe_value)
    bars = bars_for(timeframe)
    base = load_definition(DEPLOYED)
    revision = code_revision(ROOT)
    home = OUT / candidate.strategy_id / timeframe.value
    home.mkdir(parents=True, exist_ok=True)
    store = ResultStore(home / "results")
    ledger = ExperimentLedger(home / "ledger.jsonl")
    runner = ExperimentRunner()
    start, end = base.dataset.start, base.dataset.end
    evidence: dict[str, Any] = {
        "strategy_id": candidate.strategy_id,
        "family": candidate.family.value,
        "timeframe": timeframe.value,
        "params": dict(candidate.params),
        "bars": len(bars),
        "code_revision": revision,
        "policies": {},
    }

    # --- Part A (and Part B's full-year rows): every policy, and the reference ---------------
    for policy in (REFERENCE, *POLICIES):
        definition = study_definition(candidate, base=base, timeframe=timeframe, policy=policy)
        captured: list[LatchPolicyRiskEngine] = []
        result = runner.run(
            definition, bars=bars, factory=_factory(captured, policy), code_revision=revision
        )
        ledger.record(result, store=store)
        stats = captured[-1].stats if captured else None
        evidence["policies"][policy.key] = {
            "label": policy.label,
            "experiment_id": definition.experiment_id,
            "status": result.status.value,
            "error": result.error,
            "card": _card(result),
            "assessed": stats.assessed if stats else None,
            "blocked": stats.blocked if stats else None,
            "blocked_share": (stats.blocked / stats.assessed) if stats and stats.assessed else None,
            "blocked_time_share": blocked_time_share(stats, start=start, end=end)
            if stats
            else None,
            "pauses": len(stats.pauses) if stats else None,
            "first_pause": stats.pauses[0][0] if stats and stats.pauses else None,
            "trips": sorted({(r.value, at) for r, at in stats.trips}) if stats else [],
        }

    # --- Part B edge evidence, all under the research reference ------------------------------
    reference = study_definition(candidate, base=base, timeframe=timeframe, policy=REFERENCE)
    plain = ExperimentEngineFactory(
        registry=build_research_registry(), features_for=features_for, quote_asset="USDT"
    )
    stress = StressRunner(runner=runner).run(
        reference,
        StressPlan(
            base_experiment_id=reference.experiment_id, scenarios=stress_scenarios(reference)
        ),
        bars=bars,
        factory=plain,
        store=store,
        ledger=ledger,
        code_revision=revision,
    )
    evidence["stress"] = [
        {"label": label, "card": _card(run.result)}
        for label, run in zip(STRESS_LABELS, stress.scenarios, strict=False)
    ]
    # The reference just ran twice — once through the wrapper, once plain, as the stress
    # baseline. The ledger compares the two: a mismatch would be recorded as a failure.
    evidence["reference_wrapper_equals_plain"] = (
        _card(stress.baseline.result) == evidence["policies"][REFERENCE.key]["card"]
    )

    for key, window, role in (
        ("in_sample", IN_SAMPLE_WINDOW, ExperimentRole.IN_SAMPLE),
        ("out_of_sample", OUT_OF_SAMPLE_WINDOW, ExperimentRole.OUT_OF_SAMPLE),
    ):
        claimed = ExperimentRole.BENCHMARK if candidate.family is Family.BENCHMARK else role
        definition = narrowed(reference, window, claimed)
        result = runner.run(
            definition, bars=in_window(bars, window), factory=plain, code_revision=revision
        )
        ledger.record(result, store=store)
        evidence[key] = _card(result)

    if timeframe in WALK_FORWARD_TIMEFRAMES:
        outcome = WalkForwardRunner(runner=runner).run(
            reference,
            walk_forward_plan(reference),
            loader=SliceLoader(bars),
            factory=plain,
            store=store,
            ledger=ledger,
            code_revision=revision,
        )
        evidence["walk_forward_folds"] = [
            {
                "index": run.entry.fold_index,
                "role": run.result.definition.role.value,
                "card": _card(run.result),
            }
            for run in outcome.folds
        ]
        evidence["walk_forward_aborted"] = outcome.aborted
        if not outcome.aborted:
            evidence["walk_forward"] = outcome.summarise(require_complete=False).model_dump()
    else:
        evidence["walk_forward"] = None
        evidence["walk_forward_note"] = (
            "not measurable: 45-day windows hold fewer daily bars than warm-up"
        )

    if candidate.neighbours:
        sens = SensitivityRunner(runner=runner).run(
            reference,
            SensitivityPlan(
                base_experiment_id=reference.experiment_id,
                variations=tuple(SensitivityVariation(params=n) for n in candidate.neighbours),
            ),
            bars=bars,
            factory=plain,
            store=store,
            ledger=ledger,
            code_revision=revision,
        )
        evidence["neighbours"] = [
            {"params": dict(n), "card": _card(run.result)}
            for n, run in zip(candidate.neighbours, sens.variations, strict=False)
        ]

    evidence["seconds"] = round(time.time() - started, 1)
    payload: dict[str, Any] = _s(evidence)  # type: ignore[assignment]
    (home / "evidence.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _say(text: str) -> None:
    """Write one progress line and flush it."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run the requested (strategy, timeframe) jobs in parallel, slowest first."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="")
    parser.add_argument("--timeframes", default=",".join(tf.value for tf in TIMEFRAMES))
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    frames = [Timeframe(v) for v in args.timeframes.split(",") if v]
    jobs = [
        (i, tf.value)
        for tf in frames
        for i, c in enumerate(STUDY_STRATEGIES)
        if not wanted or c.strategy_id in wanted
    ]
    # Hourly jobs dominate the run time, so they start first.
    jobs.sort(key=lambda job: Timeframe(job[1]).seconds)
    OUT.mkdir(parents=True, exist_ok=True)

    started = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_job, i, tf): (i, tf) for i, tf in jobs}
        for future in as_completed(futures):
            i, tf = futures[future]
            name = STUDY_STRATEGIES[i].strategy_id
            try:
                payload = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                _say(f"{name} {tf}: FAILED {type(exc).__name__}: {exc}")
                continue
            done += 1
            ref = payload["policies"][REFERENCE.key].get("card") or {}
            _say(
                f"[{time.time() - started:6.0f}s] {done:2}/{len(jobs)} {name:22} {tf:3} "
                f"ref return={ref.get('total_return')} trades={ref.get('trades')} "
                f"({payload['seconds']:.0f}s)"
            )
    _say(f"\nfinished {done}/{len(jobs)} jobs after {time.time() - started:.0f}s")
    return 0 if done == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
