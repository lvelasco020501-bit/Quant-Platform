"""Ablate the time stop from Risk V2 and measure what exactly it was doing.

Two arms per variant: Risk V2 as deployed at 1D, and Risk V2 with ``max_holding_bars`` cleared.
Nothing else differs -- the price stop, break-even, trailing, take profit, breakers and sizing
are the same objects in both, and the override comes from M37's own table rather than being
retyped here.

Both arms run through the measurement path M39 built, so a difference between them is a
difference in that one field. The full arm should reproduce M39's screen exactly for these two
variants, and the script checks that: if it does not, the two milestones are not measuring the
same thing and no attribution below would mean anything.

Usage:
    uv run python scripts/m40_timestop.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.definition import ExperimentDefinition, canonical_json
from quantplatform.research.m37_definitions import OVERRIDES
from quantplatform.research.m37_probe import AblationRiskEngine
from quantplatform.research.m37_probe import re_entries as count_re_entries
from quantplatform.research.m39 import TIMEFRAME, Variant
from quantplatform.research.m39_definitions import definition_for
from quantplatform.research.m40 import (
    ABLATED,
    ASSETS_M39,
    CLEAR_IMPROVEMENT,
    MATERIAL_DRAWDOWN_INCREASE,
    STUDIED,
    Arm,
    CausalCheck,
    Side,
    causal,
    establishes_cause,
)
from quantplatform.research.portfolio import Allocation, Holding, positions
from quantplatform.research.rotation import align
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sleeve import Interval, long_mask
from quantplatform.risk.config import RiskConfiguration
from quantplatform.strategies.research import build_research_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m39_screen import SLEEVE, Panel, _card, _eligible, _signal_masks, load

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "var/research/m40"
M39_REPORT: Final[Path] = ROOT / "var/research/m39/screen_1d.json"


def _risk_for(arm: str, base: RiskConfiguration) -> RiskConfiguration:
    """Return the arm's risk configuration: the deployed one, or it minus the time stop."""
    if arm == Arm.FULL:
        return base
    return RiskConfiguration.model_validate({**base.model_dump(), **OVERRIDES[ABLATED]})


def _run(raw: str, variant: Variant, arm: str) -> dict[str, Any]:
    """Run one market under one arm through the full chain."""
    at = time.time()
    bars = load(raw)
    base = definition_for(raw, variant)
    # model_copy plus a canonical-JSON round trip, not model_dump: SymbolRules exposes computed
    # precisions and forbids them as input, so dumping and re-validating a definition fails.
    # This is the same path every definition builder in the project already uses.
    definition = ExperimentDefinition.model_validate_json(
        canonical_json(
            base.model_copy(
                update={
                    "name": f"m40-{raw}-{variant.key}-{arm}",
                    "risk": _risk_for(arm, base.risk),
                }
            )
        )
    )
    captured: list[AblationRiskEngine] = []

    def _engine(_: ExperimentDefinition) -> AblationRiskEngine:
        engine = AblationRiskEngine(config=definition.risk)
        captured.append(engine)
        return engine

    factory = ExperimentEngineFactory(
        registry=build_research_registry(),
        features_for=features_for,
        quote_asset="USDT",
        risk_engine_for=_engine,
    )
    result = ExperimentRunner().run(
        definition, bars=bars, factory=factory, code_revision="m40-timestop"
    )
    tally = captured[-1].tally if captured else None
    return {
        "market": raw,
        "arm": arm,
        "status": result.status.value,
        "trades": len(result.trades),
        "seconds": round(time.time() - at, 1),
        "intervals": [
            {"opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat()}
            for t in sorted(result.trades, key=lambda t: t.opened_at)
        ],
        "exits_by_code": dict(tally.by_code) if tally else {},
        "forced_exits": tally.forced_exits if tally else 0,
        "error": result.error,
    }


def _masks(runs: list[dict[str, Any]], panel: Panel) -> dict[Holding, tuple[bool, ...]]:
    """Return the timelines one arm's positions occupied."""
    out: dict[Holding, tuple[bool, ...]] = {}
    for row in sorted(runs, key=lambda r: r["market"]):
        if row["status"] != "succeeded":
            continue
        intervals = tuple(
            Interval(
                entered_at=datetime.fromisoformat(span["opened_at"]),
                exited_at=datetime.fromisoformat(span["closed_at"]),
            )
            for span in row["intervals"]
        )
        out[SLEEVE, row["market"]] = long_mask(intervals, panel.grid)
    return out


def _one_arm(
    variant: Variant, arm: str, panel: Panel, signal: dict[Holding, tuple[bool, ...]]
) -> tuple[Side, dict[str, Any]]:
    """Run one variant under one arm on every market and fold it into the declared record."""
    runs = [_run(raw, variant, arm) for raw in sorted(panel.series)]
    masks = _masks(runs, panel)
    card = _card(masks, panel)
    codes: Counter[str] = Counter()
    for row in runs:
        codes.update(row["exits_by_code"])
    shared = [h for h in masks if h in signal]
    wanted = sum(sum(signal[h]) for h in shared)
    covered = sum(
        sum(1 for a, b in zip(signal[h], masks[h], strict=True) if a and b) for h in shared
    )
    side = Side(
        arm=arm,
        key=variant.key,
        annual=card["cagr"],
        max_drawdown=card["max_drawdown"],
        calmar_ratio=card["calmar"],
        profit_factor=card["profit_factor"],
        trades=sum(r["trades"] for r in runs),
        forced_exits=sum(r["forced_exits"] for r in runs),
        time_stop_exits=codes.get("time_stop", 0),
        turnover=card["turnover"],
        fees=card["fees"],
        held_share_of_wanted=(Decimal(covered) / Decimal(wanted) if wanted else None),
        out_of_sample_return=card["oos_return"],
        annual_at_double_cost=card["cagr_x2"],
        annual_at_triple_cost=card["cagr_x3"],
        years_positive_share=card["years_positive_share"],
        single_year_share=card["single_year_share"],
    )
    detail = {
        "arm": arm,
        "exits_by_code": dict(codes),
        "re_entries": sum(count_re_entries(masks[h], signal[h]) for h in shared),
        "card": card,
        "per_market": [{k: v for k, v in r.items() if k != "intervals"} for r in runs],
        "failures": {
            r["market"]: r["error"] or r["status"] for r in runs if r["status"] != "succeeded"
        },
    }
    return side, detail


def _verify_reproduces_m39(side: Side) -> str:
    """Return whether the full arm matches M39's screen for this variant.

    The full arm is the configuration M39 already ran. If it does not reproduce M39's figures,
    the two milestones are measuring different things and the attribution is void -- so this is
    checked and printed rather than assumed.
    """
    if not M39_REPORT.exists():
        return "m39 report absent"
    m39 = json.loads(M39_REPORT.read_text(encoding="utf-8"))["variants"].get(side.key)
    if m39 is None or m39.get("risk_v2") is None:
        return "not in m39"
    before = Decimal(str(m39["risk_v2"]["cagr"]))
    after = side.annual if side.annual is not None else Decimal(0)
    exits = m39["exits_by_code"].get("time_stop", 0)
    same = before == after and exits == side.time_stop_exits
    return "reproduces M39" if same else f"DIFFERS from M39 (cagr {before} vs {after})"


def _row(side: Side) -> str:
    """Return one printed table row."""
    return (
        f"  {side.arm:22} {float(side.annual or 0):+7.2%} {float(side.max_drawdown):6.2%} "
        f"{float(side.calmar_ratio or 0):6.2f} {float(side.profit_factor or 0):5.2f} "
        f"{side.trades:6d} {side.forced_exits:7d} {side.time_stop_exits:6d} "
        f"{float(side.turnover):7.1f} {float(side.fees):8.0f} "
        f"{float(side.held_share_of_wanted or 0):6.1%} "
        f"{float(side.out_of_sample_return or 0):+7.2%} "
        f"{float(side.annual_at_double_cost or 0):+7.2%} "
        f"{float(side.annual_at_triple_cost or 0):+7.2%} "
        f"{float(side.years_positive_share or 0):5.0%}"
    )


def main() -> int:
    """Measure both arms for both studied variants and judge causality."""
    series = {raw: load(raw) for raw in ASSETS_M39}
    grid = align(series)
    slots = positions(series, grid)
    panel = Panel(
        series=series,
        grid=grid,
        universe=_eligible(slots, series, grid),
        allocation=Allocation(universe_size=len(ASSETS_M39), sleeves=1),
    )
    sys.stdout.write(
        f"M40 phase 1 -- time-stop ablation, {len(series)} markets, {TIMEFRAME.value}, "
        f"grid {len(grid)} bars {grid[0].date()} -> {grid[-1].date()}\n"
        f"one field changes: {ABLATED.value} -> {OVERRIDES[ABLATED]}\n"
        f"clear improvement = {CLEAR_IMPROVEMENT}, tolerated drawdown increase = "
        f"{MATERIAL_DRAWDOWN_INCREASE}\n\n"
    )

    header = (
        f"  {'ARM':22} {'CAGR':>8} {'DD':>7} {'CALMAR':>6} {'PF':>5} {'TRADE':>6} "
        f"{'FORCED':>7} {'TSTOP':>6} {'TURN':>7} {'FEES':>8} {'HELD%':>6} {'OOS':>8} "
        f"{'x2':>8} {'x3':>8} {'YRS+':>5}"
    )
    report: dict[str, Any] = {
        "milestone": "m40",
        "phase": 1,
        "timeframe": TIMEFRAME.value,
        "universe": list(ASSETS_M39),
        "ablated": ABLATED.value,
        "override": OVERRIDES[ABLATED],
        "clear_improvement": CLEAR_IMPROVEMENT,
        "material_drawdown_increase": MATERIAL_DRAWDOWN_INCREASE,
        "generated_at": datetime.now(UTC).isoformat(),
        "variants": {},
    }

    checks: list[CausalCheck] = []
    for variant in sorted(STUDIED, key=lambda v: v.key):
        signal = _signal_masks(variant, panel)
        sides: dict[str, Side] = {}
        details: dict[str, Any] = {}
        sys.stdout.write(f"{variant.key} ({variant.family.value})\n{header}\n")
        for arm in (Arm.FULL, Arm.NO_TIME_STOP):
            side, detail = _one_arm(variant, arm, panel, signal)
            sides[arm] = side
            details[arm] = detail
            sys.stdout.write(_row(side) + "\n")
            sys.stdout.flush()
        check = CausalCheck(key=variant.key, full=sides[Arm.FULL], ablated=sides[Arm.NO_TIME_STOP])
        checks.append(check)
        ok, failed = causal(check)
        provenance = _verify_reproduces_m39(sides[Arm.FULL])
        sys.stdout.write(
            f"  full arm vs M39: {provenance}\n"
            f"  cagr gain {float(check.relative_cagr_gain or 0):+.1%}  "
            f"calmar gain {float(check.relative_calmar_gain or 0):+.1%}  "
            f"drawdown change {float(check.drawdown_increase):+.2%}\n"
            f"  causal for {variant.key}: {'YES' if ok else 'NO'}"
            f"{'' if ok else ' -> ' + ', '.join(r.value for r in failed)}\n\n"
        )
        report["variants"][variant.key] = {
            "family": variant.family.value,
            "params": dict(variant.params),
            "reproduces_m39": provenance,
            "relative_cagr_gain": check.relative_cagr_gain,
            "relative_calmar_gain": check.relative_calmar_gain,
            "drawdown_increase": check.drawdown_increase,
            "causal": ok,
            "failed": [r.value for r in failed],
            "arms": {
                arm: {"side": side.model_dump(), **details[arm]} for arm, side in sides.items()
            },
        }

    established = establishes_cause(tuple(checks))
    report["establishes_cause"] = established
    HOME.mkdir(parents=True, exist_ok=True)
    out = HOME / "timestop_ablation_1d.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(f"-> {out.relative_to(ROOT)}\n")
    sys.stdout.write(
        "PHASE 1 PASSES: the time stop is established as the cause across both variants\n"
        if established
        else "STOP: the time stop is not established as the cause; phase 2 does not open\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
