"""Turn M18's evidence into the final verdict on policy G.

Reads ``var/research/m18/<ASSET>/<POLICY>/evidence.json`` and writes
``var/research/m18/REPORT.md`` next to a machine-readable ``gates.json``.

Gates come from :mod:`quantplatform.research.m18`, fixed before the runs: protects, recovers,
does not flap, does not ratchet, stays continuous. They are applied separately at each
drawdown limit, because a 5% breaker and a 10% breaker are allowed different absolute losses
but must behave the same way. Return is not an input to any gate and this script never ranks
by it.

Usage::

    uv run python scripts/m18_report.py
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.m16 import ASSETS
from quantplatform.research.m18 import (
    PROBE_DRAWDOWN,
    judge_policy,
    studied_policies,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m18"
DASH = "—"
CANONICAL = Decimal("0.10")
Runs = list[dict[str, Any]]


def _d(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def pct(value: object, *, signed: bool = True) -> str:
    """Render a fraction as a percentage."""
    number = _d(value)
    if number is None:
        return DASH
    return f"{number * 100:+.2f}%" if signed else f"{number * 100:.2f}%"


def _json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_runs() -> dict[str, Runs]:
    """Return every recorded run, grouped by policy key."""
    grouped: dict[str, Runs] = {}
    for asset in ASSETS:
        for policy in studied_policies():
            evidence = _json(OUT / asset.raw / policy.key / "evidence.json")
            if evidence is None:
                continue
            for run in evidence.get("runs") or []:
                run["symbol"] = asset.symbol
                run["raw"] = asset.raw
                grouped.setdefault(policy.key, []).append(run)
    return grouped


def at_threshold(runs: Runs, threshold: Decimal) -> Runs:
    """Return the runs made under one drawdown limit."""
    return [run for run in runs if _d(run.get("threshold")) == threshold]


def swing_for(runs: Runs) -> Decimal | None:
    """Return the worst trade-count move across the probe's two fee settings, per market."""
    worst: Decimal | None = None
    by_market: dict[str, list[int]] = {}
    for run in runs:
        if "probe" not in run["label"] or run.get("card") is None:
            continue
        by_market.setdefault(run["raw"], []).append(int(run["card"]["trades"]))
    for counts in by_market.values():
        if not counts or max(counts) == 0:
            continue
        moved = Decimal(max(counts) - min(counts)) / Decimal(max(counts))
        worst = moved if worst is None else max(worst, moved)
    return worst


def summarise(runs: Runs, threshold: Decimal, *, swing: Decimal | None) -> dict[str, Any]:
    """Return one policy's behaviour at one drawdown limit, and whether it met the gates."""
    scoped = at_threshold(runs, threshold)
    halts = sum(int(run["halts"]) for run in scoped)
    reopenings = sum(int(run["reopenings"]) for run in scoped)
    effective = sum(int(run["effective_reopenings"]) for run in scoped)
    drawdowns = [_d(run["card"]["max_drawdown"]) for run in scoped if run.get("card") is not None]
    blocked = [_d(run["blocked_time_share"]) or Decimal(0) for run in scoped]
    flapping = [value for run in scoped if (value := _d(run.get("flapping_share"))) is not None]
    chains = max((int(run["longest_ratchet_chain"]) for run in scoped), default=0)
    declines = [_d(run["worst_chain_decline"]) for run in scoped if run.get("worst_chain_decline")]
    worst_drawdown = max([d for d in drawdowns if d is not None], default=None)
    worst_decline = max([d for d in declines if d is not None], default=None)
    worst_flapping = max(flapping) if flapping else None
    passed, gates = judge_policy(
        threshold=threshold,
        worst_drawdown=worst_drawdown,
        halts=halts,
        effective=effective,
        flapping=worst_flapping,
        longest_chain=chains,
        worst_chain_decline=worst_decline,
        swing=swing,
    )
    return {
        "threshold": threshold,
        "halts": halts,
        "reopenings": reopenings,
        "effective": effective,
        "worst_blocked": max(blocked, default=Decimal(0)),
        "worst_drawdown": worst_drawdown,
        "longest_halt_days": max((float(run["longest_halt_days"]) for run in scoped), default=0.0),
        "longest_chain": chains,
        "worst_chain_decline": worst_decline,
        "flapping": worst_flapping,
        "swing": swing,
        "passed": passed,
        "gates": [gate.model_dump() for gate in gates],
    }


def section_table(summaries: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    """Render the table the milestone asked for."""
    lines = [
        "#### 1. The table",
        "",
        "| POLICY | LIMIT | HALTS | REAL REOPENINGS | BLOCKED % | MAX DD | WORST RATCHET "
        "| STABILITY | VERDICT |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, by_threshold in summaries.items():
        for threshold, row in by_threshold.items():
            ratchet = (
                f"{row['longest_chain']} halts"
                + (
                    ""
                    if row["worst_chain_decline"] is None
                    else f", {pct(row['worst_chain_decline'], signed=False)}"
                )
                if row["longest_chain"] > 1
                else "none"
            )
            lines.append(
                f"| {key} | {threshold} | {row['halts']} "
                f"| {row['effective']}/{row['reopenings']} "
                f"| worst {pct(row['worst_blocked'], signed=False)} "
                f"| worst {pct(row['worst_drawdown'], signed=False)} | {ratchet} "
                f"| {pct(row['swing'], signed=False)} "
                f"| {'PASS' if row['passed'] else 'FAIL'} |"
            )
    lines += [
        "",
        "HALTS, REAL REOPENINGS and the worst figures are taken across every market and every "
        "scenario run at that limit. STABILITY is the largest move in trade count between the "
        "probe's two fee settings, on any single market.",
        "",
    ]
    return lines


def section_gates(summaries: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    """Render each gate, policy by policy."""
    lines = ["#### 2. The gates, one by one", ""]
    for key, by_threshold in summaries.items():
        lines.append(f"**Policy {key}**")
        lines.append("")
        lines.append("| LIMIT | GATE | RESULT | EVIDENCE |")
        lines.append("|---|---|---|---|")
        for threshold, row in by_threshold.items():
            for gate in row["gates"]:
                mark = "✅" if gate["passed"] else "❌"
                lines.append(f"| {threshold} | {gate['name']} | {mark} | {gate['detail']} |")
        lines.append("")
    return lines


def section_markets(grouped: dict[str, Runs]) -> list[str]:
    """Render every run: market, scenario, and what the halts did."""
    lines = [
        "#### 3. Every run",
        "",
        "| ASSET | POLICY | SCENARIO | TRADES | NET | MAX DD | HALTS | REOPENED | BLOCKED "
        "| LONGEST HALT | RATCHET |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, runs in grouped.items():
        for run in runs:
            card = run.get("card") or {}
            chain = int(run["longest_ratchet_chain"])
            lines.append(
                f"| {run['symbol']} | {key} | {run['label']} "
                f"| {card.get('trades', DASH)} | {pct(card.get('total_return'))} "
                f"| {pct(card.get('max_drawdown'), signed=False)} | {run['halts']} "
                f"| {run['effective_reopenings']}/{run['reopenings']} "
                f"| {pct(run['blocked_time_share'], signed=False)} "
                f"| {float(run['longest_halt_days']):.0f}d "
                f"| {chain if chain > 1 else 'none'} |"
            )
    return [*lines, ""]


def section_ratchet(grouped: dict[str, Runs]) -> list[str]:
    """Render every chain of lower restarts that was actually found."""
    lines = ["#### 4. Ratchet chains found", ""]
    found = False
    for key, runs in grouped.items():
        for run in runs:
            for chain in run.get("ratchet_chains") or []:
                found = True
                lines.append(
                    f"* **{run['symbol']} · {key} · {run['label']}** — {chain['halts']} "
                    f"consecutive lower restarts, giving up "
                    f"{pct(chain['decline'], signed=False)} from where the chain began "
                    f"({str(chain['from'])[:10]} → {str(chain['to'])[:10]})."
                )
    if not found:
        lines.append("No chain of consecutive lower restarts occurred in any run.")
    return [*lines, ""]


def main() -> int:
    """Write the report and the machine-readable gate results."""
    grouped = load_runs()
    if not grouped:
        sys.stderr.write("no evidence found under var/research/m18\n")
        return 1
    summaries: dict[str, dict[str, dict[str, Any]]] = {}
    for key, runs in grouped.items():
        swing = swing_for(runs)
        summaries[key] = {}
        for threshold in (CANONICAL, PROBE_DRAWDOWN):
            if at_threshold(runs, threshold):
                summaries[key][f"{threshold:.0%}"] = summarise(runs, threshold, swing=swing)
    lines = [
        *section_table(summaries),
        *section_gates(summaries),
        *section_markets(grouped),
        *section_ratchet(grouped),
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "gates.json").write_text(json.dumps(summaries, indent=2, default=str), encoding="utf-8")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
