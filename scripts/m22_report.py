"""Read M22's evidence and print the tables the milestone is reported with.

Reads only ``var/research/m22/<MARKET>/<KEY>/evidence.json``. Computes nothing a run did not
record and decides nothing the protocol did not declare: the screen verdict comes from the
evidence file, where the run itself wrote it, and which families advance comes from
:func:`quantplatform.research.m22.forwarded`.

Usage::

    uv run python scripts/m22_report.py [--markets BTCUSDT,ETHUSDT]
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantplatform.research.m22 import (
    CANDIDATES_M22,
    MAX_FAMILIES_FORWARD,
    SCREEN_ASSETS,
    EdgeFamily,
    forwarded,
    ranking_key,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/research/m22"

REFERENCE_KEYS = ("bench_ema", "bench_breakout", "incumbent")


def _load(market: str, key: str) -> dict[str, Any] | None:
    path = OUT / market / key / "evidence.json"
    if not path.exists():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _d(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _pct(value: object, places: str = "0.01") -> str:
    number = _d(value)
    if number is None:
        return "—"
    return f"{(number * 100).quantize(Decimal(places))}%"


def _num(value: object, places: str = "0.01") -> str:
    number = _d(value)
    return "—" if number is None else str(number.quantize(Decimal(places)))


def _years(evidence: dict[str, Any]) -> tuple[int, int]:
    rows = evidence["run"]["per_year"]
    counted = [r for r in rows if r["return"] is not None]
    return sum(1 for r in counted if Decimal(str(r["return"])) > 0), len(counted)


def _wf(evidence: dict[str, Any]) -> tuple[int, int, Decimal | None]:
    walk = evidence["walk_forward"]
    return walk["positive"], walk["tested"], _d(walk.get("median_return"))


def _row(evidence: dict[str, Any]) -> str:
    card, hold = evidence["run"]["card"], evidence["run"]["holding"]
    if card is None:
        return (
            f"| {evidence['family']} | {evidence['key']} | {evidence['strategy_id']} | "
            f"no result ({evidence['run']['status']}) |" + " — |" * 12
        )
    positive, total = _years(evidence)
    wf_positive, wf_total, _ = _wf(evidence)
    average_win, average_loss = _d(card["average_win"]), _d(card["average_loss"])
    ratio = (
        "—"
        if average_win is None or not average_loss
        else str((average_win / abs(average_loss)).quantize(Decimal("0.01")))
    )
    return " | ".join(
        (
            "",
            evidence["family"],
            evidence["key"],
            str(card["trades"]),
            _pct(card["total_return"]),
            _pct(card["max_drawdown"]),
            _num(card["profit_factor"]),
            _num(card["expectancy"]),
            _pct(card["win_rate"], "0.1"),
            ratio,
            _pct(card["time_in_market"], "0.1"),
            _num(card["fees"], "0.01"),
            f"{positive}/{total}",
            f"{wf_positive}/{wf_total}",
            f"{hold['median_bars']}·{_pct(hold['at_time_stop_share'], '0.1')}",
            "**yes**" if evidence["shows_signal"] else "no",
            "",
        )
    ).strip()


HEADER = (
    "| FAMILY | KEY | TRADES | NET | MAX DD | PF | EXPECT | WIN% | W/L | EXPOSURE | FEES | "
    "YEARS+ | WF+ | HOLD·CAP | SIGNAL |"
)
RULE = "|" + "---|" * 15


def _table(market: str, keys: tuple[str, ...]) -> list[str]:
    lines = [f"### {market}", "", HEADER, RULE]
    for key in keys:
        evidence = _load(market, key)
        lines.append(_row(evidence) if evidence else f"| — | {key} | not run |" + " — |" * 12)
    lines.append("")
    return lines


def _emit(lines: list[str]) -> None:
    """Write the report beside the evidence and to stdout, as every milestone here does."""
    text = "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "REPORT.md").write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def main() -> int:
    """Print the screen tables, then what the declared rules make of them."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default=",".join(SCREEN_ASSETS))
    args = parser.parse_args()
    markets = tuple(m for m in args.markets.split(",") if m)

    candidate_keys = tuple(v.key for v in CANDIDATES_M22)
    lines: list[str] = ["## M22 phase one — BTC and ETH at 4h", ""]
    for market in markets:
        lines += _table(market, candidate_keys)
        lines += _table(market, REFERENCE_KEYS)

    passing: set[tuple[str, str]] = set()
    for market in markets:
        for key in candidate_keys:
            evidence = _load(market, key)
            if evidence and evidence["shows_signal"]:
                passing.add((key, market))

    lines += ["## What the declared rules make of it", ""]
    lines.append(
        f"Configurations clearing the screen gate: {len(passing)} of "
        f"{len(candidate_keys) * len(markets)}."
    )
    advancing = forwarded(passing)
    if not advancing:
        lines += [
            "",
            "**No family advances.** No single configuration cleared the gate on both BTC and "
            "ETH, which is the STOP condition declared before the run.",
        ]
        _emit(lines)
        return 0

    lines += [
        "",
        "| FAMILY | CARRIED BY | POSITIVE YEARS | WF SHARE | TRADES |",
        "|---|---|---|---|---|",
    ]
    ranked: list[tuple[tuple[int, Decimal, int], EdgeFamily, str]] = []
    for family in advancing:
        keys = [v.key for v in CANDIDATES_M22 if v.family is family]
        carried = [k for k in keys if all((k, m) in passing for m in markets)]
        for key in carried:
            years = sum(_years(_load(m, key) or {"run": {"per_year": []}})[0] for m in markets)
            shares, trades = [], 0
            for market in markets:
                evidence = _load(market, key)
                if evidence is None:
                    continue
                positive, total, _ = _wf(evidence)
                shares.append(Decimal(positive) / Decimal(total) if total else Decimal(0))
                trades += int(evidence["run"]["card"]["trades"])
            share = sum(shares, Decimal(0)) / Decimal(len(shares)) if shares else Decimal(0)
            ranked.append(
                (
                    ranking_key(positive_years=years, walk_forward_share=share, trades=trades),
                    family,
                    key,
                )
            )
            lines.append(
                f"| {family.value} | {key} | {years} | {share.quantize(Decimal('0.01'))} | "
                f"{trades} |"
            )

    best: dict[EdgeFamily, tuple[tuple[int, Decimal, int], str]] = {}
    for key_value, family, key in ranked:
        if family not in best or key_value > best[family][0]:
            best[family] = (key_value, key)
    order = sorted(best.items(), key=lambda item: item[1][0], reverse=True)
    picked = [family for family, _ in order[:MAX_FAMILIES_FORWARD]]
    lines += [
        "",
        f"**Advancing to BNB and SOL** (at most {MAX_FAMILIES_FORWARD}, ranked by positive "
        f"years then walk-forward share then sample, never by return): "
        + ", ".join(f"`{f.value}`" for f in picked),
    ]
    if len(order) > MAX_FAMILIES_FORWARD:
        dropped = [f.value for f, _ in order[MAX_FAMILIES_FORWARD:]]
        lines.append(f"Cleared the gate but not carried, for compute: {', '.join(dropped)}.")
    _emit(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
