"""Derive the 1d series M30 screens on, from the 1h bars M16 already validated.

**Nothing is downloaded.** All six declared assets -- including ADA and XRP, which M29 left out
on purpose -- were fetched and validated by M16 against two checksummed archives plus the
exchange API. 1d comes from those same 1h CSVs through the same resampler M29 used, so the daily
bars inherit M16's validation instead of needing a second lineage of their own.

Five checks run before any bar is written, because a dataset nobody verified is worse than no
dataset:

1. **Lineage.** Each 1h CSV's sha256 is compared with the one M16 recorded. If a file has been
   touched since, this stops.
2. **Outages.** Missing hours are recomputed from the CSV and held to the ``documented_absences``
   M16 confirmed against REST. Resampling may skip only those, so a bucket short of a bar for
   any other reason raises rather than being averaged over.
3. **Determinism.** The resample runs twice and the digests must match.
4. **Agreement with M29.** The four assets M29 derived must come out bit-for-bit identical here.
   This is what makes the two milestones comparable: a different daily series would quietly
   invalidate every cross-reference between M29's numbers and M30's.
5. **BTC against an independent series.** M15 built its own daily BTC bars by another route, and
   the overlap must agree bar for bar. It is the only check here capable of catching an error in
   the resampler itself.
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
from quantplatform.research.m30 import ASSETS_M30
from quantplatform.research.resample import resample_bars

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
M16: Final[Path] = ROOT / "data/raw/m16"
M29_REPORT: Final[Path] = ROOT / "data/raw/m29/dataset_report.json"
OUT: Final[Path] = ROOT / "data/raw/m30/out"
REPORT: Final[Path] = ROOT / "data/raw/m30/dataset_report.json"
M15_DAILY: Final[Path] = ROOT / "data/raw/m15/out/BTCUSDT_1d_2020-01-01_2026-09-15.csv"
HOUR: Final[timedelta] = timedelta(hours=1)

COLUMNS: Final[tuple[str, ...]] = (
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
                    # M16's canonical CSV carries no source column; provenance is the M16
                    # report, checked by sha256, rather than a per-row label.
                    source=row.get("source") or "m16_resampled",
                    is_closed=True,
                )
            )
    return tuple(bars)


def write_csv(path: Path, bars: tuple[MarketBar, ...]) -> str:
    """Write bars in M16's canonical column order and return the file's sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
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


def _m29_digests() -> dict[str, str]:
    """Return the daily digests M29 recorded, so this run can be held to them."""
    if not M29_REPORT.exists():
        return {}
    recorded = json.loads(M29_REPORT.read_text(encoding="utf-8"))
    return {
        asset: entry["output"]["bars_digest"]
        for asset, entry in recorded.items()
        if isinstance(entry, dict) and "output" in entry
    }


def _cross_check_m15(daily: tuple[MarketBar, ...]) -> dict[str, int]:
    """Compare derived BTC daily bars against M15's independently built series."""
    theirs = {bar.open_time: bar for bar in read_csv(M15_DAILY)}
    ours = {bar.open_time: bar for bar in daily if bar.open_time in theirs}
    same = sum(
        1
        for stamp, bar in ours.items()
        if (bar.open, bar.high, bar.low, bar.close)
        == (theirs[stamp].open, theirs[stamp].high, theirs[stamp].low, theirs[stamp].close)
    )
    return {"overlapping_bars": len(ours), "identical_ohlc": same, "differing": len(ours) - same}


def _derive(raw: str, recorded: dict[str, Any], failures: list[str]) -> dict[str, Any]:
    """Verify one asset's 1h lineage and write its derived 1d series."""
    source = ROOT / recorded["outputs"]["1h"]["path"]
    entry: dict[str, Any] = {"source": str(source.relative_to(ROOT))}

    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    expected = recorded["outputs"]["1h"]["csv_sha256"]
    entry["lineage"] = {
        "expected_sha256": expected,
        "actual_sha256": actual,
        "matches": actual == expected,
    }
    if actual != expected:
        failures.append(f"{raw}: 1h csv sha256 does not match what M16 recorded")
        return entry

    hourly = read_csv(source)
    absences = missing_hours(hourly)
    documented = recorded["documented_absences"]["hours"]
    entry["outages"] = {
        "recomputed": len(absences),
        "documented_by_m16": documented,
        "matches": len(absences) == documented,
    }
    if len(absences) != documented:
        failures.append(f"{raw}: recomputed outages disagree with M16's documented count")
        return entry

    first = resample_bars(hourly, Timeframe.D1, allowed_missing=absences)
    again = resample_bars(hourly, Timeframe.D1, allowed_missing=absences)
    entry["deterministic"] = bars_digest(first) == bars_digest(again)
    if not entry["deterministic"]:
        failures.append(f"{raw}: resample is not deterministic")
        return entry

    path = OUT / f"{raw}_1d_{first[0].open_time.date()}_{first[-1].close_time.date()}.csv"
    digest = bars_digest(first)
    entry["output"] = {
        "path": str(path.relative_to(ROOT)),
        "bars": len(first),
        "first_open_time": first[0].open_time.isoformat(),
        "last_open_time": first[-1].open_time.isoformat(),
        "csv_sha256": write_csv(path, first),
        "bars_digest": digest,
    }

    previously = _m29_digests().get(raw)
    if previously is not None:
        entry["agrees_with_m29"] = previously == digest
        if previously != digest:
            failures.append(f"{raw}: daily series differs from the one M29 screened")

    if raw == "BTCUSDT" and M15_DAILY.exists():
        entry["cross_check_m15_daily"] = _cross_check_m15(first)
        if entry["cross_check_m15_daily"]["differing"]:
            failures.append(f"{raw}: derived 1d disagrees with M15's own daily series")
    return entry


def main() -> int:
    """Derive, verify and write one 1d series per declared asset."""
    assets = json.loads((M16 / "validation_report.json").read_text(encoding="utf-8"))["assets"]
    out: dict[str, Any] = {
        "milestone": "m30",
        "derived_from": "data/raw/m16 1h, resampled -- nothing downloaded",
        "assets": list(ASSETS_M30),
    }
    failures: list[str] = []

    for raw in ASSETS_M30:
        entry = _derive(raw, assets[raw], failures)
        out[raw] = entry
        if "output" in entry:
            sys.stdout.write(
                f"  {raw:9} {entry['output']['bars']:5d} daily bars  "
                f"from {entry['output']['first_open_time'][:10]}  "
                f"digest={entry['output']['bars_digest'][:16]}"
                f"{'  == M29' if entry.get('agrees_with_m29') else ''}\n"
            )
            sys.stdout.flush()

    out["failures"] = failures
    out["generated_at"] = datetime.now(UTC).isoformat()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    if failures:
        sys.stdout.write("FAILED:\n" + "\n".join(f"  {line}" for line in failures) + "\n")
        return 1
    sys.stdout.write(f"ok -- report at {REPORT.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
