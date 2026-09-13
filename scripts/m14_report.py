"""Tabulate M14 and apply its decision rules — rules written before any result existed.

Reads ``var/research/m14/<strategy>/<timeframe>/evidence.json``. Every market figure comes
from the harness. Three things are decided here, each by a rule stated in this file:

* each row's verdict, by :func:`quantplatform.research.sprint.judge`;
* whether a latch policy reduces losses or merely stops trading, by
  :func:`quantplatform.research.m14.classify_protection`;
* the recommended latch policy and the two combinations worth following
  (:func:`recommend_policy`, :func:`top_combinations`).

None of them looks at return first. Return enters the verdict only as "above zero" and "above
the benchmarks", and the policy recommendation not at all.
"""

from __future__ import annotations

import json
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.m14 import (
    POLICIES,
    REFERENCE,
    STUDY_STRATEGIES,
    TIMEFRAMES,
    ProtectionEffect,
    classify_protection,
)
from quantplatform.research.sprint import (
    MEANINGFUL_TRADE_SAMPLE,
    STRESS_LABELS,
    Evidence,
    Family,
    Scorecard,
    Verdict,
    judge,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m14"
CAPITAL = Decimal(10_000)
DASH = "—"
PRODUCTION_DRAWDOWN_LIMIT = Decimal("0.20")
_TIER = {
    Verdict.PAPER_CANDIDATE.value: 3,
    Verdict.PROMISING.value: 2,
    Verdict.WEAK.value: 1,
    Verdict.REJECT.value: 0,
}


def _d(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def trades_cell(card: dict[str, Any]) -> str:
    """Render a trade count, flagged when too thin for its ratios to mean much."""
    flag = " LOW" if card["trades"] < MEANINGFUL_TRADE_SAMPLE else ""
    return f"{card['trades']}{flag}"


def pct(value: object, *, signed: bool = True) -> str:
    """Render a fraction as a percentage."""
    d = _d(value)
    if d is None:
        return DASH
    return f"{d * 100:+.2f}%" if signed else f"{d * 100:.2f}%"


def num(value: object) -> str:
    """Render a plain decimal to two places."""
    d = _d(value)
    return DASH if d is None else f"{d:.2f}"


def load() -> dict[tuple[str, str], dict[str, Any]]:
    """Return every job's evidence, keyed by (strategy, timeframe)."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in STUDY_STRATEGIES:
        for timeframe in TIMEFRAMES:
            path = OUT / candidate.strategy_id / timeframe.value / "evidence.json"
            if path.exists():
                found[(candidate.strategy_id, timeframe.value)] = json.loads(path.read_text())
    return found


def costs(card: dict[str, Any]) -> Decimal:
    """Return fees plus slippage as a fraction of starting capital."""
    return (Decimal(card["fees"]) + Decimal(card["slippage"])) / CAPITAL


def gross(card: dict[str, Any]) -> Decimal:
    """Return the result before costs, as a fraction of starting capital."""
    return Decimal(card["total_return"]) + costs(card)


def cost_share(card: dict[str, Any]) -> str:
    """Return costs as a share of gross PnL, or why that is not meaningful."""
    g = gross(card)
    if card["trades"] == 0:
        return DASH
    if g <= 0:
        return "gross ≤ 0"
    return f"{costs(card) / g * 100:.0f}%"


def _test_folds(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    folds = evidence.get("walk_forward_folds") or []
    return sorted((f for f in folds if f["role"] == "walk_forward_test"), key=lambda f: f["index"])


def walk_forward(evidence: dict[str, Any]) -> str:
    """Render positive test windows out of those that ran, or why none could."""
    if evidence.get("walk_forward") is None:
        return "n/a (1d)"
    folds = [f for f in _test_folds(evidence) if f.get("card")]
    positive = sum(1 for f in folds if Decimal(f["card"]["total_return"]) > 0)
    return f"{positive}/{len(folds)}"


def _evidence_for(
    evidence: dict[str, Any], policy_key: str, best_benchmark: Decimal | None
) -> Evidence:
    """Assemble what :func:`judge` may see for one row.

    The row's own policy supplies the full-year scorecard. Out-of-sample, walk-forward,
    stress and neighbours are edge evidence and come from the research reference at the same
    timeframe. ``deployed_return`` is the return under the policy the row would actually run
    under — its own for A-D, and production's (A) for the reference, which is not a policy
    anything would run under.
    """
    policies = evidence["policies"]
    card = policies[policy_key]["card"]
    wf = evidence.get("walk_forward") or {}
    stress = [
        value
        for s in evidence.get("stress", [])
        if s.get("card") and (value := _d(s["card"]["total_return"])) is not None
    ]
    neighbour_pfs: list[Decimal] = []
    for n in evidence.get("neighbours", []):
        ncard = n.get("card")
        if ncard is None or ncard["trades"] == 0:
            neighbour_pfs.append(Decimal(0))
        elif ncard["profit_factor"] is not None:
            neighbour_pfs.append(Decimal(ncard["profit_factor"]))
    if not evidence.get("neighbours"):
        # Benchmarks have no declared neighbours. Absent is not passed: it is recorded as a
        # failed check, so a benchmark can be judged but cannot be promoted on it.
        neighbour_pfs.append(Decimal(0))
    run_under = "A" if policy_key == REFERENCE.key else policy_key
    return Evidence(
        full=Scorecard.model_validate(card),
        out_of_sample_return=_d((evidence.get("out_of_sample") or {}).get("total_return")),
        walk_forward_positive_share=_d(wf.get("positive_share_of_completed")),
        walk_forward_median_return=_d(wf.get("median_return")),
        stress_worst_return=min(stress) if stress else None,
        neighbours_min_profit_factor=min(neighbour_pfs) if neighbour_pfs else None,
        benchmark_best_return=best_benchmark,
        deployed_return=_d(policies[run_under]["card"]["total_return"]),
    )


def rows(data: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one row per (strategy, timeframe, policy), verdict and effect included."""
    out: list[dict[str, Any]] = []
    for timeframe in TIMEFRAMES:
        for policy in (REFERENCE, *POLICIES):
            benchmarks = [
                Decimal(e["policies"][policy.key]["card"]["total_return"])
                for (sid, tf), e in data.items()
                if tf == timeframe.value
                and e["family"] == Family.BENCHMARK.value
                and e["policies"][policy.key]["card"]
            ]
            best = max(benchmarks) if benchmarks else None
            for candidate in STUDY_STRATEGIES:
                evidence = data.get((candidate.strategy_id, timeframe.value))
                if evidence is None or evidence["policies"][policy.key]["card"] is None:
                    continue
                entry = evidence["policies"][policy.key]
                card = entry["card"]
                verdict = judge(_evidence_for(evidence, policy.key, best)).value
                effect = None
                if policy.key != REFERENCE.key:
                    effect = classify_protection(
                        card, evidence["policies"][REFERENCE.key]["card"]
                    ).value
                out.append(
                    {
                        "strategy_id": candidate.strategy_id,
                        "benchmark": candidate.family is Family.BENCHMARK,
                        "timeframe": timeframe.value,
                        "policy": policy.key,
                        "card": card,
                        "gross": gross(card),
                        "costs": costs(card),
                        "blocked_share": _d(entry["blocked_share"]),
                        "blocked_time_share": _d(entry["blocked_time_share"]),
                        "verdict": verdict,
                        "effect": effect,
                        "walk_forward": walk_forward(evidence),
                    }
                )
    return out


def recommend_policy(table: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """Pick a latch policy by safety, then operability. Never by return.

    1. **Safe:** across every combination it ran, its worst drawdown stays within
       production's 20% limit.
    2. **Operable:** it does not merely stop trading — "stops trading" on at most half of the
       combinations where the reference made at least 30 trades.
    3. Among what remains, the lowest median share of time blocked; ties go to the lower
       worst drawdown. Production (A) is eligible like any other.
    """
    notes: list[str] = []
    eligible: list[tuple[Decimal, Decimal, str]] = []
    reference_trades = {
        (r["strategy_id"], r["timeframe"]): r["card"]["trades"]
        for r in table
        if r["policy"] == REFERENCE.key
    }
    for policy in POLICIES:
        mine = [r for r in table if r["policy"] == policy.key]
        if not mine:
            continue
        worst_dd = max(Decimal(r["card"]["max_drawdown"]) for r in mine)
        active = [
            r
            for r in mine
            if reference_trades.get((r["strategy_id"], r["timeframe"]), 0)
            >= MEANINGFUL_TRADE_SAMPLE
        ]
        stopped = sum(1 for r in active if r["effect"] == ProtectionEffect.STOPS_TRADING.value)
        blocked = statistics.median([r["blocked_time_share"] or Decimal(0) for r in mine])
        if worst_dd > PRODUCTION_DRAWDOWN_LIMIT:
            notes.append(
                f"{policy.key}: excluded — worst drawdown {pct(worst_dd, signed=False)} > 20%"
            )
            continue
        if active and stopped * 2 > len(active):
            notes.append(
                f"{policy.key}: excluded — stops trading on {stopped}/{len(active)} "
                "active combinations"
            )
            continue
        notes.append(
            f"{policy.key}: eligible — worst DD {pct(worst_dd, signed=False)}, median time blocked "
            f"{pct(blocked, signed=False)}, stops trading on {stopped}/{len(active)}"
        )
        eligible.append((Decimal(str(blocked)), worst_dd, policy.key))
    if not eligible:
        return None, notes
    return sorted(eligible)[0][2], notes


def top_combinations(table: list[dict[str, Any]], count: int = 2) -> list[dict[str, Any]]:
    """Return the combinations to follow: best verdict, then robustness. Never return first.

    Research strategies only, under a real policy (A-D), ordered by verdict tier, then by
    whether walk-forward was measurable and how much of it was positive, then by the smaller
    drawdown.
    """

    def wf_share(row: dict[str, Any]) -> Decimal:
        text = row["walk_forward"]
        if text.startswith("n/a") or "/" not in text:
            return Decimal(-1)
        positive, total = text.split("/")
        return Decimal(positive) / Decimal(total) if int(total) else Decimal(-1)

    candidates = [r for r in table if not r["benchmark"] and r["policy"] != REFERENCE.key]
    candidates.sort(
        key=lambda r: (
            -_TIER[r["verdict"]],
            -wf_share(r),
            Decimal(r["card"]["max_drawdown"]),
        )
    )
    return candidates[:count]


def md_table(header: list[str], body: list[list[str]]) -> str:
    """Render a Markdown table."""
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join([*lines, *("| " + " | ".join(row) + " |" for row in body)])


def build() -> str:
    """Return the full Markdown report and write the machine-readable verdicts."""
    data = load()
    table = rows(data)
    (OUT / "verdicts.json").write_text(json.dumps(table, indent=2, default=str), encoding="utf-8")
    out: list[str] = []
    labels = {p.key: p.label for p in (REFERENCE, *POLICIES)}
    out.append("Policies: " + " · ".join(f"**{k}** = {v}" for k, v in labels.items()) + "\n")

    out.append("### 1. Every combination\n")
    out.append(
        md_table(
            [
                "STRATEGY",
                "TF",
                "RISK POLICY",
                "TRADES",
                "GROSS",
                "COSTS",
                "NET",
                "PF",
                "EXP",
                "DD",
                "BLOCKED %",
                "VERDICT",
            ],
            [
                [
                    r["strategy_id"],
                    r["timeframe"],
                    r["policy"],
                    trades_cell(r["card"]),
                    pct(r["gross"]),
                    pct(r["costs"], signed=False),
                    pct(r["card"]["total_return"]),
                    num(r["card"]["profit_factor"]),
                    num(r["card"]["expectancy"]),
                    pct(r["card"]["max_drawdown"], signed=False),
                    pct(r["blocked_share"], signed=False) if r["policy"] != REFERENCE.key else DASH,
                    ("benchmark · " if r["benchmark"] else "") + r["verdict"],
                ]
                for r in table
            ],
        )
    )

    out.append("\n### 2. Part A — latch policies (all timeframes)\n")
    body: list[list[str]] = []
    for policy in POLICIES:
        mine = [r for r in table if r["policy"] == policy.key]
        effects = {e.value: 0 for e in ProtectionEffect}
        for r in mine:
            effects[r["effect"]] += 1
        body.append(
            [
                policy.key,
                policy.label,
                pct(
                    statistics.median([r["blocked_share"] or Decimal(0) for r in mine]),
                    signed=False,
                ),
                pct(
                    statistics.median([r["blocked_time_share"] or Decimal(0) for r in mine]),
                    signed=False,
                ),
                pct(max(Decimal(r["card"]["max_drawdown"]) for r in mine), signed=False),
                str(max(r["card"]["max_consecutive_losses"] for r in mine)),
                " · ".join(f"{k} {v}" for k, v in effects.items() if v),
            ]
        )
    out.append(
        md_table(
            [
                "POLICY",
                "WHAT IT DOES",
                "MEDIAN BLOCKED DECISIONS",
                "MEDIAN TIME BLOCKED",
                "WORST DD",
                "MAX LOSS STREAK",
                "EFFECT vs REFERENCE (combinations)",
            ],
            body,
        )
    )
    choice, notes = recommend_policy(table)
    out.append(
        "\n**Recommendation rule** (fixed before the run): safe → operable → least blocked.\n"
    )
    out.extend(f"* {note}" for note in notes)
    out.append(f"\n**Recommended latch policy: {choice or 'none eligible'}**\n")

    out.append("### 3. Part B — timeframe vs cost (research reference, not Risk V2)\n")
    body = []
    for candidate in STUDY_STRATEGIES:
        for timeframe in TIMEFRAMES:
            e = data.get((candidate.strategy_id, timeframe.value))
            if e is None:
                continue
            card = e["policies"][REFERENCE.key]["card"]
            g, c = gross(card), costs(card)
            ratio = DASH if c == 0 else f"{g / c:+.2f}"
            body.append(
                [
                    candidate.strategy_id,
                    timeframe.value,
                    str(card["trades"]),
                    pct(g),
                    pct(card["fees"] and Decimal(card["fees"]) / CAPITAL, signed=False),
                    pct(card["slippage"] and Decimal(card["slippage"]) / CAPITAL, signed=False),
                    pct(card["total_return"]),
                    cost_share(card),
                    ratio,
                    num(card["profit_factor"]),
                    num(card["expectancy"]),
                    pct(card["max_drawdown"], signed=False),
                    walk_forward(e),
                ]
            )
    out.append(
        md_table(
            [
                "STRATEGY",
                "TF",
                "TRADES/YR",
                "GROSS",
                "FEES",
                "SLIPPAGE",
                "NET",
                "COST / GROSS",
                "GROSS / COST",
                "PF",
                "EXP",
                "DD",
                "WALK-FORWARD",
            ],
            body,
        )
    )

    out.append("\n### 4. Walk-forward test windows (research reference)\n")
    body = []
    for candidate in STUDY_STRATEGIES:
        for timeframe in TIMEFRAMES:
            e = data.get((candidate.strategy_id, timeframe.value))
            if e is None or e.get("walk_forward") is None:
                continue
            cells = [
                f"{pct(f['card']['total_return'])} ({f['card']['trades']})"
                if f.get("card")
                else DASH
                for f in _test_folds(e)
            ]
            body.append([candidate.strategy_id, timeframe.value, *cells, walk_forward(e)])
    out.append(
        md_table(
            ["STRATEGY", "TF", "F0 Oct-Nov", "F1 Jan-Feb", "F2 Apr-May", "F3 Jul-Aug", "POSITIVE"],
            body,
        )
    )

    out.append("\n### 5. Stress (research reference, full year)\n")
    body = []
    for candidate in STUDY_STRATEGIES:
        for timeframe in TIMEFRAMES:
            e = data.get((candidate.strategy_id, timeframe.value))
            if e is None:
                continue
            body.append(
                [
                    candidate.strategy_id,
                    timeframe.value,
                    pct(e["policies"][REFERENCE.key]["card"]["total_return"]),
                    *[pct((s.get("card") or {}).get("total_return")) for s in e.get("stress", [])],
                ]
            )
    out.append(md_table(["STRATEGY", "TF", "BASE", *[s.upper() for s in STRESS_LABELS]], body))

    out.append("\n### 6. Top combinations to follow (rule: verdict → walk-forward → drawdown)\n")
    for r in top_combinations(table):
        out.append(
            f"* **{r['strategy_id']} · {r['timeframe']} · policy {r['policy']}** — "
            f"{r['verdict']}, net {pct(r['card']['total_return'])}, {r['card']['trades']} trades, "
            f"WF {r['walk_forward']}, DD {pct(r['card']['max_drawdown'], signed=False)}"
        )
    return "\n".join(out) + "\n"


def main() -> int:
    """Write the report next to the evidence and echo it."""
    report = build()
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
