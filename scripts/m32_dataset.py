"""Download and validate the markets M32's survivorship correction needs.

M30's six are **not** re-downloaded: M16 already fetched and validated them against checksummed
archives plus the live REST API, and re-acquiring them would create a second lineage for the very
assets whose numbers M32 has to reproduce. This script fetches only the twenty-three markets that
were missing -- the faded, the collapsed, and the later arrivals -- and derives 4h and 1d from
their hourly bars with the same resampler M16, M29 and M30 used.

The standard applied here is M16's, with one documented exception:

* monthly archives with their SHA-256 checksums verified, daily archives for any month not
  published, one bar per open time, rows refused rather than snapped when off the hourly grid
* **missing hours are recorded but not confirmed against the REST API.** M16 did confirm them,
  one asset at a time, and doing it for twenty-three markets is a volume of API calls this
  milestone does not need: these series feed a *liquidity ranking and a robustness check*, not a
  candidate's own track record. Long gaps are flagged as anomalies for reading rather than
  silently tolerated, and the difference in standard is stated in the report itself.

A market whose archives do not exist, or stop early, is **kept in the record as such**. A
delisting is data about what was tradeable; dropping it would reintroduce the survivorship bias
this dataset exists to remove.

Usage:
    uv run python scripts/m32_dataset.py [--only LTCUSDT,LUNAUSDT]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from quantplatform.core.enums import Timeframe
from quantplatform.research.digest import bars_digest
from quantplatform.research.m16 import DATA_END
from quantplatform.research.m32 import to_download
from quantplatform.research.resample import resample_bars
from quantplatform.research.vision import (
    Row,
    deduplicate,
    gap_runs,
    missing_hours,
    ohlcv_violations,
    parse_kline,
    to_market_bars,
    write_csv,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "data/raw/m32"
OUT: Final[Path] = HOME / "out"
REPORT: Final[Path] = HOME / "dataset_report.json"
VISION: Final[str] = "https://data.binance.vision/data/spot"
HTTP_NOT_FOUND: Final[int] = 404
EARLIEST: Final[date] = date(2017, 7, 1)
"""First month Binance published spot klines for anything. Probing from here discovers each
market's listing month instead of assuming it."""
LONG_GAP_HOURS: Final[int] = 24
"""A missing run at least this long is flagged for reading rather than quietly allowed."""
SPLICE_FACTOR: Final[Decimal] = Decimal(5)
"""Price ratio across a long gap above which the symbol is treated as having changed asset.

Binance reuses tickers. ``LUNAUSDT`` holds the original Terra token until it went to
approximately zero on 2022-05-13, then stops for 437 hours, then resumes at 8.87 with the
*post-fork* token under the same symbol -- a splice that reads as a single bar gaining
17,739,900%. Feeding that to a momentum rule would hand it the largest fabricated win in the
dataset, in a milestone whose entire purpose is to stop the data flattering the strategy.

Five is a data-integrity threshold, not a return threshold: no large-cap asset moves fivefold
across a trading halt on a venue that runs continuously, so a jump that size across one is a
redenomination, a fork or a relisting rather than a price. It is applied identically to every
market, and both prices are recorded so the call can be checked by hand."""
DELISTED_SLACK: Final[timedelta] = timedelta(days=30)
"""A series whose last bar precedes the common end by more than this is treated as delisted or
suspended, and said so in the report."""


def _fetch(url: str, *, retries: int = 3) -> bytes | None:
    """Return the body of ``url``, ``None`` on a 404, raising on anything else."""
    # Only ever https URLs built from the constants above; never a caller-supplied scheme.
    request = urllib.request.Request(url, headers={"User-Agent": "quant-platform-m32/1.0"})  # noqa: S310
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == HTTP_NOT_FOUND:
                return None
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    return None


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def _months(start: date, until: date) -> list[str]:
    months, cursor = [], date(start.year, start.month, 1)
    while cursor < until:
        months.append(f"{cursor.year}-{cursor.month:02d}")
        cursor = _next_month(cursor)
    return months


def _archive(raw: str, kind: str, stamp: str) -> dict[str, Any]:
    """Fetch one archive and its checksum, reporting what came back."""
    name = f"{raw}-1h-{stamp}.zip"
    url = f"{VISION}/{kind}/klines/{raw}/1h/{name}"
    body = _fetch(url)
    if body is None:
        return {"kind": kind, "stamp": stamp, "status": "absent"}
    folder = HOME / "binance_vision_archives" / raw / kind
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(body)
    actual = hashlib.sha256(body).hexdigest()
    checksum = _fetch(url + ".CHECKSUM")
    expected = checksum.decode().split()[0] if checksum else None
    return {
        "kind": kind,
        "stamp": stamp,
        "status": "ok" if expected == actual else "checksum_mismatch",
        "sha256": actual,
        "expected": expected,
    }


def _download(raw: str) -> list[dict[str, Any]]:
    """Fetch every monthly archive that exists, then daily ones for the gaps and the tail."""
    today = datetime.now(UTC).date()
    current = date(today.year, today.month, 1)
    stamps = _months(EARLIEST, current)
    with ThreadPoolExecutor(max_workers=8) as pool:
        monthly = list(pool.map(lambda m: _archive(raw, "monthly", m), stamps))
    published = [entry for entry in monthly if entry["status"] != "absent"]
    if not published:
        return monthly
    # Only months inside the listed span need daily cover; months before the first archive are
    # absent because the market did not exist, which is a fact and not a gap.
    first = min(entry["stamp"] for entry in published)
    last = max(entry["stamp"] for entry in published)
    days: list[date] = []
    for entry in monthly:
        if entry["status"] == "absent" and first < entry["stamp"] <= last:
            year, month = (int(part) for part in entry["stamp"].split("-"))
            start = date(year, month, 1)
            days += [start + timedelta(days=i) for i in range((_next_month(start) - start).days)]
    days += [current + timedelta(days=i) for i in range((today - current).days + 1)]
    days = [day for day in days if day < DATA_END.date()]
    if not days:
        return monthly
    with ThreadPoolExecutor(max_workers=8) as pool:
        daily = list(pool.map(lambda d: _archive(raw, "daily", d.isoformat()), days))
    return monthly + daily


def _parse(raw: str, archives: list[dict[str, Any]]) -> tuple[list[Row], list[str]]:
    """Read every downloaded archive's rows, refusing anything off the hourly grid."""
    rows: list[Row] = []
    problems: list[str] = []
    present = [entry for entry in archives if entry["status"] != "absent"]
    ordered = sorted(present, key=lambda a: (a["stamp"][:7], a["kind"] == "daily", a["stamp"]))
    for entry in ordered:
        name = f"{raw}-1h-{entry['stamp']}.zip"
        path = HOME / "binance_vision_archives" / raw / entry["kind"] / name
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                text = archive.read(member).decode("utf-8")
                for line in csv.reader(io.StringIO(text)):
                    try:
                        row = parse_kline(line, source=entry["kind"])
                    except (ValueError, ArithmeticError) as exc:
                        problems.append(f"{entry['stamp']}: unreadable row ({type(exc).__name__})")
                        continue
                    if row is None:
                        continue
                    if row.open_time.minute or row.open_time.second:
                        problems.append(f"off-grid bar refused at {row.open_time.isoformat()}")
                        continue
                    rows.append(row)
    return rows, problems


def _splice(kept: list[Row], runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first long gap that the price jumps across, or ``None`` if there is none.

    A gap the price walks across is an outage. A gap it leaps across is a different asset
    wearing the same ticker, and everything after it belongs to that asset rather than this one.
    """
    for run in sorted(runs, key=lambda entry: str(entry["from"])):
        if int(run["hours"]) < LONG_GAP_HOURS:
            continue
        opened = datetime.fromisoformat(str(run["from"]))
        closed = datetime.fromisoformat(str(run["to"]))
        before = [row for row in kept if row.open_time < opened]
        after = [row for row in kept if row.open_time > closed]
        if not before or not after:
            continue
        last, first = before[-1].close, after[0].close
        if last <= 0 or first <= 0:
            return {
                "at": run["from"],
                "hours": run["hours"],
                "before": str(last),
                "after": str(first),
                "ratio": "undefined",
            }
        ratio = max(last, first) / min(last, first)
        if ratio >= SPLICE_FACTOR:
            return {
                "at": run["from"],
                "hours": run["hours"],
                "before": str(last),
                "after": str(first),
                "ratio": f"{ratio:.1f}",
            }
    return None


def _clean(entry: dict[str, Any], rows: list[Row]) -> list[Row]:
    """Deduplicate, cut to the common end, and record every integrity finding."""
    kept, duplicates, conflicts = deduplicate(rows)
    kept = [row for row in kept if row.open_time < DATA_END]
    entry["duplicates_dropped"] = len(duplicates)
    entry["conflicting_duplicates"] = len(conflicts)
    if conflicts:
        entry["anomalies"].append({"kind": "conflicting_duplicates", "detail": conflicts[:5]})
    violations = ohlcv_violations(kept)
    if violations:
        entry["anomalies"].append({"kind": "ohlcv_violation", "detail": violations[:5]})
    return kept


def _cover(entry: dict[str, Any], kept: list[Row]) -> tuple[list[Row], frozenset[datetime]]:
    """Record coverage, cut the series at a ticker change, and return what survives."""
    absent = missing_hours(
        kept, start=kept[0].open_time, end=kept[-1].open_time + timedelta(hours=1)
    )
    runs = gap_runs(absent)

    # Cut where a ticker changed hands, before anything downstream can compound across the
    # seam. Everything after the seam belongs to a different asset, not to this one.
    splice = _splice(kept, runs)
    entry["ticker_reuse"] = splice
    if splice is not None:
        entry["anomalies"].append({"kind": "ticker_reuse_truncated", "detail": splice})
        cut = datetime.fromisoformat(str(splice["at"]))
        entry["bars_discarded_after_splice"] = sum(1 for row in kept if row.open_time >= cut)
        kept = [row for row in kept if row.open_time < cut]
        absent = missing_hours(
            kept, start=kept[0].open_time, end=kept[-1].open_time + timedelta(hours=1)
        )
        runs = gap_runs(absent)

    long_runs = [run for run in runs if int(run.get("hours", 0)) >= LONG_GAP_HOURS]
    end = kept[-1].open_time + timedelta(hours=1)
    entry["coverage"] = {
        "first_open_time": kept[0].open_time.isoformat(),
        "last_open_time": kept[-1].open_time.isoformat(),
        "hourly_bars": len(kept),
        "missing_hours": len(absent),
        "gap_runs": len(runs),
        "long_gap_runs": len(long_runs),
        "rest_confirmed": False,
    }
    if long_runs:
        entry["anomalies"].append({"kind": "long_gap", "detail": long_runs[:5]})
    entry["delisted_or_suspended"] = bool(DATA_END - end > DELISTED_SLACK)
    if entry["delisted_or_suspended"]:
        entry["anomalies"].append(
            {"kind": "series_ends_early", "detail": kept[-1].open_time.isoformat()}
        )
    return kept, frozenset(absent)


def _write(raw: str, kept: list[Row], allowed: frozenset[datetime]) -> dict[str, Any]:
    """Write the hourly series and the two derived ones, returning their provenance."""
    symbol = f"{raw.removesuffix('USDT')}/USDT"
    hourly = to_market_bars(kept, symbol=symbol)
    OUT.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Any] = {}
    for timeframe in (Timeframe.H1, Timeframe.H4, Timeframe.D1):
        series = (
            hourly
            if timeframe is Timeframe.H1
            else resample_bars(hourly, timeframe, allowed_missing=allowed)
        )
        if not series:
            continue
        span = f"{series[0].open_time.date()}_{series[-1].close_time.date()}"
        path = OUT / f"{raw}_{timeframe.value}_{span}.csv"
        outputs[timeframe.value] = {
            "path": str(path.relative_to(ROOT)),
            "bars": len(series),
            "csv_sha256": write_csv(path, series),
            "bars_digest": bars_digest(series),
        }
    return outputs


def _build(raw: str) -> dict[str, Any]:
    """Download, validate and write one market's hourly, 4h and 1d series."""
    entry: dict[str, Any] = {"symbol": raw, "anomalies": []}
    archives = _download(raw)
    present = [a for a in archives if a["status"] != "absent"]
    entry["archives"] = {
        "monthly": sum(1 for a in present if a["kind"] == "monthly"),
        "daily": sum(1 for a in present if a["kind"] == "daily"),
        "checksums_verified": sum(1 for a in present if a["status"] == "ok"),
        "checksum_mismatches": sum(1 for a in present if a["status"] == "checksum_mismatch"),
    }
    for bad in [a for a in present if a["status"] != "ok"]:
        entry["anomalies"].append({"kind": "checksum_mismatch", "detail": bad})
    if not present:
        entry["status"] = "no_archives"
        entry["anomalies"].append({"kind": "no_archives", "detail": "nothing published for 1h"})
        return entry

    rows, problems = _parse(raw, archives)
    entry["anomalies"].extend({"kind": "parse", "detail": p} for p in problems[:20])
    if not rows:
        entry["status"] = "no_rows"
        return entry

    kept = _clean(entry, rows)
    if not kept:
        entry["status"] = "no_rows_in_window"
        return entry
    kept, allowed = _cover(entry, kept)
    entry["outputs"] = _write(raw, kept, allowed)
    if not entry["outputs"]:
        entry["anomalies"].append({"kind": "empty_output", "detail": "no series could be written"})
    entry["status"] = "ok"
    return entry


def main() -> int:
    """Build every market M32 needs and write one report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    markets = [raw for raw in to_download() if not wanted or raw in wanted]

    HOME.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "milestone": "m32",
        "standard": (
            "M16's, except that missing hours are not REST-confirmed "
            "-- see the module docstring for why"
        ),
        "data_end_exclusive": DATA_END.isoformat(),
        "markets": {},
    }
    started = time.time()
    for index, raw in enumerate(markets, start=1):
        at = time.time()
        entry = _build(raw)
        report["markets"][raw] = entry
        coverage = entry.get("coverage", {})
        first = str(coverage.get("first_open_time", "-"))[:10]
        last = str(coverage.get("last_open_time", "-"))[:10]
        sys.stdout.write(
            f"  [{index:2d}/{len(markets)}] {raw:11} {entry['status']:12} "
            f"{coverage.get('hourly_bars', 0):6d} 1h bars  {first} -> {last}  "
            f"gaps {coverage.get('missing_hours', 0):4d}  "
            f"{'DELISTED ' if entry.get('delisted_or_suspended') else ''}"
            f"{len(entry['anomalies'])} anomalies  ({time.time() - at:.0f}s)\n"
        )
        sys.stdout.flush()
        REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    report["seconds"] = round(time.time() - started, 1)
    REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    ok = sum(1 for e in report["markets"].values() if e["status"] == "ok")
    sys.stdout.write(
        f"\n{ok}/{len(markets)} markets built in {report['seconds']}s -> "
        f"{REPORT.relative_to(ROOT)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
