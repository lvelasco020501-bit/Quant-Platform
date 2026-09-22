"""Turn M19's evidence into the comparison A / C / E / G / H / I / J and a GO / NO-GO.

Reads ``var/research/m19/<ASSET>/<POLICY>/evidence.json`` for H, I and J, and
``var/research/m18/<ASSET>/<POLICY>/evidence.json`` for C, E and G, which were run over the
identical four scenarios. Every figure is recomputed here from the stored halt episodes and
scorecards, so a policy measured in M18 and one measured in M19 are measured the same way.

Policy A (deployed Risk V2) is cited from M17 rather than re-run: it is production, not a
candidate, and its behaviour — 48% to 90% of every market's history blocked — is not in doubt.

Gates come from :mod:`quantplatform.research.m19`, fixed before the runs. Return is not an
input to any of them and this script never ranks by it.

Usage::

    uv run python scripts/m19_report.py
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.m16 import ASSETS
from quantplatform.research.m18 import flapping_share, ratchet_chains
from quantplatform.research.m19 import (
    GLOBAL_CAP,
    POLICIES_M19,
    Observed,
    judge_m19,
    local_overshoot,
)

ROOT = Path(__file__).resolve().parents[1]
M18 = ROOT / "var/research/m18"
M19 = ROOT / "var/research/m19"
OUT = M19
DASH = "—"
CANONICAL = Decimal("0.10")
PROBE = Decimal("0.05")
CARRIED = ("BTCUSDT", "SOLUSDT")
"""Markets whose breaker never engages in any scenario, so a budget cannot change them."""

BUDGETED = {policy.key for policy in POLICIES_M19}
PAIR = 2
"""Two halts: the fewest that can form a chain."""
Run = dict[str, Any]


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


def load() -> dict[str, list[Run]]:
    """Return every run, grouped by policy, with each figure recomputed the same way."""
    grouped: dict[str, list[Run]] = {}
    sources = [(M18, key) for key in ("C", "E", "G")] + [(M19, key) for key in ("H", "I", "J")]
    for home, key in sources:
        for asset in ASSETS:
            evidence = _json(home / asset.raw / key / "evidence.json")
            if evidence is None:
                continue
            for run in evidence.get("runs") or []:
                episodes = run.get("episodes") or []
                card = run.get("card") or {}
                ended = [e for e in episodes if e.get("ended")]
                chains = ratchet_chains(episodes)
                permanent = bool(episodes) and episodes[-1].get("ended") is None
                spent = bool(run.get("budget_exhausted"))
                grouped.setdefault(key, []).append(
                    {
                        "raw": asset.raw,
                        "symbol": asset.symbol,
                        "policy": key,
                        "label": run["label"],
                        "threshold": _d(run.get("threshold")),
                        "trades": card.get("trades"),
                        "net": _d(card.get("total_return")),
                        "global_drawdown": _d(card.get("max_drawdown")),
                        "local_overshoot": local_overshoot(episodes),
                        "blocked": _d(run.get("blocked_time_share")) or Decimal(0),
                        "halts": len(episodes),
                        "reopenings": len(ended),
                        "effective": int(run.get("effective_reopenings") or 0),
                        "resets": sum(1 for e in episodes if e.get("reference_after") is not None),
                        "chains": chains,
                        "longest_chain": max((c["halts"] for c in chains), default=0),
                        "worst_chain": max(
                            (c["decline"] for c in chains if c["decline"] is not None),
                            default=None,
                        ),
                        "flapping": flapping_share(episodes),
                        "permanent": permanent,
                        "budget_spent": spent,
                        "permanent_without_cause": permanent and not spent,
                    }
                )
    return grouped


def swing_for(runs: list[Run]) -> Decimal | None:
    """Return the worst trade-count move across the probe's two fee settings, per market."""
    worst: Decimal | None = None
    by_market: dict[str, list[int]] = {}
    for run in runs:
        if "probe" not in run["label"] or run["trades"] is None:
            continue
        by_market.setdefault(run["raw"], []).append(int(run["trades"]))
    for counts in by_market.values():
        if not counts or max(counts) == 0:
            continue
        moved = Decimal(max(counts) - min(counts)) / Decimal(max(counts))
        worst = moved if worst is None else max(worst, moved)
    return worst


def summarise(runs: list[Run], threshold: Decimal, *, swing: Decimal | None) -> dict[str, Any]:
    """Return one policy's behaviour at one local limit, and whether it met the gates."""
    scoped = [run for run in runs if run["threshold"] == threshold]
    observed = Observed(
        local_limit=threshold,
        worst_local=max(
            (r["local_overshoot"] for r in scoped if r["local_overshoot"] is not None), default=None
        ),
        worst_global=max(
            (r["global_drawdown"] for r in scoped if r["global_drawdown"] is not None), default=None
        ),
        halts=sum(r["halts"] for r in scoped),
        effective=sum(r["effective"] for r in scoped),
        flapping=max((r["flapping"] for r in scoped if r["flapping"] is not None), default=None),
        worst_chain_decline=max(
            (r["worst_chain"] for r in scoped if r["worst_chain"] is not None), default=None
        ),
        permanent_halts=sum(1 for r in scoped if r["permanent"]),
        permanent_without_cause=sum(1 for r in scoped if r["permanent_without_cause"]),
        budget_bound=(
            any(r["budget_spent"] for r in scoped)
            if scoped and scoped[0]["policy"] in BUDGETED
            else None
        ),
        swing=swing,
    )
    passed, gates = judge_m19(observed)
    return {
        "threshold": threshold,
        "observed": observed.model_dump(),
        "reopenings": sum(r["reopenings"] for r in scoped),
        "resets": sum(r["resets"] for r in scoped),
        "worst_blocked": max((r["blocked"] for r in scoped), default=Decimal(0)),
        "longest_chain": max((r["longest_chain"] for r in scoped), default=0),
        "passed": passed,
        "gates": [gate.model_dump() for gate in gates],
    }


def section_rules() -> list[str]:
    """Render what each policy does, in one line each."""
    return [
        "#### 0. The policies",
        "",
        "| POLICY | RULE | ROLE |",
        "|---|---|---|",
        "| A | production Risk V2: five losses or 20% drawdown, both permanent | reference (M17) |",
        "| C | 10% drawdown, permanent latch | reference |",
        "| E | 10% drawdown, 30-day cooldown, reference carried over | reference |",
        "| G | 10% drawdown, 30-day cooldown, reference restarts at the reopening | reference |",
        "| H | G, plus a 20% cap on the loss from the original peak | candidate |",
        "| I | G, plus two local resets per rolling year, then the latch is final | candidate |",
        "| J | G, plus both the cap and the allowance | candidate |",
        "",
        "H, I and J share G's local limit and cooldown exactly, so every difference below is "
        "the budget and nothing else.",
        "",
    ]


def section_table(summaries: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    """Render the table the milestone asked for."""
    lines = [
        "#### 1. The table",
        "",
        "| POLICY | LIMIT | HALTS | REAL REOPENINGS | BLOCKED % | MAX DD | GLOBAL DD | RESETS "
        "| WORST RESET CHAIN | OPERABILITY | VERDICT |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, by_threshold in summaries.items():
        for limit, row in by_threshold.items():
            seen = row["observed"]
            halts, effective = seen["halts"], seen["effective"]
            operability = (
                DASH if not row["reopenings"] else f"{Decimal(effective) / row['reopenings']:.0%}"
            )
            decline = pct(seen["worst_chain_decline"], signed=False)
            chain = (
                "none"
                if row["longest_chain"] < PAIR
                else f"{row['longest_chain']} halts, {decline}"
            )
            lines.append(
                f"| {key} | {limit} | {halts} | {effective}/{row['reopenings']} "
                f"| worst {pct(row['worst_blocked'], signed=False)} "
                f"| worst {pct(seen['worst_local'], signed=False)} "
                f"| worst {pct(seen['worst_global'], signed=False)} | {row['resets']} "
                f"| {chain} | {operability} "
                f"| {'**PASS**' if row['passed'] else 'FAIL'} |"
            )
    lines += [
        "",
        "MAX DD is the deepest loss any *single* halt allowed, measured from that halt's own "
        "reference — it says whether the breaker acted promptly. GLOBAL DD is the deepest loss "
        "from the **original** high-water mark, which is what a budget has to bound. Figures are "
        "the worst across every market and scenario at that limit.",
        "",
    ]
    return lines


def section_gates(summaries: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    """Render every gate for the candidates, and for G as the thing they must improve on."""
    lines = ["#### 2. The gates", ""]
    for key in ("G", "H", "I", "J"):
        if key not in summaries:
            continue
        lines += [
            f"**Policy {key}**",
            "",
            "| LIMIT | GATE | RESULT | EVIDENCE |",
            "|---|---|---|---|",
        ]
        for limit, row in summaries[key].items():
            for gate in row["gates"]:
                lines.append(
                    f"| {limit} | {gate['name']} | {'✅' if gate['passed'] else '❌'} "
                    f"| {gate['detail']} |"
                )
        lines.append("")
    return lines


def section_markets(grouped: dict[str, list[Run]]) -> list[str]:
    """Render every run of the candidates beside G, market by market."""
    lines = [
        "#### 3. Every run, candidates beside G",
        "",
        "| ASSET | SCENARIO | POLICY | TRADES | NET | GLOBAL DD | HALTS | REOPENED | RESETS "
        "| CHAIN | BUDGET SPENT |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    labels: list[str] = []
    for run in grouped.get("G", []):
        if run["label"] not in labels:
            labels.append(run["label"])
    for asset in ASSETS:
        for label in labels:
            for key in ("G", "H", "I", "J"):
                matching: list[Run] = [
                    r for r in grouped.get(key, []) if r["raw"] == asset.raw and r["label"] == label
                ]
                if not matching:
                    continue
                run = matching[0]
                chain = run["longest_chain"] if run["longest_chain"] >= PAIR else DASH
                lines.append(
                    f"| {asset.symbol} | {label[:28]} | {key} | {run['trades']} "
                    f"| {pct(run['net'])} | {pct(run['global_drawdown'], signed=False)} "
                    f"| {run['halts']} | {run['effective']}/{run['reopenings']} "
                    f"| {run['resets']} | {chain} "
                    f"| {'yes' if run['budget_spent'] else 'no'} |"
                )
    lines += [
        "",
        f"Markets absent from this table ({', '.join(CARRIED)}) never engage the breaker in any "
        "scenario — 0 halts under C, E and G, worst drawdowns 8.32% and 6.32% — so a budget that "
        "only acts after a halt cannot change them. They were not re-run.",
        "",
    ]
    return lines


def section_ratchet(grouped: dict[str, list[Run]]) -> list[str]:
    """Render the chains of lower restarts, and whether the budget contained them."""
    lines = [
        "#### 4. Ratchet, before and after the budget",
        "",
        "| ASSET | SCENARIO | POLICY | CHAIN | DECLINE | GLOBAL DD | INSIDE THE 20% BUDGET |",
        "|---|---|---|---|---|---|---|",
    ]
    found = False
    for key in ("G", "H", "I", "J"):
        for run in grouped.get(key, []):
            for chain in run["chains"]:
                found = True
                decline = _d(chain["decline"])
                inside = decline is not None and decline <= GLOBAL_CAP
                lines.append(
                    f"| {run['symbol']} | {run['label'][:28]} | {key} | {chain['halts']} halts "
                    f"| {pct(decline, signed=False)} "
                    f"| {pct(run['global_drawdown'], signed=False)} "
                    f"| {'yes' if inside else '**no**'} |"
                )
    if not found:
        lines.append(
            f"| {DASH} | {DASH} | {DASH} | no chain of lower restarts | {DASH} | {DASH} | {DASH} |"
        )
    return [*lines, ""]


def main() -> int:
    """Write the report and the machine-readable gate results."""
    grouped = load()
    if not any(key in grouped for key in BUDGETED):
        sys.stderr.write("no M19 evidence found under var/research/m19\n")
        return 1
    summaries: dict[str, dict[str, dict[str, Any]]] = {}
    for key in ("C", "E", "G", "H", "I", "J"):
        runs = grouped.get(key)
        if not runs:
            continue
        swing = swing_for(runs)
        summaries[key] = {
            f"{threshold:.0%}": summarise(runs, threshold, swing=swing)
            for threshold in (CANONICAL, PROBE)
            if any(run["threshold"] == threshold for run in runs)
        }
    lines = [
        *section_rules(),
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
