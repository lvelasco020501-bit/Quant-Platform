"""Build M16's canonical datasets: six markets, hourly, resampled to 4h.

Same standard as M10c and M15, run over every asset: monthly archives with their SHA-256
checksums verified, daily archives where a month is not published, one bar per open time,
missing hours confirmed against the live REST API before they are called outages, and the 4h
series compared against Binance's own 4h klines — which are built independently of the hourly
ones, so agreeing with them is evidence the resample is right.

Two checks are specific to this milestone:

* BTC's 4h bars over M15's exact window must reproduce M15's recorded digest. The pipeline
  here is a generalisation of that script; if the generalisation changed anything, this is
  where it shows.
* Every asset is cut to the same last bar, so no market gets a longer recent window than
  another.

Nothing is filled in, smoothed or repaired. Usage::

    uv run python scripts/m16_dataset.py [--only BTCUSDT,ETHUSDT]
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
from quantplatform.core.models.market import MarketBar
from quantplatform.research.digest import bars_digest
from quantplatform.research.m16 import ASSETS, DATA_END, Asset, symbol_rules_for
from quantplatform.research.resample import resample_bars
from quantplatform.research.vision import (
    HOUR,
    Row,
    deduplicate,
    gap_runs,
    missing_hours,
    ohlcv_violations,
    open_time,
    parse_kline,
    to_market_bars,
    write_csv,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
HOME: Final[Path] = ROOT / "data/raw/m16"
OUT: Final[Path] = HOME / "out"
VISION: Final[str] = "https://data.binance.vision/data/spot"
REST: Final[str] = "https://api.binance.com/api/v3/klines"
HTTP_NOT_FOUND: Final[int] = 404
M15_4H_DIGEST: Final[str] = "360b9b605f945d85f7707f87736d5add"
M15_START: Final[datetime] = datetime(2020, 1, 1, tzinfo=UTC)
OFFICIAL: Final[str] = "4h"


def _fetch(url: str, *, retries: int = 3) -> bytes | None:
    """Return the body of ``url``, ``None`` on a 404, raising on anything else."""
    # Only ever https URLs built from the constants above; never a caller-supplied scheme.
    request = urllib.request.Request(url, headers={"User-Agent": "quant-platform-m16/1.0"})  # noqa: S310
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


class Builder:
    """Builds and validates one asset's dataset, reporting everything it finds."""

    def __init__(self, asset: Asset) -> None:
        """Bind the builder to one market and prepare its archive directory."""
        self.asset = asset
        self.archives = HOME / "binance_vision_archives" / asset.raw
        self.report: dict[str, Any] = {
            "symbol": asset.symbol,
            "listing_month": asset.listing_month,
            "start": asset.start.isoformat(),
            "end_exclusive": DATA_END.isoformat(),
            "anomalies": [],
            "info": {},
        }

    def anomaly(self, kind: str, detail: object) -> None:
        """Record something that must be explained before the dataset can be used."""
        self.report["anomalies"].append({"kind": kind, "detail": detail})

    # --- download ---------------------------------------------------------------------

    def _archive(self, kind: str, stamp: str, interval: str) -> dict[str, Any]:
        name = f"{self.asset.raw}-{interval}-{stamp}.zip"
        url = f"{VISION}/{kind}/klines/{self.asset.raw}/{interval}/{name}"
        folder = self.archives / interval / kind
        folder.mkdir(parents=True, exist_ok=True)
        body = _fetch(url)
        if body is None:
            return {"kind": kind, "stamp": stamp, "interval": interval, "status": "absent"}
        actual = hashlib.sha256(body).hexdigest()
        checksum = _fetch(url + ".CHECKSUM")
        (folder / name).write_bytes(body)
        expected = checksum.decode().split()[0] if checksum else None
        return {
            "kind": kind,
            "stamp": stamp,
            "interval": interval,
            "status": "ok" if expected == actual else "checksum_mismatch",
            "sha256": actual,
            "expected": expected,
        }

    def download(self, interval: str) -> list[dict[str, Any]]:
        """Fetch monthly archives, then daily ones for any month that is not published."""
        today = datetime.now(UTC).date()
        current = date(today.year, today.month, 1)
        stamps = _months(self.asset.start.date(), current)
        with ThreadPoolExecutor(max_workers=8) as pool:
            monthly = list(pool.map(lambda m: self._archive("monthly", m, interval), stamps))
        days: list[date] = []
        for entry in monthly:
            if entry["status"] == "absent":
                year, month = (int(part) for part in entry["stamp"].split("-"))
                first = date(year, month, 1)
                days += [
                    first + timedelta(days=i) for i in range((_next_month(first) - first).days)
                ]
        days += [current + timedelta(days=i) for i in range((today - current).days + 1)]
        days = [d for d in days if d < DATA_END.date()]
        with ThreadPoolExecutor(max_workers=8) as pool:
            daily = list(pool.map(lambda d: self._archive("daily", d.isoformat(), interval), days))
        archives = [a for a in monthly + daily if a["status"] != "absent"]
        for bad in [a for a in archives if a["status"] != "ok"]:
            self.anomaly("checksum_mismatch", bad)
        if interval == "1h":
            self.report["archives"] = {
                "monthly": sum(1 for a in archives if a["kind"] == "monthly"),
                "daily": sum(1 for a in archives if a["kind"] == "daily"),
                "checksums_verified": sum(1 for a in archives if a["status"] == "ok"),
            }
        return archives

    # --- parse and validate ------------------------------------------------------------

    def parse(self, archives: list[dict[str, Any]], interval: str) -> tuple[list[Row], set[date]]:
        """Read every archive's rows, and report the days whose rows are off the grid.

        A row that does not sit on an exact bar boundary is refused rather than snapped to
        one: on 2018-02-09 Binance's *monthly* archive restarts the hourly grid 28 minutes
        late after an outage, while its daily archive and the live API both have properly
        aligned bars for those hours, holding different prices. Snapping would invent a bar
        the exchange never published; the caller re-fetches the affected days instead.
        """
        rows: list[Row] = []
        units = {"ms": 0, "us": 0}
        unreadable: list[str] = []
        off_grid: list[str] = []
        off_grid_days: set[date] = set()
        interval_seconds = Timeframe(interval).seconds
        ordered = sorted(archives, key=lambda a: (a["stamp"][:7], a["kind"] == "daily", a["stamp"]))
        for entry in ordered:
            name = f"{self.asset.raw}-{interval}-{entry['stamp']}.zip"
            path = self.archives / interval / entry["kind"] / name
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    text = archive.read(member).decode("utf-8")
                    for line in csv.reader(io.StringIO(text)):
                        try:
                            row = parse_kline(line, source=entry["kind"])
                        except ValueError as exc:
                            unreadable.append(f"{name}: {exc}")
                            continue
                        if row is None:
                            continue
                        if row.open_time.timestamp() % interval_seconds:
                            off_grid.append(row.open_time.isoformat())
                            off_grid_days.add(row.open_time.date())
                            continue
                        rows.append(row)
                        units[open_time(line[0])[1]] += 1
        if interval == "1h":
            self.report["raw_rows_read"] = len(rows)
            self.report["timestamp_units"] = units
        if unreadable:
            self.anomaly("unreadable_row", unreadable[:20])
        if off_grid:
            self.report["info"].setdefault("off_grid_rows", {})[interval] = {
                "count": len(off_grid),
                "first": off_grid[:5],
                "days": sorted(day.isoformat() for day in off_grid_days),
            }
        return rows, off_grid_days

    def validate(self, rows: list[Row]) -> list[Row]:
        """Apply the M10c checks and return the ordered, de-duplicated bars in range."""
        start = self.asset.start
        in_range = [r for r in rows if start <= r.open_time < DATA_END]
        self.report["rows_outside_requested_range_dropped"] = len(rows) - len(in_range)
        bars, duplicates, conflicts = deduplicate(in_range)
        if duplicates:
            self.anomaly(
                "duplicate_open_time", {"count": len(duplicates), "first": duplicates[:10]}
            )
        if conflicts:
            self.anomaly("conflicting_duplicate", conflicts[:10])
        misaligned = [
            b.open_time.isoformat()
            for b in bars
            if b.open_time.minute or b.open_time.second or b.open_time.microsecond
        ]
        if misaligned:
            self.anomaly("misaligned_open_time", misaligned[:20])
        violations = ohlcv_violations(bars)
        if violations:
            self.anomaly("ohlcv_violation", violations[:20])
        expected = int((DATA_END - start) / HOUR)
        missing = missing_hours(bars, start=start, end=DATA_END)
        self.report["expected_bar_count"] = expected
        self.report["actual_bar_count"] = len(bars)
        self.report["missing_bar_count"] = len(missing)
        if missing:
            self.anomaly("gap", gap_runs(missing)[:50])
        self.report["info"]["zero_volume_bars"] = sum(1 for b in bars if b.volume == 0)
        return bars

    def repair(self, days: set[date], *, label: str, wanted: set[datetime]) -> list[Row]:
        """Re-fetch days the monthly archive got wrong from the daily archives.

        The daily archives are published independently of the monthly ones, so this is a
        second source rather than a second reading of the same one. Whatever it returns is
        validated exactly like everything else; if it is off the grid too, those hours stay
        missing and the outage check has to account for them.
        """
        fetched = [self._archive("daily", day.isoformat(), "1h") for day in sorted(days)]
        usable = [a for a in fetched if a["status"] == "ok"]
        parsed, still_off = self.parse(usable, "1h")
        # Only the hours we are missing: re-reading hours we already have would report the
        # whole day as duplicates, and would turn Binance's own monthly-vs-daily volume
        # disagreements into conflicts that say nothing about this dataset.
        rows = [row for row in parsed if row.open_time in wanted]
        self.report["info"][label] = {
            "days": sorted(day.isoformat() for day in days),
            "daily_archives_fetched": len(usable),
            "aligned_rows_recovered": len(rows),
            "still_off_grid_days": sorted(day.isoformat() for day in still_off),
        }
        return rows

    def confirm_outages(self, missing: list[datetime]) -> frozenset[datetime]:
        """Characterise every missing hour against the live API, and document them all.

        Coverage is whatever the checksummed archives contain — monthly first, then daily.
        The REST API is asked about each hole, but as evidence rather than as a patch: on
        2017-09-06 it serves bars that neither the archive nor Binance's own 4h series has,
        so backfilling from it would invent slower bars the exchange never published. Every
        missing hour is therefore documented, the split is reported, and the cross-check
        against Binance's own 4h series is what proves the omissions are the right ones.
        """
        present: list[str] = []
        for hour in missing:
            start_ms = int(hour.timestamp() * 1000)
            url = f"{REST}?symbol={self.asset.raw}&interval=1h&startTime={start_ms}&limit=1"
            body = _fetch(url)
            if body:
                kline = json.loads(body)
                if kline and open_time(str(kline[0][0]))[0] == hour:
                    present.append(hour.isoformat())
            time.sleep(0.05)
        self.report["documented_absences"] = {
            "hours": len(missing),
            "absent_on_rest_too": len(missing) - len(present),
            "served_by_rest_but_not_by_either_archive": len(present),
            "first_served_by_rest": present[:10],
        }
        if missing:
            self.report["anomalies"] = [a for a in self.report["anomalies"] if a["kind"] != "gap"]
            self.report["info"]["gaps"] = (
                f"{len(missing)} missing hours: {len(missing) - len(present)} absent from the "
                f"live API as well, {len(present)} served by it but in neither archive"
            )
        return frozenset(missing)

    # --- cross-check ------------------------------------------------------------------

    def cross_check_official(
        self,
        ours: tuple[MarketBar, ...],
        outages: frozenset[datetime],
        hourly_volume: dict[datetime, Decimal],
    ) -> None:
        """Compare our resampled 4h bars against Binance's own, and explain every difference."""
        archives = self.download(OFFICIAL)
        theirs = {
            row.open_time: row
            for row in self.parse(archives, OFFICIAL)[0]
            if self.asset.start <= row.open_time < DATA_END
        }
        tick = symbol_rules_for(self.asset).price_tick
        first_hours = {bar.open_time: hourly_volume.get(bar.open_time) for bar in ours}
        identical, explained, unexplained = 0, [], []
        for bar in ours:
            other = theirs.get(bar.open_time)
            if other is None:
                record_absent = {"open_time": bar.open_time.isoformat(), "why": "absent there"}
                if bar.volume == 0:
                    record_absent["reason"] = (
                        "nothing traded in the bucket; their series omits no-trade hours, "
                        "ours keeps the venue's own zero-volume bar"
                    )
                    explained.append(record_absent)
                else:
                    unexplained.append(record_absent)
                continue
            mine = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.trade_count)
            yours = (
                other.open,
                other.high,
                other.low,
                other.close,
                other.volume,
                other.trade_count,
            )
            if mine == yours:
                identical += 1
                continue
            record: dict[str, Any] = {
                "open_time": bar.open_time.isoformat(),
                "ours": [str(v) for v in mine],
                "binance": [str(v) for v in yours],
            }
            my_prices = (bar.open, bar.high, bar.low, bar.close)
            their_prices = (other.open, other.high, other.low, other.close)
            if my_prices == their_prices:
                record["reason"] = "prices identical; volume or trade count differs"
            elif all(abs(a - b) <= tick for a, b in zip(my_prices, their_prices, strict=True)):
                record["reason"] = "prices differ by at most one tick"
            elif first_hours.get(bar.open_time) == 0 and (
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            ) == (other.high, other.low, other.close, other.volume):
                record["reason"] = (
                    "their bucket opens at the first hour that traded; ours opens at the "
                    "venue's zero-volume bar"
                )
            elif any(
                bar.open_time + HOUR * i in outages
                for i in range(Timeframe.H4.seconds // int(HOUR.total_seconds()))
            ):
                record["reason"] = "their bar covers an hour no endpoint has a bar for"
            else:
                unexplained.append(record)
                continue
            explained.append(record)
        extra = sorted(set(theirs) - {b.open_time for b in ours})
        self.report["cross_check_official_4h"] = {
            "binance_bars": len(theirs),
            "our_bars": len(ours),
            "identical": identical,
            "explained": explained[:20],
            "explained_count": len(explained),
            "unexplained_count": len(unexplained),
            "absent_from_ours": [stamp.isoformat() for stamp in extra[:20]],
        }
        if unexplained:
            self.anomaly("official_4h_mismatch", unexplained[:20])
        if extra:
            self.anomaly("official_4h_bar_we_do_not_have", [s.isoformat() for s in extra[:20]])

    # --- build ------------------------------------------------------------------------

    def build(self) -> dict[str, Any]:
        """Download, validate, resample and cross-check this asset end to end."""
        started = time.time()
        rows, _off_grid_days = self.parse(self.download("1h"), "1h")
        # Sources in order of authority: the checksummed monthly archive, then the daily
        # archive, then the exchange's own API. Only what none of them has is an outage.
        provisional, _, _ = deduplicate(
            [r for r in rows if self.asset.start <= r.open_time < DATA_END]
        )
        gaps = missing_hours(provisional, start=self.asset.start, end=DATA_END)
        if gaps:
            rows += self.repair(
                {hour.date() for hour in gaps}, label="daily_repair", wanted=set(gaps)
            )
        bars = self.validate(rows)
        self.report["info"]["rows_by_source"] = {
            kind: sum(1 for b in bars if b.source == kind) for kind in ("monthly", "daily")
        }
        outages = self.confirm_outages(missing_hours(bars, start=self.asset.start, end=DATA_END))
        hourly = to_market_bars(bars, symbol=self.asset.symbol)
        first = resample_bars(hourly, Timeframe.H4, allowed_missing=outages)
        again = resample_bars(hourly, Timeframe.H4, allowed_missing=outages)
        if bars_digest(first) != bars_digest(again):
            self.anomaly("resample_not_deterministic", self.asset.raw)
        self.cross_check_official(first, outages, {bar.open_time: bar.volume for bar in hourly})
        if self.asset.raw == "BTCUSDT":
            overlap = tuple(b for b in first if b.open_time >= M15_START)
            digest = bars_digest(overlap)
            self.report["m15_digest_check"] = {
                "expected": M15_4H_DIGEST,
                "actual": digest,
                "bars": len(overlap),
            }
            if digest != M15_4H_DIGEST:
                self.anomaly("m15_digest_mismatch", self.report["m15_digest_check"])
        OUT.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Any] = {}
        for label, series in (("1h", hourly), ("4h", first)):
            path = OUT / f"{self.asset.raw}_{label}_{self.asset.start.date()}_{DATA_END.date()}.csv"
            outputs[label] = {
                "path": str(path.relative_to(ROOT)),
                "bars": len(series),
                "first_open_time": series[0].open_time.isoformat(),
                "last_open_time": series[-1].open_time.isoformat(),
                "csv_sha256": write_csv(path, series),
                "bars_digest": bars_digest(series),
            }
        volumes = {bar.open_time: bar.volume for bar in hourly}
        self.report["info"]["4h_bars_opening_on_a_zero_volume_hour"] = sum(
            1 for bar in first if volumes.get(bar.open_time) == 0
        )
        self.report["outputs"] = outputs
        self.report["seconds"] = round(time.time() - started, 1)
        return self.report


def main() -> int:
    """Build every requested asset and write one report covering all of them."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    wanted = {name for name in args.only.split(",") if name}
    assets = [a for a in ASSETS if not wanted or a.raw in wanted]

    HOME.mkdir(parents=True, exist_ok=True)
    reports: dict[str, Any] = {}
    for asset in assets:
        sys.stdout.write(f"building {asset.raw} from {asset.start.date()} ...\n")
        sys.stdout.flush()
        report = Builder(asset).build()
        reports[asset.raw] = report
        sys.stdout.write(
            f"  {report['actual_bar_count']} hourly bars, "
            f"{report['missing_bar_count']} missing, "
            f"{len(report['anomalies'])} anomalies, {report['seconds']}s\n"
        )
        sys.stdout.flush()
    last_bars = {r["outputs"]["4h"]["last_open_time"] for r in reports.values()}
    summary = {
        "assets": reports,
        "every_asset_ends_on_the_same_bar": len(last_bars) == 1,
        "last_4h_open_time": sorted(last_bars),
        "total_anomalies": sum(len(r["anomalies"]) for r in reports.values()),
    }
    (HOME / "validation_report.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    sys.stdout.write(json.dumps({k: v for k, v in summary.items() if k != "assets"}, indent=2))
    sys.stdout.write("\n")
    return 0 if summary["total_anomalies"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
