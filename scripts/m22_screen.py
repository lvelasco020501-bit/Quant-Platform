"""Run M22 phase one: twelve configurations and three references, on BTC and ETH at 4h.

Research only. Reads the canonical 4h CSVs under ``data/raw/m16/out/`` — the same dataset M16
built and verified against Binance's own klines — and writes evidence under
``var/research/m22/``. Touches no paper state, no production risk and no running session.

One job is one ``(market, configuration)`` pair, and it does two things: the continuous run
over the market's whole history under the research variant, and the walk-forward over that
same history, training on one year and testing the next. They are bundled because the
walk-forward is cheap next to the full run and splitting them would double the process count
for no gain — and process count is the thing being rationed here.

**Compute is deliberately constrained.** The backtest engine revalidates the whole history on
every bar, so a nine-year 4h run costs roughly twelve minutes of one core. Thirty jobs at two
workers is about three and a half hours. ``--workers`` defaults to 2 and the machine should be
watched rather than trusted: if it starts swapping, stop and come back at one.

Each job records, besides the scorecard, the two things that decide how its number should be
read — the per-year split, so a result carried by one year is visible, and the holding-time
distribution, so a result decided by the deployed seven-day time stop rather than by the
strategy's own exit rule is visible too.

Usage::

    uv run python scripts/m22_screen.py [--workers 2] [--only BTCUSDT] [--keys T1,T2]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from statistics import median
from typing import Any

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.research import ExperimentEngineFactory, code_revision
from quantplatform.research.definition import ExperimentDefinition
from quantplatform.research.folds import WalkForwardPlan, WindowSpec
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.m16 import DATA_END, Asset, walk_forward_folds_for
from quantplatform.research.m22 import (
    CANDIDATES_M22,
    REFERENCES,
    SCREEN_ASSETS,
    TIME_STOP_BARS,
    TIMEFRAME,
    asset_for,
    reference_definition,
    shows_signal,
    study_definition,
    walk_forward_summary,
)
from quantplatform.research.plan_runner import WalkForwardRunner
from quantplatform.research.result import ExperimentResult
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.sprint import Scorecard
from quantplatform.research.store import ResultStore
from quantplatform.strategies.research import build_research_registry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/m16/out"
OUT = ROOT / "var/research/m22"


@cache
def load(raw: str) -> tuple[MarketBar, ...]:
    """Read the canonical 4h CSV for one market. Every bar is re-validated by the harness."""
    asset = asset_for(raw)
    name = f"{raw}_{TIMEFRAME.value}_{asset.start.date()}_{DATA_END.date()}.csv"
    bars: list[MarketBar] = []
    with (DATA / name).open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            bars.append(
                MarketBar(
                    symbol=row["symbol"],
                    market_type=MarketType(row["market_type"]),
                    timeframe=Timeframe(row["timeframe"]),
                    open_time=datetime.fromisoformat(row["open_time"]),
                    close_time=datetime.fromisoformat(row["close_time"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                    trade_count=int(row["trade_count"]) if row["trade_count"] else None,
                    source="csv_historical",
                    is_closed=True,
                )
            )
    return tuple(bars)


def within(bars: tuple[MarketBar, ...], window: WindowSpec) -> tuple[MarketBar, ...]:
    """Return the bars whose open time falls inside ``window``."""
    return tuple(bar for bar in bars if window.contains(bar.open_time))


class Loader:
    """Serves walk-forward windows from bars already in memory."""

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        """Hold one market's bars for the runner to slice."""
        self._bars = bars

    def __call__(
        self, *, symbol: str, market_type: MarketType, timeframe: Timeframe, window: WindowSpec
    ) -> tuple[MarketBar, ...]:
        """Return the window's bars; each dataset holds one symbol and one market."""
        del symbol, market_type, timeframe
        return within(self._bars, window)


def _factory() -> ExperimentEngineFactory:
    return ExperimentEngineFactory(
        registry=build_research_registry(), features_for=features_for, quote_asset="USDT"
    )


def card(result: ExperimentResult | None) -> dict[str, Any] | None:
    """Return a run's scorecard plus the gross figures a pooled sample would be built from."""
    if result is None or result.performance is None:
        return None
    performance = result.performance
    out = Scorecard.from_performance(performance).model_dump()
    out["gross_profit"] = performance.trades.gross_profit
    out["gross_loss"] = performance.trades.gross_loss
    return out


def holding(result: ExperimentResult | None) -> dict[str, Any]:
    """Return how long trades were held, in bars, and how often the time stop ended them.

    The deployed configuration closes any position after :data:`TIME_STOP_BARS` bars. When
    most trades end exactly there, the exit under measurement is the platform's, not the
    strategy's — and a family's result says more about the time stop than about its rule.
    """
    if result is None or not result.trades:
        return {"trades": 0, "median_bars": None, "max_bars": None, "at_time_stop_share": None}
    seconds = TIMEFRAME.seconds
    bars = [int((t.closed_at - t.opened_at).total_seconds()) // seconds for t in result.trades]
    capped = sum(1 for held in bars if held >= TIME_STOP_BARS)
    return {
        "trades": len(bars),
        "median_bars": median(bars),
        "max_bars": max(bars),
        "at_time_stop_share": Decimal(capped) / Decimal(len(bars)),
    }


def per_year(result: ExperimentResult | None) -> list[dict[str, Any]]:
    """Return each calendar year's return and trade count, cut from the continuous run."""
    if result is None or result.performance is None:
        return []
    opening = result.performance.initial_equity
    out: list[dict[str, Any]] = []
    for year in sorted({point.at.year for point in result.equity_curve}):
        points = [p for p in result.equity_curve if p.at.year == year]
        closing = points[-1].equity if points else opening
        out.append(
            {
                "year": year,
                "return": (closing / opening - 1) if opening else None,
                "trades": sum(1 for t in result.trades if t.closed_at.year == year),
            }
        )
        opening = closing
    return out


def _s(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _s(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_s(v) for v in value]
    return value


def _screen(entry: dict[str, Any]) -> bool | None:
    """Apply the gate declared before any of this ran. ``None``: the run produced no card."""
    scorecard = entry.get("card")
    if scorecard is None:
        return None
    factor = scorecard["profit_factor"]
    return shows_signal(
        trades=int(scorecard["trades"]),
        total_return=Decimal(str(scorecard["total_return"])),
        profit_factor=None if factor is None else Decimal(str(factor)),
        max_drawdown=Decimal(str(scorecard["max_drawdown"])),
    )


def _walk_forward(
    definition: ExperimentDefinition, asset: Asset, home: Path, revision: str | None
) -> dict[str, Any]:
    outcome = WalkForwardRunner().run(
        definition,
        WalkForwardPlan(
            base_experiment_id=definition.experiment_id, folds=walk_forward_folds_for(asset)
        ),
        loader=Loader(load(asset.raw)),
        factory=_factory(),
        store=ResultStore(home / "wf_results"),
        ledger=ExperimentLedger(home / "wf_ledger.jsonl"),
        code_revision=revision,
    )
    folds: list[dict[str, Any]] = [
        {
            "index": run.entry.fold_index,
            "role": run.result.definition.role.value,
            "start": run.result.definition.dataset.start,
            "status": run.result.status.value,
            "card": card(run.result),
        }
        for run in outcome.folds
    ]
    return {
        "aborted": outcome.aborted,
        "abort_reason": outcome.abort_reason,
        "folds": folds,
        **walk_forward_summary(folds),
    }


def recompute(root: Path) -> int:
    """Rewrite every evidence file's walk-forward aggregate from the folds it already holds."""
    touched = 0
    for path in sorted(root.glob("*/*/evidence.json")):
        evidence = json.loads(path.read_text(encoding="utf-8"))
        walk = evidence.get("walk_forward")
        if not walk or not walk.get("folds"):
            continue
        walk.update(walk_forward_summary(walk["folds"]))
        path.write_text(json.dumps(_s(evidence), indent=2), encoding="utf-8")
        touched += 1
    return touched


def run_one(raw: str, key: str) -> dict[str, Any]:
    """Run one configuration on one market: the continuous history, then the walk-forward."""
    started = time.time()
    asset, revision = asset_for(raw), code_revision(ROOT)
    home = OUT / raw / key
    home.mkdir(parents=True, exist_ok=True)

    if key in {"bench_ema", "bench_breakout", "incumbent"}:
        reference = {
            "bench_ema": REFERENCES[0],
            "bench_breakout": REFERENCES[1],
            "incumbent": REFERENCES[2],
        }[key]
        definition = reference_definition(raw, reference)
        label = reference.strategy_id
        params = dict(reference.params)
        family = "reference"
    else:
        variant = next(v for v in CANDIDATES_M22 if v.key == key)
        definition = study_definition(raw, variant)
        label = variant.candidate.strategy_id
        params = dict(variant.candidate.params)
        family = variant.family.value

    bars = load(raw)
    result = ExperimentRunner().run(
        definition, bars=bars, factory=_factory(), code_revision=revision
    )
    ExperimentLedger(home / "ledger.jsonl").record(result, store=ResultStore(home / "results"))
    entry = {
        "experiment_id": definition.experiment_id,
        "name": definition.name,
        "status": result.status.value,
        "error": result.error,
        "card": card(result),
        "holding": holding(result),
        "per_year": per_year(result),
    }
    evidence: dict[str, Any] = {
        "milestone": "m22",
        "phase": "screen",
        "symbol": asset.symbol,
        "raw": raw,
        "key": key,
        "family": family,
        "strategy_id": label,
        "params": params,
        "timeframe": TIMEFRAME.value,
        "start": asset.start,
        "end": DATA_END,
        "bars": len(bars),
        "code_revision": revision,
        "run": entry,
        "walk_forward": _walk_forward(definition, asset, home, revision),
        "shows_signal": _screen(entry),
    }
    evidence["seconds"] = round(time.time() - started, 1)
    (home / "evidence.json").write_text(json.dumps(_s(evidence), indent=2), encoding="utf-8")
    return evidence


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


KEYS: tuple[str, ...] = (
    *(v.key for v in CANDIDATES_M22),
    "bench_ema",
    "bench_breakout",
    "incumbent",
)


def main() -> int:
    """Run every requested configuration, longest history first, on a small pool."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2, help="Keep this small; see module doc.")
    parser.add_argument("--only", default="", help="Comma-separated markets; default both.")
    parser.add_argument("--keys", default="", help="Comma-separated configurations; default all.")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="Run nothing; re-derive each evidence file's walk-forward aggregate from its folds.",
    )
    args = parser.parse_args()

    if args.recompute:
        _say(f"recomputed walk-forward for {recompute(OUT)} evidence files")
        return 0

    markets = [m for m in SCREEN_ASSETS if not args.only or m in args.only.split(",")]
    keys = [k for k in KEYS if not args.keys or k in args.keys.split(",")]
    jobs = [(raw, key) for raw in markets for key in keys]

    started, done, failed = time.time(), 0, 0
    runner: Callable[[str, str], dict[str, Any]] = run_one
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[dict[str, Any]], tuple[str, str]] = {
            pool.submit(runner, raw, key): (raw, key) for raw, key in jobs
        }
        for future in as_completed(futures):
            raw, key = futures[future]
            try:
                evidence = future.result()
            except Exception as exc:  # a crashed job is reported, never hidden
                failed += 1
                _say(f"FAILED {raw} {key}: {type(exc).__name__}: {exc}")
                continue
            done += 1
            scorecard = evidence["run"]["card"]
            summary = (
                f"net {scorecard['total_return']:>10} dd {scorecard['max_drawdown']:>8} "
                f"trades {scorecard['trades']:>4} signal {evidence['shows_signal']}"
                if scorecard
                else f"no card ({evidence['run']['status']})"
            )
            _say(
                f"[{time.time() - started:6.0f}s] {done:3}/{len(jobs)} {raw:8} {key:14} "
                f"({evidence['seconds']:6.0f}s) {summary}"
            )
    _say(f"\nfinished {done}/{len(jobs)} jobs, {failed} failed, after {time.time() - started:.0f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
