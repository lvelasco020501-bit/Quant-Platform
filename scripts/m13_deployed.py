"""Scenario A of the M13 sprint: every candidate under the deployed Risk V2, unchanged.

The sprint runner measures edge under the research variant, whose latching breakers do not
latch. That variant is not Risk V2, so it cannot say whether a strategy survives the policy
paper would actually run it under. This pass answers that, and only that: the full year under
the exact deployed configuration, recorded in each candidate's ledger, with a breakdown of
what the risk engine refused and why.

Usage::

    uv run python scripts/m13_deployed.py [--workers 6] [--only momentum_roc,ema_slope]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m13_sprint import DEPLOYED, OUT, ROOT, _card, _s, load_bars

from quantplatform.core.enums import RiskOutcome
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import (
    ExperimentEngineFactory,
    code_revision,
    load_definition,
)
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import (
    CANDIDATES,
    RiskScenario,
    deployed_definition_for,
)
from quantplatform.research.store import ResultStore
from quantplatform.strategies.research import build_research_registry

_LATCHED = "latched"


def run_deployed(index: int) -> dict[str, Any]:
    """Run one candidate under the deployed Risk V2 and describe what it was allowed to do."""
    candidate = CANDIDATES[index]
    started = time.time()
    bars = load_bars()
    registry = build_research_registry()
    factory = ExperimentEngineFactory(
        registry=registry, features_for=features_for, quote_asset="USDT"
    )
    revision = code_revision(ROOT)
    version = registry.metadata_for(candidate.strategy_id).version
    definition = deployed_definition_for(
        candidate, base=load_definition(DEPLOYED), strategy_version=version
    )

    home = OUT / candidate.strategy_id
    home.mkdir(parents=True, exist_ok=True)
    result = ExperimentRunner().run(definition, bars=bars, factory=factory, code_revision=revision)
    ExperimentLedger(home / "ledger.jsonl").record(result, store=ResultStore(home / "results"))

    # The harness result carries no decisions, so the refusal breakdown needs the engine's own.
    raw = factory(definition).run(bars)
    decisions = raw.decisions
    approved = sum(1 for d in decisions if d.outcome is not RiskOutcome.REJECTED)
    latched = [d for d in decisions if any(_LATCHED in r for r in d.rejection_reasons)]
    reasons = collections.Counter(r for d in decisions for r in d.rejection_reasons)
    same = (
        raw.performance is not None
        and result.performance is not None
        and raw.performance.total_return == result.performance.total_return
    )
    evidence = {
        "strategy_id": candidate.strategy_id,
        "scenario": RiskScenario.DEPLOYED.value,
        "scenario_label": RiskScenario.DEPLOYED.label,
        "experiment_id": definition.experiment_id,
        "code_revision": revision,
        "status": result.status.value,
        "error": result.error,
        "card": _card(result),
        "decisions": len(decisions),
        "approved": approved,
        "rejected": len(decisions) - approved,
        "latched_rejections": len(latched),
        "blocked_share": (len(latched) / len(decisions)) if decisions else None,
        "first_latch_at": latched[0].decided_at if latched else None,
        "top_rejection_reasons": reasons.most_common(4),
        "harness_and_engine_agree": same,
        "seconds": round(time.time() - started, 1),
    }
    payload: dict[str, Any] = _s(evidence)  # type: ignore[assignment]
    (home / "deployed.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _say(text: str) -> None:
    """Write one progress line and flush it."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run every requested candidate under the deployed Risk V2."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    indices = [i for i, c in enumerate(CANDIDATES) if not wanted or c.strategy_id in wanted]

    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_deployed, i): i for i in indices}
        for future in as_completed(futures):
            name = CANDIDATES[futures[future]].strategy_id
            try:
                payload = future.result()
            except Exception as exc:  # a crashed candidate is reported, never hidden
                _say(f"{name}: FAILED {type(exc).__name__}: {exc}")
                continue
            card = payload.get("card") or {}
            _say(
                f"[{time.time() - started:6.0f}s] {name:24} deployed: "
                f"return={card.get('total_return')} trades={card.get('trades')} "
                f"blocked={payload['blocked_share']} agree={payload['harness_and_engine_agree']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
