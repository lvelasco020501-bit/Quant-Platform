"""Derive the 1d series M29 screens on, from the 1h bars M16 already validated.

**Nothing is downloaded.** M16's pipeline downloads 1h bars, validates them against two
checksummed archives plus the exchange API, and then *derives* 4h by resampling — it never
fetched a 4h series. 1d is produced here by the same function, from the same validated 1h
CSVs already on disk, so the daily bars inherit M16's validation rather than needing their
own. Re-downloading would introduce a second lineage for no gain and some risk.

Three things are checked before any bar is written, because a dataset nobody verified is
worse than no dataset:

1. **Lineage.** Each 1h CSV's sha256 is compared with the one M16 recorded. If a file was
   touched since, this stops.
2. **Outages.** The missing hours are recomputed from the CSV and their count held to the
   ``documented_absences`` M16 confirmed against REST. Resampling is allowed to skip only
   those, so a bucket short of a bar for any other reason raises instead of being averaged
   over.
3. **Determinism.** The resample runs twice and the digests must match — the same check
   M16 runs on its own 4h output.

BTC gets a fourth: M15 produced its own 1d series independently, and the overlap must agree
bar for bar. That is the only check here that could catch an error in the resampler itself.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.research.digest import bars_digest
from quantplatform.research.m29 import ASSETS
from quantplatform.research.resample import resample_bars

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
M16: Final[Path] = ROOT / "data/raw/m16"
OUT: Final[Path] = ROOT / "data/raw/m29/out"
REPORT: Final[Path] = ROOT / "data/raw/m29/dataset_report.json"
M15_DAILY: Final[Path] = ROOT / "data/raw/m15/out/BTCUSDT_1d_2020-01-01_2026-09-15.csv"
HOUR: Final[timedelta] = timedelta(hours=1)


def read_csv(path: Path) -> tuple[MarketBar, ...]:
    """Read a canonical CSV back into bars, re-validating every row through the model."""
    bars: list[MarketBar] = []
    with path.open(encoding="utf-8") as handle:
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
                    quote_volume=Decimal(row["quote_volume"]) if row.get("quote_volume") else None,
                    trade_count=int(row["trade_count"]) if row.get("trade_count") else None,
                    # M16's canonical CSV carries no source column; the provenance of these
                    # bars is the M16 report, checked by sha256 above, not a per-row label.
                    source=row.get("source") or "m16_resampled",
                    is_closed=True,
                )
            )
    return tuple(bars)


def write_csv(path: Path, bars: tuple[MarketBar, ...]) -> str:
    """Write bars in the same canonical column order M16 writes, and return the sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "symbol",
        "market_type",
        "timeframe",
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for bar in bars:
            writer.writerow(
                [
                    bar.symbol,
                    bar.market_type.value,
                    bar.timeframe.value,
                    bar.open_time.isoformat(),
                    bar.close_time.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    "" if bar.quote_volume is None else bar.quote_volume,
                    "" if bar.trade_count is None else bar.trade_count,
                ]
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def missing_hours(bars: tuple[MarketBar, ...]) -> frozenset[datetime]:
    """Return every hour absent from the continuous grid the series spans."""
    present = {bar.open_time for bar in bars}
    cursor, last, gaps = bars[0].open_time, bars[-1].open_time, []
    while cursor < last:
        cursor += HOUR
        if cursor not in present:
            gaps.append(cursor)
    return frozenset(gaps)


def main() -> int:
    """Derive, verify and write one 1d series per declared asset."""
    report_m16 = json.loads((M16 / "validation_report.json").read_text(encoding="utf-8"))["assets"]
    out: dict[str, Any] = {"derived_from": "data/raw/m16 1h, resampled — nothing downloaded"}
    failures: list[str] = []

    for raw in ASSETS:
        recorded = report_m16[raw]
        source = ROOT / recorded["outputs"]["1h"]["path"]
        entry: dict[str, Any] = {"source": str(source.relative_to(ROOT))}

        actual_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        entry["lineage"] = {
            "expected_sha256": recorded["outputs"]["1h"]["csv_sha256"],
            "actual_sha256": actual_sha,
            "matches": actual_sha == recorded["outputs"]["1h"]["csv_sha256"],
        }
        if not entry["lineage"]["matches"]:
            failures.append(f"{raw}: 1h csv sha256 does not match what M16 recorded")
            out[raw] = entry
            continue

        hourly = read_csv(source)
        absences = missing_hours(hourly)
        entry["outages"] = {
            "recomputed": len(absences),
            "documented_by_m16": recorded["documented_absences"]["hours"],
            "matches": len(absences) == recorded["documented_absences"]["hours"],
        }
        if not entry["outages"]["matches"]:
            failures.append(f"{raw}: recomputed outages disagree with M16's documented count")
            out[raw] = entry
            continue

        first = resample_bars(hourly, Timeframe.D1, allowed_missing=absences)
        again = resample_bars(hourly, Timeframe.D1, allowed_missing=absences)
        entry["deterministic"] = bars_digest(first) == bars_digest(again)
        if not entry["deterministic"]:
            failures.append(f"{raw}: resample is not deterministic")
            out[raw] = entry
            continue

        path = OUT / f"{raw}_1d_{first[0].open_time.date()}_{first[-1].close_time.date()}.csv"
        entry["output"] = {
            "path": str(path.relative_to(ROOT)),
            "bars": len(first),
            "first_open_time": first[0].open_time.isoformat(),
            "last_open_time": first[-1].open_time.isoformat(),
            "csv_sha256": write_csv(path, first),
            "bars_digest": bars_digest(first),
        }

        if raw == "BTCUSDT" and M15_DAILY.exists():
            # The only check that can catch an error in the resampler itself: M15 built its
            # own daily series by a different route, and the overlap must agree exactly.
            theirs = {bar.open_time: bar for bar in read_csv(M15_DAILY)}
            ours = {bar.open_time: bar for bar in first if bar.open_time in theirs}
            same = sum(
                1
                for stamp, bar in ours.items()
                if (bar.open, bar.high, bar.low, bar.close)
                == (theirs[stamp].open, theirs[stamp].high, theirs[stamp].low, theirs[stamp].close)
            )
            entry["cross_check_m15_daily"] = {
                "overlapping_bars": len(ours),
                "identical_ohlc": same,
                "differing": len(ours) - same,
            }
            if len(ours) != same:
                failures.append(f"{raw}: derived 1d disagrees with M15's own daily series")

        out[raw] = entry
        sys.stdout.write(
            f"  {raw}: {len(first)} daily bars  digest={entry['output']['bars_digest']}\n"
        )
        sys.stdout.flush()

    out["failures"] = failures
    out["generated_at"] = datetime.now(UTC).isoformat()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    if failures:
        sys.stdout.write("FAILED:\n" + "\n".join(f"  {line}" for line in failures) + "\n")
        return 1
    sys.stdout.write(f"ok — report at {REPORT.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
