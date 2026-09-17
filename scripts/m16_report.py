"""Turn M16's evidence into one report: per asset, pooled, and what survives.

Reads ``var/research/m16/<ASSET>/{c,a,bench,stress,neigh,common,wf}/evidence.json`` and the
dataset's own validation report, and writes ``var/research/m16/REPORT.md`` next to a machine
readable ``verdicts.json``.

The rules it applies were fixed in :mod:`quantplatform.research.m16` before any cross-asset
result existed:

* **the verdict** is :func:`~quantplatform.research.sprint.judge` on each asset's own run,
  then :func:`~quantplatform.research.m15.cap_by_sample`, so a thin sample cannot become a
  PAPER CANDIDATE however good its ratios look;
* **the pooled sample** adds every asset's trades and rebuilds the ratios from the totals —
  reported because "is the sample big enough" is a question about the strategy, but never
  used to award a verdict, which stays per market;
* **discarding** follows :func:`~quantplatform.research.m16.discard_reason`.

Usage::

    uv run python scripts/m16_report.py
"""

from __future__ import annotations

import json
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.m15 import MIN_PAPER_TRADES, MIN_TRADES_PER_TEST_WINDOW, cap_by_sample
from quantplatform.research.m16 import (
    ASSETS,
    COMMON_START,
    DATA_END,
    Asset,
    discard_reason,
    pool,
)
from quantplatform.research.sprint import (
    MEANINGFUL_TRADE_SAMPLE,
    STRESS_LABELS,
    Evidence,
    Scorecard,
    judge,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m16"
DATASET_REPORT = ROOT / "data/raw/m16/validation_report.json"
CAPITAL = Decimal(10_000)
DASH = "—"
OOS_YEAR = 2025
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
    """Everything M16 recorded, indexed by asset."""

    def __init__(self) -> None:
        """Load every job's evidence, tolerating jobs that were not run."""
        self.jobs: dict[tuple[str, str], dict[str, Any]] = {}
        for asset in ASSETS:
            for job in ("c", "a", "bench", "stress", "neigh", "common", "wf"):
                found = _json(OUT / asset.raw / job / "evidence.json")
                if found is not None:
                    self.jobs[(asset.raw, job)] = found
        self.dataset = _json(DATASET_REPORT) or {}

    def card(self, asset: Asset, job: str) -> Card | None:
        """Return the scorecard of a single-run job."""
        evidence = self.jobs.get((asset.raw, job))
        return None if evidence is None else (evidence.get("run") or {}).get("card")

    def per_year(self, asset: Asset, job: str = "c") -> list[dict[str, Any]]:
        """Return the continuous run split at each year end."""
        evidence = self.jobs.get((asset.raw, job))
        return [] if evidence is None else (evidence.get("run") or {}).get("per_year") or []

    def benchmarks(self, asset: Asset) -> dict[str, Card | None]:
        """Return each benchmark's scorecard on this market."""
        evidence = self.jobs.get((asset.raw, "bench")) or {}
        runs = evidence.get("runs") or {}
        return {name: (entry or {}).get("card") for name, entry in runs.items()}

    def stress(self, asset: Asset) -> list[Card | None]:
        """Return the three cost scenarios, in the order they are labelled."""
        evidence = self.jobs.get((asset.raw, "stress")) or {}
        entries = evidence.get("stress") or []
        return [entry.get("card") for entry in entries]

    def neighbours(self, asset: Asset) -> list[Card | None]:
        """Return the declared neighbours' scorecards."""
        evidence = self.jobs.get((asset.raw, "neigh")) or {}
        return [entry.get("card") for entry in evidence.get("neighbours") or []]

    def folds(self, asset: Asset) -> list[tuple[int, Card | None]]:
        """Return the walk-forward test windows, in order."""
        evidence = self.jobs.get((asset.raw, "wf")) or {}
        return [
            (int(fold["start"][:4]), fold.get("card"))
            for fold in evidence.get("folds") or []
            if fold["role"] == "walk_forward_test"
        ]

    def blocked(self, asset: Asset, job: str = "c") -> Decimal | None:
        """Return the share of time the latch held new exposure off."""
        evidence = self.jobs.get((asset.raw, job))
        return (
            None if evidence is None else _d((evidence.get("run") or {}).get("blocked_time_share"))
        )


def _evidence_for(study: Study, asset: Asset) -> tuple[Evidence, list[int], list[Decimal]] | None:
    """Assemble exactly what the verdict is allowed to look at for one market."""
    card = study.card(asset, "c")
    if card is None:
        return None
    folds = study.folds(asset)
    returns = [Decimal(str(c["total_return"])) for _, c in folds if c is not None]
    trades = [int(c["trades"]) for _, c in folds if c is not None]
    out_of_sample = next((c for year, c in folds if year == OOS_YEAR and c is not None), None)
    stress = [Decimal(str(c["total_return"])) for c in study.stress(asset) if c is not None]
    neighbour_factors = [
        Decimal(str(c["profit_factor"])) if c.get("profit_factor") is not None else Decimal(0)
        for c in study.neighbours(asset)
        if c is not None
    ]
    benchmark_returns = [
        Decimal(str(c["total_return"])) for c in study.benchmarks(asset).values() if c is not None
    ]
    deployed = study.card(asset, "a")
    evidence = Evidence(
        full=Scorecard.model_validate(
            {k: v for k, v in card.items() if k in Scorecard.model_fields}
        ),
        out_of_sample_return=_d(None if out_of_sample is None else out_of_sample["total_return"]),
        walk_forward_positive_share=(
            Decimal(sum(1 for r in returns if r > 0)) / len(returns) if returns else None
        ),
        walk_forward_median_return=(Decimal(str(statistics.median(returns))) if returns else None),
        stress_worst_return=min(stress) if stress else None,
        neighbours_min_profit_factor=min(neighbour_factors) if neighbour_factors else None,
        benchmark_best_return=max(benchmark_returns) if benchmark_returns else None,
        deployed_return=_d(None if deployed is None else deployed["total_return"]),
    )
    return evidence, trades, returns


def rows(study: Study) -> list[dict[str, Any]]:
    """Return one row per asset: its KPIs, its verdict and whether it is discarded."""
    out: list[dict[str, Any]] = []
    for asset in ASSETS:
        assembled = _evidence_for(study, asset)
        if assembled is None:
            continue
        evidence, fold_trades, fold_returns = assembled
        card = study.card(asset, "c") or {}
        years = study.per_year(asset)
        verdict = cap_by_sample(
            judge(evidence), trades=evidence.full.trades, window_trades=fold_trades
        )
        out.append(
            {
                "symbol": asset.symbol,
                "raw": asset.raw,
                "start": asset.start.date().isoformat(),
                "card": card,
                "common": study.card(asset, "common"),
                "costs": (Decimal(str(card["fees"])) + Decimal(str(card["slippage"]))) / CAPITAL,
                "per_year": years,
                "positive_years": sum(
                    1 for y in years if y["return"] is not None and Decimal(str(y["return"])) > 0
                ),
                "years": len(years),
                "wf_positive": sum(1 for r in fold_returns if r > 0),
                "wf_total": len(fold_returns),
                "wf_min_trades": min(fold_trades) if fold_trades else 0,
                "worst_stress": evidence.stress_worst_return,
                "neighbour_min_pf": evidence.neighbours_min_profit_factor,
                "benchmark_best": evidence.benchmark_best_return,
                "benchmark_cards": study.benchmarks(asset),
                "deployed_return": evidence.deployed_return,
                "blocked": study.blocked(asset),
                "verdict": verdict.value,
                "discard": discard_reason(
                    trades=evidence.full.trades,
                    net=evidence.full.total_return,
                    profit_factor=evidence.full.profit_factor,
                    worst_stress=evidence.stress_worst_return,
                ),
            }
        )
    return out


def section_dataset(study: Study) -> list[str]:
    """Render the dataset checks, per asset."""
    lines = [
        "#### 0. Datasets",
        "",
        "| ASSET | FROM | 1H BARS | MISSING (OUTAGES) | ARCHIVES | 4H BARS | VS BINANCE'S OWN 4H "
        "| ANOMALIES |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for asset in ASSETS:
        report = (study.dataset.get("assets") or {}).get(asset.raw)
        if report is None:
            continue
        official = report.get("cross_check_official_4h") or {}
        archives = report.get("archives") or {}
        confirmation = report.get("outage_confirmation") or {}
        lines.append(
            f"| {asset.symbol} | {report['start'][:10]} | {report['actual_bar_count']} "
            f"| {report['missing_bar_count']} ({confirmation.get('absent_on_rest', DASH)} "
            f"confirmed absent on REST) "
            f"| {archives.get('monthly', 0)}m + {archives.get('daily', 0)}d, "
            f"{archives.get('checksums_verified', 0)} checksums "
            f"| {(report['outputs']['4h'])['bars']} "
            f"| {official.get('identical', DASH)}/{official.get('binance_bars', DASH)} identical, "
            f"{official.get('unexplained_count', DASH)} unexplained "
            f"| {len(report.get('anomalies') or [])} |"
        )
    digests = []
    for asset in ASSETS:
        report = (study.dataset.get("assets") or {}).get(asset.raw) or {}
        digest = (report.get("outputs") or {}).get("4h", {}).get("bars_digest", DASH)
        digests.append(f"{asset.symbol} `{digest}`")
    lines += ["", "4h digests: " + " · ".join(digests), ""]
    return lines


def section_per_asset(table: list[dict[str, Any]]) -> list[str]:
    """Render the headline table: one row per market, over its own history."""
    lines = [
        "#### 3. Every asset in full (policy C, its own history)",
        "",
        "| ASSET | FROM | TRADES | NET | PF | EXP | MAX DD | COSTS | LOSS STREAK | +YEARS | WF "
        "| WORST STRESS | VERDICT |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in table:
        card = row["card"]
        lines.append(
            f"| {row['symbol']} | {row['start'][:7]} | {card['trades']} "
            f"| {pct(card['total_return'])} | {num(card['profit_factor'])} "
            f"| {num(card['expectancy'])} | {pct(card['max_drawdown'], signed=False)} "
            f"| {pct(row['costs'], signed=False)} | {card.get('max_consecutive_losses', DASH)} "
            f"| {row['positive_years']}/{row['years']} | {row['wf_positive']}/{row['wf_total']} "
            f"| {pct(row['worst_stress'])} | {row['verdict']} |"
        )
    return [*lines, ""]


def _ema(row: dict[str, Any]) -> Decimal | None:
    """Return the EMA benchmark's net return on this market, or None if it did not run."""
    card = (row.get("benchmark_cards") or {}).get("ema_trend_mtf")
    return None if card is None else Decimal(str(card["total_return"]))


def section_asked(table: list[dict[str, Any]]) -> list[str]:
    """Render the table the milestone asked for, one line per market."""
    lines = [
        "#### 1. The table",
        "",
        "| ASSET | TRADES | NET | PF | DD | WF | STRESS | EMA BENCHMARK | VERDICT |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in table:
        card = row["card"]
        ema = _ema(row)
        beats = (
            "" if ema is None else (" ✅" if Decimal(str(card["total_return"])) > ema else " ❌")
        )
        low = " · LOW SAMPLE" if card["trades"] < MEANINGFUL_TRADE_SAMPLE else ""
        lines.append(
            f"| {row['symbol']} | {card['trades']}{low} | {pct(card['total_return'])} "
            f"| {num(card['profit_factor'])} | {pct(card['max_drawdown'], signed=False)} "
            f"| {row['wf_positive']}/{row['wf_total']} | {pct(row['worst_stress'])} "
            f"| {pct(ema)}{beats} | {row['verdict']} |"
        )
    lines += [
        "",
        "NET, PF, DD and STRESS are policy C over each market's own history; STRESS is the worst "
        "of the three cost scenarios. ✅/❌ is whether regime_trend beat the EMA benchmark **on "
        "that same market**, which is a different question from being positive.",
        "",
    ]
    return lines


def section_answers(table: list[dict[str, Any]]) -> list[str]:
    """Answer the milestone's questions with counts, not adjectives."""
    total = len(table)
    traded = [r for r in table if r["card"]["trades"] > 0]
    positive = [r for r in traded if Decimal(str(r["card"]["total_return"])) > 0]
    factor = [
        r
        for r in traded
        if r["card"]["profit_factor"] is not None and Decimal(str(r["card"]["profit_factor"])) > 1
    ]
    stressed = [r for r in traded if r["worst_stress"] is not None and r["worst_stress"] > 0]
    beat = []
    for row in traded:
        ema = _ema(row)
        if ema is not None and Decimal(str(row["card"]["total_return"])) > ema:
            beat.append(row)
    enough = [r for r in traded if r["card"]["trades"] >= MEANINGFUL_TRADE_SAMPLE]
    pooled = pool([r["card"] for r in table])
    return [
        "#### 2. The answers",
        "",
        f"1. **Does it work on several markets?** It is profitable on {len(positive)}/{total} "
        f"and loses money on {total - len(positive)}.",
        f"2. **Profit factor above 1:** {len(factor)}/{total}.",
        f"3. **Still positive under the cost stress:** {len(stressed)}/{total}.",
        f"4. **Beats the EMA benchmark on the same market:** {len(beat)}/{total}. "
        "Being profitable and being better than the benchmark are different bars; the verdict "
        "needs the second one.",
        f"5. **Pooled sample:** {pooled.trades} trades across {total} markets "
        f"({len(enough)}/{total} markets on their own reach {MEANINGFUL_TRADE_SAMPLE} trades, "
        f"{sum(1 for r in traded if r['card']['trades'] >= MIN_PAPER_TRADES)}/{total} reach "
        f"{MIN_PAPER_TRADES}).",
        "",
    ]


def section_common(table: list[dict[str, Any]]) -> list[str]:
    """Render the same six markets over the one window they all share."""
    lines = [
        f"#### 4. The window every asset shares ({COMMON_START.date()} → {DATA_END.date()}, "
        "policy C)",
        "",
        "| ASSET | TRADES | NET | PF | EXP | MAX DD | COSTS |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in table:
        card = row["common"]
        if card is None:
            lines.append(
                f"| {row['symbol']} | {DASH} | {DASH} | {DASH} | {DASH} | {DASH} | {DASH} |"
            )
            continue
        costs = (Decimal(str(card["fees"])) + Decimal(str(card["slippage"]))) / CAPITAL
        lines.append(
            f"| {row['symbol']} | {card['trades']} | {pct(card['total_return'])} "
            f"| {num(card['profit_factor'])} | {num(card['expectancy'])} "
            f"| {pct(card['max_drawdown'], signed=False)} | {pct(costs, signed=False)} |"
        )
    return [*lines, ""]


def _pooled_lines(label: str, cards: list[Card], returns: list[Decimal]) -> list[str]:
    pooled = pool(cards)
    positive = sum(1 for r in returns if r > 0)
    median = Decimal(str(statistics.median(returns))) if returns else None
    return [
        f"* **{label}** — {pooled.trades} trades across {len(cards)} markets, "
        f"pooled profit factor {num(pooled.profit_factor)}, "
        f"pooled expectancy {num(pooled.expectancy)} per trade, "
        f"{positive}/{len(returns)} markets positive, median net {pct(median)}.",
    ]


def section_aggregate(table: list[dict[str, Any]]) -> list[str]:
    """Render the pooled sample, over both windows and with and without the discards."""
    own = [row["card"] for row in table]
    kept = [row["card"] for row in table if row["discard"] is None]
    common = [row["common"] for row in table if row["common"] is not None]
    lines = ["#### 5. The sample, pooled across markets", ""]
    lines += _pooled_lines(
        "Each asset's own history", own, [Decimal(str(c["total_return"])) for c in own]
    )
    lines += _pooled_lines(
        "The common window", common, [Decimal(str(c["total_return"])) for c in common]
    )
    if kept and len(kept) != len(own):
        lines += _pooled_lines(
            "Kept markets only", kept, [Decimal(str(c["total_return"])) for c in kept]
        )
    total = pool(own).trades
    lines += [
        "",
        f"A pooled {total} trades is {'past' if total >= MIN_PAPER_TRADES else 'short of'} the "
        f"{MIN_PAPER_TRADES}-trade bar — but pooling is not what the verdict counts: every asset "
        "is judged on its own sample, because one market's run cannot vouch for another's.",
        "",
    ]
    return lines


def section_walk_forward(study: Study) -> list[str]:
    """Render each market's test years."""
    years = sorted({year for asset in ASSETS for year, _ in study.folds(asset)})
    lines = [
        "#### 6. Walk-forward — train one year, test the next (policy C)",
        "",
        "| ASSET | " + " | ".join(str(year) for year in years) + " | POSITIVE |",
        "|---" * (len(years) + 2) + "|",
    ]
    for asset in ASSETS:
        folds = dict(study.folds(asset))
        cells = []
        positive = total = 0
        for year in years:
            card = folds.get(year)
            if card is None:
                cells.append(DASH)
                continue
            total += 1
            positive += 1 if Decimal(str(card["total_return"])) > 0 else 0
            cells.append(f"{pct(card['total_return'])} ({card['trades']})")
        lines.append(f"| {asset.symbol} | " + " | ".join(cells) + f" | {positive}/{total} |")
    return [*lines, ""]


def section_stress(study: Study) -> list[str]:
    """Render every market under the three cost scenarios."""
    lines = [
        "#### 7. Stress — the same runs under worse costs (policy C)",
        "",
        "| ASSET | BASE | " + " | ".join(STRESS_LABELS) + " |",
        "|---" * (len(STRESS_LABELS) + 2) + "|",
    ]
    for asset in ASSETS:
        base = study.card(asset, "c")
        if base is None:
            continue
        cells = [
            pct(card["total_return"]) if card is not None else DASH for card in study.stress(asset)
        ]
        lines.append(
            f"| {asset.symbol} | {pct(base['total_return'])} | " + " | ".join(cells) + " |"
        )
    return [*lines, ""]


def section_years(study: Study) -> list[str]:
    """Render per-year stability, cut from each continuous run."""
    years = sorted({y["year"] for asset in ASSETS for y in study.per_year(asset)})
    lines = [
        "#### 8. Stability by calendar year (net · trades, cut from the continuous run)",
        "",
        "| ASSET | " + " | ".join(str(year) for year in years) + " |",
        "|---" * (len(years) + 1) + "|",
    ]
    for asset in ASSETS:
        by_year = {entry["year"]: entry for entry in study.per_year(asset)}
        cells = []
        for year in years:
            entry = by_year.get(year)
            cells.append(DASH if entry is None else f"{pct(entry['return'])} · {entry['trades']}")
        lines.append(f"| {asset.symbol} | " + " | ".join(cells) + " |")
    return [*lines, ""]


def section_verdicts(table: list[dict[str, Any]]) -> list[str]:
    """Render what each market is judged to be, and what is discarded."""
    lines = [
        "#### 9. Verdicts and discards",
        "",
        "| ASSET | VERDICT | WF MIN TRADES | NEIGHBOUR MIN PF | BEST BENCHMARK | UNDER RISK V2 "
        "| BLOCKED | DISCARD |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in table:
        lines.append(
            f"| {row['symbol']} | {row['verdict']} | {row['wf_min_trades']} "
            f"| {num(row['neighbour_min_pf'])} | {pct(row['benchmark_best'])} "
            f"| {pct(row['deployed_return'])} | {pct(row['blocked'], signed=False)} "
            f"| {row['discard'] or 'kept'} |"
        )
    discarded = [row for row in table if row["discard"] is not None]
    kept = [row for row in table if row["discard"] is None]
    lines += [
        "",
        f"**Discard:** {', '.join(r['symbol'] for r in discarded) if discarded else 'nothing'}.",
        f"**Keep:** {', '.join(r['symbol'] for r in kept) if kept else 'nothing'}.",
        "",
        f"PAPER CANDIDATE additionally needs {MIN_PAPER_TRADES} trades in the market's own run "
        f"and {MIN_TRADES_PER_TEST_WINDOW} in every walk-forward test year.",
        "",
    ]
    return lines


def main() -> int:
    """Write the report and the machine-readable verdicts."""
    study = Study()
    table = rows(study)
    if not table:
        sys.stderr.write("no evidence found under var/research/m16\n")
        return 1
    lines = [
        *section_dataset(study),
        *section_asked(table),
        *section_answers(table),
        *section_per_asset(table),
        *section_common(table),
        *section_aggregate(table),
        *section_walk_forward(study),
        *section_stress(study),
        *section_years(study),
        *section_verdicts(table),
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "verdicts.json").write_text(json.dumps(table, indent=2, default=str), encoding="utf-8")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
