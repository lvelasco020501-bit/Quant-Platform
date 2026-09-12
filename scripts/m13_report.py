"""Tabulate the M13 sprint's evidence into the report the milestone asked for.

Two risk scenarios, kept apart on every line: **A · DEPLOYED RISK V2** (``deployed.json``,
exactly what paper runs) and **B · RESEARCH VARIANT** (``evidence.json``, latches released,
not Risk V2). Computes no market figure: every number was produced by the harness. The
verdict is recomputed here, and only here, by :func:`quantplatform.research.sprint.judge`,
which is what makes this file rather than the runner the single source of verdicts.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.sprint import (
    CANDIDATES,
    MEANINGFUL_TRADE_SAMPLE,
    STRESS_LABELS,
    Evidence,
    Family,
    RiskScenario,
    Scorecard,
    judge,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m13"
DASH = "—"


def _dec(value: object) -> Decimal | None:
    """Return a decimal from a JSON value, keeping unknown as unknown."""
    return None if value is None else Decimal(str(value))


def pct(value: object) -> str:
    """Render a signed fraction as a percentage."""
    d = _dec(value)
    return DASH if d is None else f"{d * 100:+.2f}%"


def dd(value: object) -> str:
    """Render a drawdown fraction as an unsigned percentage."""
    d = _dec(value)
    return DASH if d is None else f"{d * 100:.2f}%"


def num(value: object, places: int = 2) -> str:
    """Render a plain decimal."""
    d = _dec(value)
    return DASH if d is None else f"{d:.{places}f}"


def pf(card: dict[str, Any] | None) -> str:
    """Render a profit factor, distinguishing 'no losses' from 'nothing traded'."""
    if card is None or card["trades"] == 0:
        return DASH
    if card["profit_factor"] is None:
        return "no losses"
    return num(card["profit_factor"])


def trades(card: dict[str, Any] | None) -> str:
    """Render a trade count, flagged when too thin for its ratios to mean much."""
    if card is None:
        return DASH
    flag = " LOW" if card["trades"] < MEANINGFUL_TRADE_SAMPLE else ""
    return f"{card['trades']}{flag}"


def test_folds(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the walk-forward *test* folds, in order."""
    folds = [f for f in evidence.get("walk_forward_folds", []) if f["role"] == "walk_forward_test"]
    return sorted(folds, key=lambda f: f["index"])


def walk_forward(evidence: dict[str, Any]) -> str:
    """Render positive test folds out of completed ones, with the median fold return."""
    folds = [f for f in test_folds(evidence) if f.get("card")]
    if not folds:
        return DASH
    positive = sum(1 for f in folds if Decimal(f["card"]["total_return"]) > 0)
    median = (evidence.get("walk_forward") or {}).get("median_return")
    return f"{positive}/{len(folds)} · med {pct(median)}"


def table(header: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown table."""
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _load(name: str) -> dict[str, dict[str, Any]]:
    """Return each candidate's ``name`` file, keyed by strategy id."""
    found: dict[str, dict[str, Any]] = {}
    for candidate in CANDIDATES:
        path = OUT / candidate.strategy_id / name
        if path.exists():
            found[candidate.strategy_id] = json.loads(path.read_text())
    return found


def _evidence(
    research: dict[str, Any], deployed: dict[str, Any] | None, best_benchmark: Decimal | None
) -> Evidence:
    """Assemble what :func:`judge` may look at. Only ``deployed_return`` comes from A."""
    wf = research.get("walk_forward") or {}
    stress = [
        value
        for s in research.get("stress", [])
        if s.get("card") and (value := _dec(s["card"]["total_return"])) is not None
    ]
    # A neighbour that traded nothing counts as zero, never as "no losses": silence is not
    # robustness. One with trades but no losses has no profit factor and passes.
    neighbour_pfs: list[Decimal] = []
    for n in research.get("neighbours", []):
        card = n.get("card")
        if card is None or card["trades"] == 0:
            neighbour_pfs.append(Decimal(0))
        elif card["profit_factor"] is not None:
            neighbour_pfs.append(Decimal(card["profit_factor"]))
    deployed_card = (deployed or {}).get("card") or {}
    return Evidence(
        full=Scorecard.model_validate(research["full"]),
        out_of_sample_return=_dec((research.get("out_of_sample") or {}).get("total_return")),
        walk_forward_positive_share=_dec(wf.get("positive_share_of_completed")),
        walk_forward_median_return=_dec(wf.get("median_return")),
        stress_worst_return=min(stress) if stress else None,
        neighbours_min_profit_factor=min(neighbour_pfs) if neighbour_pfs else None,
        benchmark_best_return=best_benchmark,
        deployed_return=_dec(deployed_card.get("total_return")),
    )


def classify(
    research_return: Decimal | None, deployed_return: Decimal | None, *, low_sample: bool
) -> str:
    """Answer the three questions: edge on its own, survives deployed, or relaxed-only.

    A positive return on too few trades is not called edge: it is reported as positive and
    unproven, which is what it is.
    """
    if research_return is None or research_return <= 0:
        return "no edge"
    survives = deployed_return is not None and deployed_return > 0
    if low_sample:
        return "positive, unproven (LOW SAMPLE)" + (", survives deployed" if survives else "")
    if survives:
        return "edge, survives deployed"
    return "relaxed-only"


def verdict_rows(
    research: dict[str, dict[str, Any]], deployed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Recompute every verdict from the evidence on disk. The only place verdicts are made."""
    benchmarks = [
        _dec(r["full"]["total_return"])
        for r in research.values()
        if r["family"] == Family.BENCHMARK.value and r.get("full")
    ]
    best = max((b for b in benchmarks if b is not None), default=None)
    rows: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        sid = candidate.strategy_id
        r = research.get(sid)
        if r is None or r.get("full") is None:
            rows.append({"strategy_id": sid, "verdict": "FAILED"})
            continue
        evidence = _evidence(r, deployed.get(sid), best)
        verdict = judge(evidence)
        tag = classify(
            evidence.full.total_return,
            evidence.deployed_return,
            low_sample=evidence.full.low_sample,
        )
        rows.append(
            {
                "strategy_id": sid,
                "family": candidate.family.value,
                "verdict": verdict.value,
                "benchmark": candidate.family is Family.BENCHMARK,
                "class": tag,
                "low_sample": evidence.full.low_sample,
                "evidence": json.loads(evidence.model_dump_json()),
            }
        )
    return rows


def _pair(a: str, b: str) -> str:
    return f"{a} / {b}"


def build() -> str:
    """Return the full Markdown report."""
    research = _load("evidence.json")
    deployed = _load("deployed.json")
    rows = verdict_rows(research, deployed)
    (OUT / "verdicts.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    by_id = {row["strategy_id"]: row for row in rows}
    order = [c.strategy_id for c in CANDIDATES if c.strategy_id in research]
    evidence = research
    out: list[str] = []

    out.append(
        f"**A** = {RiskScenario.DEPLOYED.label} (exactly what paper runs).  "
        f"**B** = {RiskScenario.RESEARCH_VARIANT.label}.  Cells read `A / B`.\n"
    )
    out.append("## 1. Both scenarios\n")
    main_rows: list[list[str]] = []
    for sid in order:
        a = (deployed.get(sid) or {}).get("card")
        b = research[sid]["full"]
        dep = deployed.get(sid) or {}
        latch = (dep.get("first_latch_at") or "never")[:10]
        blocked = dep.get("blocked_share")
        blocked_txt = DASH if blocked is None else f"{Decimal(str(blocked)) * 100:.0f}% blocked"
        row = by_id.get(sid, {})
        verdict = row.get("verdict", DASH)
        if row.get("benchmark"):
            verdict = f"benchmark ({verdict})"
        main_rows.append(
            [
                sid,
                f"{pct((a or {}).get('total_return'))} · {blocked_txt} · latch {latch}",
                f"{pct(b['total_return'])} · WF {walk_forward(research[sid])}",
                _pair(trades(a), trades(b)),
                _pair(pf(a), pf(b)),
                _pair(num((a or {}).get("expectancy")), num(b["expectancy"])),
                _pair(dd((a or {}).get("max_drawdown")), dd(b["max_drawdown"])),
                _pair(pct((a or {}).get("total_return")), pct(b["total_return"])),
                f"{verdict} · {row.get('class', DASH)}",
            ]
        )
    out.append(
        table(
            [
                "STRATEGY",
                "DEPLOYED RISK RESULT (A)",
                "RESEARCH VARIANT RESULT (B)",
                "TRADES",
                "PF",
                "EXPECTANCY",
                "DD",
                "RETURN",
                "VERDICT",
            ],
            main_rows,
        )
    )

    out.append("\n## 2. A · deployed Risk V2 — what the risk engine allowed\n")
    out.append(
        table(
            [
                "STRATEGY",
                "DECISIONS",
                "APPROVED",
                "LATCHED REJECTIONS",
                "BLOCKED",
                "FIRST LATCH",
                "TOP REFUSAL",
            ],
            [
                [
                    sid,
                    str(deployed[sid]["decisions"]),
                    str(deployed[sid]["approved"]),
                    str(deployed[sid]["latched_rejections"]),
                    DASH
                    if deployed[sid]["blocked_share"] is None
                    else f"{Decimal(str(deployed[sid]['blocked_share'])) * 100:.1f}%",
                    (deployed[sid]["first_latch_at"] or "never")[:10],
                    (deployed[sid]["top_rejection_reasons"] or [[DASH, 0]])[0][0][:60],
                ]
                for sid in order
                if sid in deployed
            ],
        )
    )
    out.append("\n## 3. B · research variant — full KPIs (not Risk V2)\n")
    out.append(
        table(
            [
                "STRATEGY",
                "WIN RATE",
                "AVG WIN",
                "AVG LOSS",
                "TIME IN MKT",
                "SLIPPAGE",
                "MAX CONSEC L",
                "OVERLAY EXITS",
            ],
            [
                [
                    sid,
                    pct(evidence[sid]["full"]["win_rate"]).lstrip("+"),
                    num(evidence[sid]["full"]["average_win"]),
                    num(evidence[sid]["full"]["average_loss"]),
                    pct(evidence[sid]["full"]["time_in_market"]).lstrip("+"),
                    num(evidence[sid]["full"]["slippage"]),
                    str(evidence[sid]["full"]["max_consecutive_losses"]),
                    f"{evidence[sid]['exits']['overlay_exits_approx']}/{evidence[sid]['exits']['trades']}",
                ]
                for sid in order
            ],
        )
    )

    out.append("\n## 4. B · research variant — in-sample vs out-of-sample\n")
    out.append(
        table(
            ["STRATEGY", "IS RETURN", "IS PF", "IS TRADES", "OOS RETURN", "OOS PF", "OOS TRADES"],
            [
                [
                    sid,
                    pct((evidence[sid].get("in_sample") or {}).get("total_return")),
                    pf(evidence[sid].get("in_sample")),
                    trades(evidence[sid].get("in_sample")),
                    pct((evidence[sid].get("out_of_sample") or {}).get("total_return")),
                    pf(evidence[sid].get("out_of_sample")),
                    trades(evidence[sid].get("out_of_sample")),
                ]
                for sid in order
            ],
        )
    )

    out.append("\n## 5. B · research variant — walk-forward test windows\n")
    out.append(
        table(
            ["STRATEGY", "F0 Oct-Nov", "F1 Jan-Feb", "F2 Apr-May", "F3 Jul-Aug", "POSITIVE"],
            [
                [
                    sid,
                    *[
                        f"{pct(f['card']['total_return'])} ({f['card']['trades']})"
                        if f.get("card")
                        else DASH
                        for f in test_folds(evidence[sid])
                    ],
                    walk_forward(evidence[sid]),
                ]
                for sid in order
            ],
        )
    )

    out.append("\n## 6. B · research variant — stress\n")
    out.append(
        table(
            ["STRATEGY", "BASE", *[label.upper() for label in STRESS_LABELS]],
            [
                [
                    sid,
                    pct(evidence[sid]["full"]["total_return"]),
                    *[
                        pct(s["card"]["total_return"]) if s.get("card") else DASH
                        for s in evidence[sid].get("stress", [])
                    ],
                ]
                for sid in order
            ],
        )
    )

    out.append("\n## 7. B · research variant — sensitivity\n")
    sens_rows: list[list[str]] = []
    for sid in order:
        neighbours = evidence[sid].get("neighbours", [])
        if not neighbours:
            continue
        cells = [
            sid,
            f"{pct(evidence[sid]['full']['total_return'])} / PF {pf(evidence[sid]['full'])}",
        ]
        for n in neighbours:
            varied = {k: v for k, v in n["params"].items() if evidence[sid]["params"].get(k) != v}
            label = ",".join(f"{k}={v}" for k, v in varied.items())
            card = n.get("card")
            cells.append(f"{label}: {pct(card['total_return']) if card else DASH} / PF {pf(card)}")
        sens_rows.append(cells)
    out.append(table(["STRATEGY", "CANONICAL", "NEIGHBOUR A", "NEIGHBOUR B"], sens_rows))

    return "\n".join(out) + "\n"


def main() -> int:
    """Write the report next to the evidence and echo it."""
    report = build()
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
