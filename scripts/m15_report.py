"""Tabulate M15 and apply its decision rules — every rule written before any result existed.

Reads ``var/research/m15/<strategy>/<timeframe>/{wf,full,<year>}/`` and the dataset's own
validation report. Computes no market figure: every number comes from the harness. It decides
four things, each by a rule stated here:

* **the headline** of a (strategy, timeframe, policy): the continuous whole-period run at 4h and
  1d; at 1h, where a whole-period run is unaffordable, the seven calendar years combined —
  returns compounded, trades and costs summed, profit factor from summed gross profit and loss,
  and drawdown the worst single year (a lower bound, and labelled as one);
* **per-year stability**: at 4h and 1d taken from the continuous run, split at the calendar
  year — never from yearly windows, whose warm-up would drop the first 73 days of every daily
  year; at 1h from the yearly windows, where warm-up is three days;
* **the verdict**: :func:`~quantplatform.research.sprint.judge`, then
  :func:`~quantplatform.research.m15.cap_by_sample`, so a thin sample cannot be a PAPER CANDIDATE;
* **the latch policy and the top two combinations**, by safety and robustness — never by return.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.m14 import ProtectionEffect, classify_protection
from quantplatform.research.m15 import (
    MIN_PAPER_TRADES,
    MIN_TRADES_PER_TEST_WINDOW,
    POLICIES,
    STUDY_STRATEGIES,
    TIMEFRAMES,
    YEARLY_WINDOWS,
    cap_by_sample,
)
from quantplatform.research.result import ExperimentResult
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
OUT = ROOT / "var/research/m15"
DATASET_REPORT = ROOT / "data/raw/m15/validation_report.json"
CAPITAL = Decimal(10_000)
DASH = "—"
PRODUCTION_DRAWDOWN_LIMIT = Decimal("0.20")
YEARS = [w.start.year for w in YEARLY_WINDOWS]
OOS_YEAR = 2025
_ORDER = (Verdict.REJECT, Verdict.WEAK, Verdict.PROMISING, Verdict.PAPER_CANDIDATE)
_TIER = {v.value: rank for rank, v in enumerate(_ORDER)}
Card = dict[str, Any]


def _d(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def pct(value: object, *, signed: bool = True) -> str:
    """Render a fraction as a percentage."""
    d = _d(value)
    if d is None:
        return DASH
    return f"{d * 100:+.2f}%" if signed else f"{d * 100:.2f}%"


def num(value: object) -> str:
    """Render a decimal to two places."""
    d = _d(value)
    return DASH if d is None else f"{d:.2f}"


def _json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def costs(card: Card) -> Decimal:
    """Return fees plus slippage as a fraction of starting capital."""
    return (Decimal(card["fees"]) + Decimal(card["slippage"])) / CAPITAL


def combine(cards: Iterable[Card | None]) -> Card | None:
    """Combine yearly cards into one: compounded return, summed counts and gross figures."""
    items = [c for c in cards if c is not None]
    if not items:
        return None
    growth, trades, fees, slippage = Decimal(1), 0, Decimal(0), Decimal(0)
    profit, loss = Decimal(0), Decimal(0)
    for c in items:
        growth *= 1 + Decimal(c["total_return"])
        trades += c["trades"]
        fees += Decimal(c["fees"])
        slippage += Decimal(c["slippage"])
        profit += Decimal(c["gross_profit"])
        loss += Decimal(c["gross_loss"])
    return {
        "total_return": growth - 1,
        "max_drawdown": max(Decimal(c["max_drawdown"]) for c in items),
        "profit_factor": profit / loss if loss > 0 else None,
        "expectancy": (profit - loss) / trades if trades else None,
        "trades": trades,
        "fees": fees,
        "slippage": slippage,
        "win_rate": None,
        "average_win": None,
        "average_loss": None,
        "time_in_market": None,
        "max_consecutive_losses": max(c["max_consecutive_losses"] for c in items),
        "gross_profit": profit,
        "gross_loss": loss,
    }


class Study:
    """Every piece of M15 evidence, indexed the way the rules read it."""

    def __init__(self) -> None:
        self.dataset: dict[str, Any] = _json(DATASET_REPORT) or {}
        self.wf: dict[tuple[str, str], dict[str, Any]] = {}
        self.full: dict[tuple[str, str], dict[str, Any]] = {}
        self.year: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.continuous: dict[tuple[str, str, str], ExperimentResult] = {}
        for candidate in STUDY_STRATEGIES:
            sid = candidate.strategy_id
            for tf in TIMEFRAMES:
                self._load(sid, tf.value)

    def _load(self, sid: str, tf: str) -> None:
        home = OUT / sid / tf
        if (wf := _json(home / "wf" / "evidence.json")) is not None:
            self.wf[(sid, tf)] = wf
        if (full := _json(home / "full" / "evidence.json")) is not None:
            self.full[(sid, tf)] = full
            for path in sorted((home / "full" / "results").glob("*.json")):
                result = ExperimentResult.model_validate_json(path.read_text())
                policy = result.definition.name.split("-")[-2]
                self.continuous[(sid, tf, policy)] = result
        for year in YEARS:
            if (yearly := _json(home / str(year) / "evidence.json")) is not None:
                self.year[(sid, tf, year)] = yearly

    def _policy_entry(self, sid: str, tf: str, year: int, policy: str) -> dict[str, Any]:
        policies = (self.year.get((sid, tf, year)) or {}).get("policies", {})
        entry: dict[str, Any] = policies.get(policy) or {}
        return entry

    def yearly_cards(self, sid: str, tf: str, policy: str) -> dict[int, Card | None]:
        """Return one card per year, from yearly windows (the reference from walk-forward)."""
        if policy == "REF":
            cards: dict[int, Card | None] = {}
            for fold in (self.wf.get((sid, tf)) or {}).get("folds", []):
                year = int(fold["start"][:4])
                if fold["role"] == "walk_forward_test" or year not in cards:
                    cards[year] = fold["card"]
            return cards
        return {year: self._policy_entry(sid, tf, year, policy).get("card") for year in YEARS}

    def headline(self, sid: str, tf: str, policy: str) -> Card | None:
        """Return the scorecard a (strategy, timeframe, policy) is judged on."""
        if tf != "1h":
            entry = (self.full.get((sid, tf)) or {}).get("policies", {}).get(policy)
            return None if entry is None else entry["card"]
        return combine(self.yearly_cards(sid, tf, policy).values())

    def blocked(self, sid: str, tf: str, policy: str) -> tuple[Decimal | None, Decimal | None]:
        """Return (share of decisions blocked, share of time blocked)."""
        if policy == "REF":
            return None, None
        if tf != "1h":
            entry = (self.full.get((sid, tf)) or {}).get("policies", {}).get(policy) or {}
            assessed = entry.get("assessed") or 0
            share = Decimal(entry.get("blocked") or 0) / assessed if assessed else None
            return share, _d(entry.get("blocked_time_share"))
        entries = [self._policy_entry(sid, tf, y, policy) for y in YEARS]
        assessed = sum(e.get("assessed") or 0 for e in entries)
        blocked = sum(e.get("blocked") or 0 for e in entries)
        known = [t for e in entries if (t := _d(e.get("blocked_time_share"))) is not None]
        return (
            Decimal(blocked) / assessed if assessed else None,
            sum(known, start=Decimal(0)) / len(known) if known else None,
        )

    def per_year(self, sid: str, tf: str, policy: str) -> dict[int, tuple[Decimal | None, int]]:
        """Return (return, trades) per calendar year. 4h/1d: split the continuous run."""
        if tf == "1h":
            yearly = self.yearly_cards(sid, tf, policy)
            return {
                y: (None, 0) if c is None else (Decimal(c["total_return"]), c["trades"])
                for y, c in yearly.items()
            }
        result = self.continuous.get((sid, tf, policy))
        if result is None:
            return {}
        out: dict[int, tuple[Decimal | None, int]] = {}
        opening = CAPITAL
        for year in YEARS:
            points = [p for p in result.equity_curve if p.at.year == year]
            closing = points[-1].equity if points else opening
            trades = sum(1 for t in result.trades if t.closed_at.year == year)
            out[year] = (closing / opening - 1 if opening else None, trades)
            opening = closing
        return out

    def walk_forward(self, sid: str, tf: str) -> list[tuple[int, Card | None]]:
        """Return the reference's test windows, in order."""
        folds = (self.wf.get((sid, tf)) or {}).get("folds", [])
        return [(int(f["start"][:4]), f["card"]) for f in folds if f["role"] == "walk_forward_test"]

    def stress(self, sid: str, tf: str) -> list[Card | None]:
        """Return each cost scenario combined across the years, on the reference."""
        out = []
        for index in range(len(STRESS_LABELS)):
            cards = []
            for y in YEARS:
                entries = (self.year.get((sid, tf, y)) or {}).get("stress") or []
                cards.append(entries[index]["card"] if index < len(entries) else None)
            out.append(combine(cards))
        return out

    def neighbours(self, sid: str, tf: str) -> list[Card | None]:
        """Return each declared neighbour combined across the years, on the reference."""
        candidate = next(c for c in STUDY_STRATEGIES if c.strategy_id == sid)
        out = []
        for index in range(len(candidate.neighbours)):
            cards = []
            for y in YEARS:
                entries = (self.year.get((sid, tf, y)) or {}).get("neighbours") or []
                cards.append(entries[index]["card"] if index < len(entries) else None)
            out.append(combine(cards))
        return out


# --- Verdicts and decisions ----------------------------------------------------------------------


def _evidence(
    study: Study, sid: str, tf: str, policy: str, card: Card, best: Decimal | None
) -> tuple[Evidence, list[int], list[Decimal]]:
    """Assemble what the verdict may look at, plus the walk-forward trades and returns."""
    candidate = next(c for c in STUDY_STRATEGIES if c.strategy_id == sid)
    wf = study.walk_forward(sid, tf)
    wf_returns = [Decimal(c["total_return"]) for _, c in wf if c is not None]
    wf_trades = [c["trades"] if c else 0 for _, c in wf]
    stress = [Decimal(c["total_return"]) for c in study.stress(sid, tf) if c]
    neighbour_pfs: list[Decimal] = []
    for n in study.neighbours(sid, tf):
        if n is None or n["trades"] == 0:
            neighbour_pfs.append(Decimal(0))
        elif n["profit_factor"] is not None:
            neighbour_pfs.append(Decimal(n["profit_factor"]))
    if not candidate.neighbours:
        neighbour_pfs.append(Decimal(0))  # absent is a failed check, never a pass
    oos = next((c for y, c in wf if y == OOS_YEAR), None)
    own = study.headline(sid, tf, "A" if policy == "REF" else policy)
    evidence = Evidence(
        full=Scorecard.model_validate(
            {k: v for k, v in card.items() if k in Scorecard.model_fields}
        ),
        out_of_sample_return=_d(None if oos is None else oos["total_return"]),
        walk_forward_positive_share=(
            Decimal(sum(1 for r in wf_returns if r > 0)) / len(wf_returns) if wf_returns else None
        ),
        walk_forward_median_return=Decimal(str(statistics.median(wf_returns)))
        if wf_returns
        else None,
        stress_worst_return=min(stress) if stress else None,
        neighbours_min_profit_factor=min(neighbour_pfs) if neighbour_pfs else None,
        benchmark_best_return=best,
        deployed_return=_d(None if own is None else own["total_return"]),
    )
    return evidence, wf_trades, wf_returns


def rows(study: Study) -> list[dict[str, Any]]:
    """Return one row per (strategy, timeframe, policy), with verdict and protection effect."""
    out: list[dict[str, Any]] = []
    for tf in (t.value for t in TIMEFRAMES):
        for policy in POLICIES:
            benchmarks = [
                Decimal(card["total_return"])
                for c in STUDY_STRATEGIES
                if c.family is Family.BENCHMARK
                and (card := study.headline(c.strategy_id, tf, policy.key)) is not None
            ]
            best = max(benchmarks) if benchmarks else None
            for candidate in STUDY_STRATEGIES:
                sid = candidate.strategy_id
                card = study.headline(sid, tf, policy.key)
                if card is None:
                    continue
                evidence, wf_trades, wf_returns = _evidence(study, sid, tf, policy.key, card, best)
                verdict = cap_by_sample(
                    judge(evidence), trades=card["trades"], window_trades=wf_trades
                )
                reference = study.headline(sid, tf, "REF")
                effect = None
                if policy.key != "REF" and reference is not None:
                    effect = classify_protection(card, reference).value
                blocked, blocked_time = study.blocked(sid, tf, policy.key)
                out.append(
                    {
                        "strategy_id": sid,
                        "benchmark": candidate.family is Family.BENCHMARK,
                        "timeframe": tf,
                        "policy": policy.key,
                        "card": card,
                        "gross": Decimal(card["total_return"]) + costs(card),
                        "costs": costs(card),
                        "blocked": blocked,
                        "blocked_time": blocked_time,
                        "verdict": verdict.value,
                        "effect": effect,
                        "wf_positive": sum(1 for r in wf_returns if r > 0),
                        "wf_total": len(wf_returns),
                        "wf_min_trades": min(wf_trades) if wf_trades else 0,
                    }
                )
    return out


def recommend_policy(table: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """M14's rule, unchanged: safe (worst DD within 20%), operable, then least time blocked."""
    notes: list[str] = []
    eligible: list[tuple[Decimal, Decimal, str]] = []
    ref_trades = {
        (r["strategy_id"], r["timeframe"]): r["card"]["trades"]
        for r in table
        if r["policy"] == "REF"
    }
    for policy in (p for p in POLICIES if p.key != "REF"):
        mine = [r for r in table if r["policy"] == policy.key]
        if not mine:
            continue
        worst = max(Decimal(r["card"]["max_drawdown"]) for r in mine)
        active = [
            r
            for r in mine
            if ref_trades.get((r["strategy_id"], r["timeframe"]), 0) >= MEANINGFUL_TRADE_SAMPLE
        ]
        stopped = sum(1 for r in active if r["effect"] == ProtectionEffect.STOPS_TRADING.value)
        blocked = statistics.median([r["blocked_time"] or Decimal(0) for r in mine])
        label = (
            f"worst DD {pct(worst, signed=False)}, median time blocked "
            f"{pct(blocked, signed=False)}, stops trading on {stopped}/{len(active)}"
        )
        if worst > PRODUCTION_DRAWDOWN_LIMIT:
            notes.append(f"{policy.key}: excluded (unsafe) — {label}")
        elif active and stopped * 2 > len(active):
            notes.append(f"{policy.key}: excluded (not operable) — {label}")
        else:
            notes.append(f"{policy.key}: eligible — {label}")
            eligible.append((Decimal(str(blocked)), worst, policy.key))
    return (sorted(eligible)[0][2] if eligible else None), notes


def top_combinations(table: list[dict[str, Any]], count: int = 2) -> list[dict[str, Any]]:
    """Research strategies under a real policy: verdict, then walk-forward, then drawdown.

    One entry per (strategy, timeframe): M14's version returned the same combination twice
    under two policies that never tripped, so a combination now counts once, at its best rank.
    """
    candidates = [r for r in table if not r["benchmark"] and r["policy"] != "REF"]
    candidates.sort(
        key=lambda r: (
            -_TIER[r["verdict"]],
            -(Decimal(r["wf_positive"]) / r["wf_total"] if r["wf_total"] else Decimal(-1)),
            Decimal(r["card"]["max_drawdown"]),
        )
    )
    seen: set[tuple[str, str]] = set()
    picked: list[dict[str, Any]] = []
    for r in candidates:
        key = (r["strategy_id"], r["timeframe"])
        if key not in seen:
            seen.add(key)
            picked.append(r)
        if len(picked) == count:
            break
    return picked


# --- Rendering ------------------------------------------------------------------------------------


def md(header: list[str], body: list[list[str]]) -> str:
    """Render a Markdown table."""
    head = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join([*head, *("| " + " | ".join(r) + " |" for r in body)])


def _section_dataset(data: dict[str, Any]) -> list[str]:
    archives, units = data["archives"], data["timestamp_units"]
    m10c, outages = data["cross_check_m10c"], data["outage_confirmation"]
    rest_ok = sum(1 for x in data["cross_check_rest"] if x["match"])
    body = [
        ["archives", f"{archives['monthly']} monthly + {archives['daily']} daily"],
        ["checksums verified", str(archives["checksums_verified"])],
        ["timestamp units detected", f"{units['ms']} ms + {units['us']} µs"],
        [
            "1h bars expected / present",
            f"{data['expected_bar_count']} / {data['actual_bar_count']}",
        ],
        [
            "missing hours",
            f"{data['missing_bar_count']}, absent on live REST too: "
            f"{outages['absent_on_rest']}/{outages['hours']} (outages)",
        ],
        ["anomalies", ", ".join(a["kind"] for a in data["anomalies"]) or "none"],
        ["vs M10c year", f"{m10c['compared'] - m10c['mismatched']}/{m10c['compared']} identical"],
        ["vs live REST", f"{rest_ok}/{len(data['cross_check_rest'])} identical"],
    ]
    for tf, c in data["cross_check_official"].items():
        body.append(
            [
                f"{tf} vs Binance's own {tf}",
                f"{c['identical']}/{c['binance_bars']} identical, "
                f"{len(c['explained_deviations'])} explained, {c['unexplained']} unexplained",
            ]
        )
    for tf, o in data["outputs"].items():
        body.append([f"{tf} digest", f"`{o['bars_digest']}` ({o['bars']} bars)"])
    return ["#### 0. Dataset\n", md(["CHECK", "RESULT"], body)]


def _row_cells(r: dict[str, Any]) -> list[str]:
    card = r["card"]
    low = " LOW" if card["trades"] < MEANINGFUL_TRADE_SAMPLE else ""
    lower_bound = "¹" if r["timeframe"] == "1h" else ""
    return [
        r["strategy_id"],
        r["timeframe"],
        r["policy"],
        f"{card['trades']}{low}",
        pct(r["gross"]),
        pct(r["costs"], signed=False),
        pct(card["total_return"]),
        num(card["profit_factor"]),
        num(card["expectancy"]),
        pct(card["max_drawdown"], signed=False) + lower_bound,
        DASH if r["blocked"] is None else pct(r["blocked"], signed=False),
        f"{r['wf_positive']}/{r['wf_total']}",
        ("benchmark · " if r["benchmark"] else "") + r["verdict"],
    ]


def _section_table(table: list[dict[str, Any]]) -> list[str]:
    header = [
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
        "WF",
        "VERDICT",
    ]
    return [
        "\n#### 1. Every combination\n",
        md(header, [_row_cells(r) for r in table]),
        "\n¹ 1h drawdown is the worst single calendar year: a lower bound on the true one.\n",
    ]


def _section_policies(table: list[dict[str, Any]]) -> list[str]:
    body = []
    for policy in (p for p in POLICIES if p.key != "REF"):
        mine = [r for r in table if r["policy"] == policy.key]
        effects = {e.value: 0 for e in ProtectionEffect}
        for r in mine:
            if r["effect"]:
                effects[r["effect"]] += 1
        body.append(
            [
                policy.key,
                policy.label,
                pct(statistics.median([r["blocked"] or Decimal(0) for r in mine]), signed=False),
                pct(
                    statistics.median([r["blocked_time"] or Decimal(0) for r in mine]), signed=False
                ),
                pct(max(Decimal(r["card"]["max_drawdown"]) for r in mine), signed=False),
                str(max(r["card"]["max_consecutive_losses"] for r in mine)),
                " · ".join(f"{k} {v}" for k, v in effects.items() if v),
            ]
        )
    header = [
        "POLICY",
        "WHAT IT DOES",
        "MEDIAN BLOCKED DECISIONS",
        "MEDIAN TIME BLOCKED",
        "WORST DD",
        "LONGEST LOSS STREAK",
        "EFFECT vs REF",
    ]
    choice, notes = recommend_policy(table)
    return [
        "#### 2. Latch policies — A vs C vs D (reference REF, not Risk V2)\n",
        md(header, body),
        "\n**M14's rule, unchanged** (safe → operable → least blocked):\n",
        *(f"* {n}" for n in notes),
        f"\n**Recommended latch policy on six and a half years: {choice or 'none eligible'}**\n",
    ]


def _section_years(study: Study) -> list[str]:
    body = []
    for candidate in STUDY_STRATEGIES:
        for tf in (t.value for t in TIMEFRAMES):
            for key in ("REF", "C"):
                years = study.per_year(candidate.strategy_id, tf, key)
                if not years:
                    continue
                cells = []
                for year in YEARS:
                    ret, trades = years.get(year, (None, 0))
                    cells.append(DASH if ret is None else f"{pct(ret)} · {trades}")
                positive = sum(1 for r, _ in years.values() if r is not None and r > 0)
                body.append([candidate.strategy_id, tf, key, *cells, f"{positive}/{len(years)}"])
    return [
        "#### 3. Stability by calendar year (net return · trades)\n",
        md(["STRATEGY", "TF", "POLICY", *[str(y) for y in YEARS], "POSITIVE YEARS"], body),
        "\n1h from yearly windows; 4h and 1d from the continuous run, split at each year end.\n",
    ]


def _section_walk_forward(study: Study) -> list[str]:
    body = []
    for candidate in STUDY_STRATEGIES:
        for tf in (t.value for t in TIMEFRAMES):
            folds = study.walk_forward(candidate.strategy_id, tf)
            if not folds:
                continue
            cells = [f"{pct(c['total_return'])} ({c['trades']})" if c else DASH for _, c in folds]
            positive = sum(1 for _, c in folds if c and Decimal(c["total_return"]) > 0)
            body.append([candidate.strategy_id, tf, *cells, f"{positive}/{len(folds)}"])
    return [
        "#### 4. Walk-forward — train one year, test the next (reference)\n",
        md(["STRATEGY", "TF", *[str(y) for y in YEARS[1:]], "POSITIVE"], body),
    ]


def _section_stress(study: Study) -> list[str]:
    body = []
    for candidate in STUDY_STRATEGIES:
        sid = candidate.strategy_id
        for tf in (t.value for t in TIMEFRAMES):
            base = combine(study.yearly_cards(sid, tf, "REF").values())
            stressed = [
                pct(None if c is None else c["total_return"]) for c in study.stress(sid, tf)
            ]
            body.append([sid, tf, pct(None if base is None else base["total_return"]), *stressed])
    return [
        "\n#### 5. Stress — every calendar year, combined (reference)\n",
        md(["STRATEGY", "TF", "BASE", *[s.upper() for s in STRESS_LABELS]], body),
    ]


def _section_top(table: list[dict[str, Any]]) -> list[str]:
    lines = [
        "\n#### 6. Top combinations (verdict → walk-forward → drawdown; one per strategy, TF)\n"
    ]
    for r in top_combinations(table):
        lines.append(
            f"* **{r['strategy_id']} · {r['timeframe']} · policy {r['policy']}** — {r['verdict']}, "
            f"net {pct(r['card']['total_return'])}, {r['card']['trades']} trades, walk-forward "
            f"{r['wf_positive']}/{r['wf_total']} (min {r['wf_min_trades']} trades in a test year), "
            f"DD {pct(r['card']['max_drawdown'], signed=False)}"
        )
    lines.append(
        f"\nPAPER CANDIDATE needs ≥ {MIN_PAPER_TRADES} trades and ≥ {MIN_TRADES_PER_TEST_WINDOW} "
        "in every walk-forward test year, on top of every check the verdict already makes.\n"
    )
    return lines


def build(study: Study) -> str:
    """Return the Markdown report, and write the machine-readable verdicts next to it."""
    table = rows(study)
    (OUT / "verdicts.json").write_text(json.dumps(table, indent=2, default=str), encoding="utf-8")
    sections = [
        _section_dataset(study.dataset),
        _section_table(table),
        _section_policies(table),
        _section_years(study),
        _section_walk_forward(study),
        _section_stress(study),
        _section_top(table),
    ]
    return "\n".join(line for section in sections for line in section) + "\n"


def main() -> int:
    """Write the report next to the evidence and echo it."""
    report = build(Study())
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
