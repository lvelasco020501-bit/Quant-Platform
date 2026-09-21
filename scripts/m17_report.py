"""Turn M17's evidence into one report: five recovery rules, six markets, one recommendation.

Reads ``var/research/m17/<ASSET>/<POLICY>/{policy,stress}/evidence.json`` and writes
``var/research/m17/REPORT.md`` next to a machine-readable ``policies.json``.

The choosing rule is the one declared in :mod:`quantplatform.research.m17` before any result
existed — damage, then predictability, then continuity, with ties broken on time blocked.
Return is not an input, and this script never sorts by it.

Usage::

    uv run python scripts/m17_report.py
"""

from __future__ import annotations

import json
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.m16 import ASSETS, pool
from quantplatform.research.m17 import (
    FEE_MULTIPLIERS,
    POLICIES,
    PolicyRow,
    continuity_swing,
    recommend,
)
from quantplatform.research.sprint import STRESS_LABELS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m17"
DASH = "—"
BASE_FEE = Decimal("1.00")
Card = dict[str, Any]


def _d(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def pct(value: object, *, signed: bool = True) -> str:
    """Render a fraction as a percentage."""
    number = _d(value)
    if number is None:
        return DASH
    return f"{number * 100:+.2f}%" if signed else f"{number * 100:.2f}%"


def num(value: object, places: int = 2) -> str:
    """Render a decimal to a fixed number of places."""
    number = _d(value)
    return DASH if number is None else f"{number:.{places}f}"


def _json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class Study:
    """Everything M17 recorded, indexed by market and policy."""

    def __init__(self) -> None:
        """Load every job's evidence, tolerating jobs that were not run."""
        self.policy: dict[tuple[str, str], dict[str, Any]] = {}
        self.stress: dict[tuple[str, str], dict[str, Any]] = {}
        for asset in ASSETS:
            for policy in POLICIES:
                found = _json(OUT / asset.raw / policy.key / "policy" / "evidence.json")
                if found is not None:
                    self.policy[(asset.raw, policy.key)] = found
                stressed = _json(OUT / asset.raw / policy.key / "stress" / "evidence.json")
                if stressed is not None:
                    self.stress[(asset.raw, policy.key)] = stressed

    def runs(self, raw: str, key: str) -> list[dict[str, Any]]:
        """Return the three fee settings for one market and policy."""
        evidence = self.policy.get((raw, key))
        return [] if evidence is None else evidence.get("runs") or []

    def at_base_fee(self, raw: str, key: str) -> dict[str, Any] | None:
        """Return the run at the real cost assumption."""
        for run in self.runs(raw, key):
            if Decimal(str(run["fee_multiplier"])) == BASE_FEE:
                return run
        return None

    def worst_stress(self, raw: str, key: str) -> Decimal | None:
        """Return the worst of M16's three cost scenarios, when they were run."""
        evidence = self.stress.get((raw, key))
        if evidence is None:
            return None
        returns = [
            Decimal(str(entry["card"]["total_return"]))
            for entry in evidence.get("stress") or []
            if entry.get("card") is not None
        ]
        return min(returns) if returns else None


def _stored_result(home: Path, experiment_id: str) -> dict[str, Any] | None:
    """Return the recorded result for one experiment, so its trades can be read back."""
    ledger = home / "ledger.jsonl"
    if not ledger.exists():
        return None
    attempt: str | None = None
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("experiment_id") == experiment_id:
            attempt = entry.get("attempt_id")
    if attempt is None:
        return None
    path = home / "results" / f"{attempt}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def reopenings(run: dict[str, Any], result: dict[str, Any] | None) -> tuple[int, int]:
    """Return how many halts ended, and how many of those were followed by a trade.

    A halt that ends without the market trading again before the next halt is a reopening in
    name only — the shape of failure policy E shows under stress, where it reopened XRP
    twenty times and still finished on C's exact result.
    """
    halts = run.get("halts") or []
    ended = [h for h in halts if h.get("to")]
    if not ended or result is None:
        return len(ended), 0
    opened = sorted(trade["opened_at"] for trade in result.get("trades") or [])
    starts = [h["from"] for h in halts]
    effective = 0
    for halt in ended:
        release = halt["to"]
        following = [s for s in starts if s > release]
        limit = min(following) if following else None
        if any(when >= release and (limit is None or when < limit) for when in opened):
            effective += 1
    return len(ended), effective


def market_rows(study: Study) -> list[dict[str, Any]]:
    """Return one row per market and policy, at the real cost assumption."""
    rows: list[dict[str, Any]] = []
    for asset in ASSETS:
        for policy in POLICIES:
            run = study.at_base_fee(asset.raw, policy.key)
            if run is None or run.get("card") is None:
                continue
            card = run["card"]
            counts = [r["card"]["trades"] for r in study.runs(asset.raw, policy.key) if r["card"]]
            home = OUT / asset.raw / policy.key / "policy"
            ended, effective = reopenings(run, _stored_result(home, run["experiment_id"]))
            # Operability is counted over every cost scenario, not only the cheap one: the
            # halts that matter are the ones a policy imposes when the account is struggling.
            stressed = study.stress.get((asset.raw, policy.key)) or {}
            stress_home = OUT / asset.raw / policy.key / "stress"
            for entry in stressed.get("stress") or []:
                more, good = reopenings(entry, _stored_result(stress_home, entry["experiment_id"]))
                ended += more
                effective += good
            rows.append(
                {
                    "raw": asset.raw,
                    "symbol": asset.symbol,
                    "policy": policy.key,
                    "trades": card["trades"],
                    "net": Decimal(str(card["total_return"])),
                    "profit_factor": _d(card["profit_factor"]),
                    "max_drawdown": Decimal(str(card["max_drawdown"])),
                    "loss_streak": card.get("max_consecutive_losses"),
                    "blocked": _d(run["blocked_time_share"]) or Decimal(0),
                    "halts": run.get("halt_count", 0),
                    "longest_halt_days": run.get("longest_halt_days", 0),
                    "trade_counts": counts,
                    "swing": continuity_swing(counts),
                    "reopenings": ended,
                    "effective": effective,
                    "stress": study.worst_stress(asset.raw, policy.key),
                    "card": card,
                }
            )
    return rows


def policy_rows(rows: list[dict[str, Any]]) -> list[PolicyRow]:
    """Group the market rows by policy, in the order the policies were declared."""
    return [
        PolicyRow(policy.key, [r for r in rows if r["policy"] == policy.key])
        for policy in POLICIES
        if any(r["policy"] == policy.key for r in rows)
    ]


def section_rules() -> list[str]:
    """Render what each policy actually does."""
    lines = ["#### 0. The five rules", "", "| POLICY | RULE |", "|---|---|"]
    for policy in POLICIES:
        lines.append(f"| {policy.key} | {policy.label} |")
    lines += [
        "",
        "C, E, F and G arm the identical breaker at the identical 10% threshold. What differs "
        "is only when a halted market reopens — and, for F and G, that the drawdown is then "
        "measured from where it restarted rather than from a peak it can no longer reach.",
        "",
        "**Why that last part matters.** The drawdown *limit* is not the latch: while equity "
        "sits 10% below its reference, the limit refuses new exposure whether or not a latch "
        "is set. So a rule that merely ends the halt (E) reopens the market straight back into "
        "the same refusal. Only moving the reference (F, G) lets a market trade again.",
        "",
    ]
    return lines


def section_policies(policies: list[PolicyRow], rows: list[dict[str, Any]]) -> list[str]:
    """Render the table the milestone asked for, one line per policy."""
    lines = [
        "#### 1. The policies, across all six markets",
        "",
        "| POLICY | BLOCKED % | TRADES | NET | PF | DD | LOSS STREAK | STRESS |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for policy in policies:
        mine = [r for r in rows if r["policy"] == policy.key]
        pooled = pool([r["card"] for r in mine])
        nets = [r["net"] for r in mine]
        stresses = [r["stress"] for r in mine if r["stress"] is not None]
        lines.append(
            f"| {policy.key} | median {pct(policy.median_blocked, signed=False)}, "
            f"worst {pct(policy.worst_blocked, signed=False)} "
            f"| {pooled.trades} | median {pct(statistics.median(nets) if nets else None)} "
            f"| {num(pooled.profit_factor)} "
            f"| worst {pct(policy.worst_drawdown, signed=False)} "
            f"| worst {max((r['loss_streak'] or 0) for r in mine)} "
            f"| {pct(min(stresses)) if stresses else DASH} |"
        )
    lines += [
        "",
        "BLOCKED % is the share of each market's history in which new exposure was refused; "
        "TRADES is pooled across the six markets; NET is the median market, never a sum; "
        "STRESS is the worst market under the worst of M16's three cost scenarios, where those "
        "were run.",
        "",
    ]
    return lines


def section_markets(rows: list[dict[str, Any]]) -> list[str]:
    """Render every market under every policy."""
    lines = [
        "#### 2. Every market under every policy (real costs)",
        "",
        "| ASSET | POLICY | TRADES | NET | PF | DD | BLOCKED | HALTS | LONGEST HALT | REOPENED |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['symbol']} | {row['policy']} | {row['trades']} | {pct(row['net'])} "
            f"| {num(row['profit_factor'])} | {pct(row['max_drawdown'], signed=False)} "
            f"| {pct(row['blocked'], signed=False)} | {row['halts']} "
            f"| {row['longest_halt_days']:.0f}d "
            f"| {row['effective']}/{row['reopenings']} |"
        )
    lines += [
        "",
        "REOPENED counts the halts that ended and were followed by a trade before the next "
        "halt, over the halts that ended at all. A reopening that cannot trade is not a "
        "recovery.",
        "",
    ]
    return lines


def section_continuity(rows: list[dict[str, Any]]) -> list[str]:
    """Render how much a small fee change moves each market under each policy."""
    header = " | ".join(f"fee x{m.normalize()}" for m in FEE_MULTIPLIERS)
    lines = [
        "#### 3. Continuity — trades at three fee settings",
        "",
        f"| ASSET | POLICY | {header} | SWING |",
        "|---" * (len(FEE_MULTIPLIERS) + 3) + "|",
    ]
    for row in rows:
        counts = " | ".join(str(count) for count in row["trade_counts"])
        flag = ""
        if row["swing"] is not None and row["swing"] >= Decimal("0.5"):
            flag = " ⚠️"
        lines.append(
            f"| {row['symbol']} | {row['policy']} | {counts} "
            f"| {pct(row['swing'], signed=False)}{flag} |"
        )
    lines += [
        "",
        "SWING is how much the trade count moved as a share of the largest. A quarter more in "
        "fees should not change whether a market trades at all; ⚠️ marks a swing of half or more.",
        "",
    ]
    return lines


def section_xrp(rows: list[dict[str, Any]]) -> list[str]:
    """Render the market the milestone exists for, before and after."""
    lines = [
        "#### 4. XRP — the market that exposed the defect",
        "",
        "| POLICY | TRADES AT x1.00 | x1.10 | x1.25 | NET | BLOCKED | HALTS | LONGEST HALT |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in [r for r in rows if r["raw"] == "XRPUSDT"]:
        counts = row["trade_counts"] + [None] * (len(FEE_MULTIPLIERS) - len(row["trade_counts"]))
        cells = " | ".join(DASH if c is None else str(c) for c in counts)
        lines.append(
            f"| {row['policy']} | {cells} | {pct(row['net'])} "
            f"| {pct(row['blocked'], signed=False)} | {row['halts']} "
            f"| {row['longest_halt_days']:.0f}d |"
        )
    return [*lines, ""]


def section_head_to_head(study: Study, rows: list[dict[str, Any]]) -> list[str]:
    """Render E against G, the two rules that differ only in where they measure from."""
    lines = [
        "#### 5. E against G — the same cooldown, one reference apart",
        "",
        "| ASSET | E TRADES | G TRADES | E DD | G DD | E BLOCKED | G BLOCKED | E HALTS | G HALTS "
        "| E REOPENED | G REOPENED |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for asset in ASSETS:
        e = next((r for r in rows if r["raw"] == asset.raw and r["policy"] == "E"), None)
        g = next((r for r in rows if r["raw"] == asset.raw and r["policy"] == "G"), None)
        if e is None or g is None:
            continue
        lines.append(
            f"| {asset.symbol} | {e['trades']} | {g['trades']} "
            f"| {pct(e['max_drawdown'], signed=False)} | {pct(g['max_drawdown'], signed=False)} "
            f"| {pct(e['blocked'], signed=False)} | {pct(g['blocked'], signed=False)} "
            f"| {e['halts']} | {g['halts']} "
            f"| {e['effective']}/{e['reopenings']} | {g['effective']}/{g['reopenings']} |"
        )
    lines += [
        "",
        "Under the harshest cost scenario, where the account does not recover on its "
        "own, the two separate:",
        "",
    ]
    lines += [
        "| ASSET | POLICY | STRESS TRADES | STRESS NET | STRESS BLOCKED | STRESS HALTS |",
        "|---|---|---|---|---|---|",
    ]
    for asset in ASSETS:
        for key in ("C", "E", "G"):
            evidence = study.stress.get((asset.raw, key))
            if evidence is None:
                continue
            worst = (evidence.get("stress") or [])[-1:]
            for entry in worst:
                card = entry.get("card") or {}
                lines.append(
                    f"| {asset.symbol} | {key} | {card.get('trades', DASH)} "
                    f"| {pct(card.get('total_return'))} "
                    f"| {pct(entry.get('blocked_time_share'), signed=False)} "
                    f"| {entry.get('halt_count', DASH)} |"
                )
    return [*lines, ""]


def section_choice(policies: list[PolicyRow], rows: list[dict[str, Any]]) -> list[str]:
    """Render the recommendation, and why each policy was or was not eligible."""
    baseline = next((p.worst_drawdown for p in policies if p.key == "C"), None)
    chosen, notes = recommend(policies, baseline_drawdown=baseline or Decimal("0.10"))
    lines = [
        "#### 6. The choice — damage, then predictability, then continuity",
        "",
        *[f"* {note}" for note in notes],
        "",
        f"**Recommended recovery rule: {chosen or 'none of them'}**"
        + (
            ""
            if chosen is None
            else " — chosen on continuity and time blocked, with return never consulted."
        ),
        "",
    ]
    if chosen is not None:
        lines += [
            f"##### regime_trend 4h under policy {chosen}, cross-asset",
            "",
            "| ASSET | TRADES | NET | PF | DD | BLOCKED | SWING |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in [r for r in rows if r["policy"] == chosen]:
            lines.append(
                f"| {row['symbol']} | {row['trades']} | {pct(row['net'])} "
                f"| {num(row['profit_factor'])} | {pct(row['max_drawdown'], signed=False)} "
                f"| {pct(row['blocked'], signed=False)} | {pct(row['swing'], signed=False)} |"
            )
        pooled = pool([r["card"] for r in rows if r["policy"] == chosen])
        nets = [r["net"] for r in rows if r["policy"] == chosen]
        lines += [
            "",
            f"Pooled: {pooled.trades} trades, profit factor {num(pooled.profit_factor)}, "
            f"{sum(1 for n in nets if n > 0)}/{len(nets)} markets positive, "
            f"median {pct(statistics.median(nets) if nets else None)}.",
            "",
        ]
    return lines


def section_stress(study: Study, rows: list[dict[str, Any]]) -> list[str]:
    """Render the cost scenarios, where they were run."""
    have = [r for r in rows if r["stress"] is not None]
    if not have:
        return []
    lines = [
        "#### 7. Stress — M16's three cost scenarios",
        "",
        "| ASSET | POLICY | BASE | " + " | ".join(STRESS_LABELS) + " |",
        "|---" * (len(STRESS_LABELS) + 3) + "|",
    ]
    for row in have:
        evidence = study.stress.get((row["raw"], row["policy"])) or {}
        cells = [
            pct(entry["card"]["total_return"]) if entry.get("card") else DASH
            for entry in evidence.get("stress") or []
        ]
        lines.append(
            f"| {row['symbol']} | {row['policy']} | {pct(row['net'])} | " + " | ".join(cells) + " |"
        )
    return [*lines, ""]


def main() -> int:
    """Write the report and the machine-readable policy summary."""
    study = Study()
    rows = market_rows(study)
    if not rows:
        sys.stderr.write("no evidence found under var/research/m17\n")
        return 1
    policies = policy_rows(rows)
    lines = [
        *section_rules(),
        *section_policies(policies, rows),
        *section_markets(rows),
        *section_continuity(rows),
        *section_xrp(rows),
        *section_head_to_head(study, rows),
        *section_choice(policies, rows),
        *section_stress(study, rows),
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "policies.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
