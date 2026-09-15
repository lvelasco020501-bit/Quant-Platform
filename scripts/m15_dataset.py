"""Acquire, validate and canonicalise BTC/USDT spot 1h klines from 2020, to M10c's standard.

Research data only: writes under ``data/raw/m15/`` (git-ignored, like M10c's). Nothing is filled,
interpolated or invented. A gap, a conflicting duplicate or an invalid bar is *reported*, never
repaired; what is done about one is a separate, documented decision.

Unlike M10c's builder, this script is committed, so the dataset can be rebuilt byte for byte.

Steps:
  1. Download every monthly archive from 2020-01, falling back to daily archives for any month
     not yet published, and daily archives for the current month; verify each ``.CHECKSUM``.
  2. Parse both timestamp units Binance Vision has used: milliseconds (to 2024) and
     microseconds (from 2025). The unit is detected per row, never assumed per file.
  3. Validate exactly as M10c did — range, duplicates, conflicting duplicates, grid alignment,
     gaps, OHLCV — and add: unparsable rows, zero-price bars, per-unit row counts.
  4. Cross-check every overlapping bar against M10c's canonical CSV, field by field, and a
     handful of boundary bars against the live Binance REST API.
  5. Write the canonical CSV (M10c's exact schema), its SHA-256, the platform's own bars digest,
     and deterministic 4h and 1d resamples with their digests.

Usage::

    uv run python scripts/m15_dataset.py
"""

from __future__ import annotations

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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.research.digest import bars_digest
from quantplatform.research.resample import resample_bars

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "data/raw/m15"
ARCHIVES = HOME / "binance_vision_archives"
OUT = HOME / "out"
M10C_CSV = ROOT / "data/raw/m10c/BTCUSDT_1h_2025-09-01_2026-09-01.csv"
VISION = "https://data.binance.vision/data/spot"
REST = "https://api.binance.com/api/v3/klines"
HTTP_NOT_FOUND = 404
MILLISECOND_DIGITS = 13
MICROSECOND_DIGITS = 16
SYMBOL_RAW = "BTCUSDT"
SYMBOL = "BTC/USDT"
START = datetime(2020, 1, 1, tzinfo=UTC)
STEP = timedelta(hours=1)
HEADER = [
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
    "trade_count",
]
REST_SPOT_CHECKS = (
    datetime(2020, 1, 1, 0, tzinfo=UTC),  # first bar
    datetime(2020, 3, 12, 12, tzinfo=UTC),  # March 2020 crash
    datetime(2021, 5, 19, 13, tzinfo=UTC),  # May 2021 crash
    datetime(2022, 11, 8, 18, tzinfo=UTC),  # FTX week
    datetime(2024, 12, 31, 23, tzinfo=UTC),  # last millisecond-format bar
    datetime(2025, 1, 1, 0, tzinfo=UTC),  # first microsecond-format bar
)

report: dict[str, Any] = {"anomalies": [], "info": {}}


def _anomaly(kind: str, detail: object) -> None:
    report["anomalies"].append({"kind": kind, "detail": detail})


def _fetch(url: str, *, retries: int = 3) -> bytes | None:
    """Return the body of ``url``, ``None`` on a 404, raising on anything else."""
    # Only ever https URLs built from the constants above; never a caller-supplied scheme.
    request = urllib.request.Request(url, headers={"User-Agent": "quant-platform-m15/1.0"})  # noqa: S310
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


def _archive(kind: str, stamp: str) -> dict[str, Any]:
    """Download one archive and its checksum, verify it, and keep both on disk."""
    name = f"{SYMBOL_RAW}-1h-{stamp}.zip"
    url = f"{VISION}/{kind}/klines/{SYMBOL_RAW}/1h/{name}"
    folder = ARCHIVES / kind
    folder.mkdir(parents=True, exist_ok=True)
    body = _fetch(url)
    if body is None:
        return {"kind": kind, "stamp": stamp, "status": "absent"}
    checksum = _fetch(url + ".CHECKSUM")
    (folder / name).write_bytes(body)
    actual = hashlib.sha256(body).hexdigest()
    expected = checksum.decode().split()[0] if checksum else None
    if checksum is not None:
        (folder / (name + ".CHECKSUM")).write_bytes(checksum)
    return {
        "kind": kind,
        "stamp": stamp,
        "status": "ok" if expected == actual else "checksum_mismatch",
        "sha256": actual,
        "expected": expected,
        "bytes": len(body),
    }


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def _months(until: date) -> list[str]:
    months, cursor = [], date(START.year, START.month, 1)
    while cursor < until:
        months.append(f"{cursor.year}-{cursor.month:02d}")
        cursor = _next_month(cursor)
    return months


def download() -> list[dict[str, Any]]:
    """Fetch monthly archives, then daily ones for any unpublished month and the current one."""
    today = datetime.now(UTC).date()
    current_month = date(today.year, today.month, 1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        monthly = list(pool.map(lambda m: _archive("monthly", m), _months(current_month)))
    daily_days: list[date] = []
    for entry in monthly:
        if entry["status"] == "absent":
            year, month = map(int, entry["stamp"].split("-"))
            first = date(year, month, 1)
            span = (_next_month(first) - first).days
            daily_days += [first + timedelta(days=i) for i in range(span)]
    elapsed = (today - current_month).days + 1
    daily_days += [current_month + timedelta(days=i) for i in range(elapsed)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        daily = list(pool.map(lambda d: _archive("daily", d.isoformat()), daily_days))
    # Trailing absent days are "not yet published", not gaps; absent days before the last
    # published one would be holes and are left for the gap check to report.
    published = [d for d in daily if d["status"] != "absent"]
    last = max((d["stamp"] for d in published), default=None)
    trailing = [d for d in daily if d["status"] == "absent" and (last is None or d["stamp"] > last)]
    report["info"]["daily_not_yet_published"] = [d["stamp"] for d in trailing]
    archives = [a for a in monthly + daily if a["status"] != "absent"]
    for missing in [a for a in monthly if a["status"] == "absent"]:
        report["info"].setdefault("monthly_substituted_by_daily", []).append(missing["stamp"])
    for bad in [a for a in archives if a["status"] != "ok"]:
        _anomaly("checksum_mismatch", bad)
    report["archives"] = {
        "monthly": sum(1 for a in archives if a["kind"] == "monthly"),
        "daily": sum(1 for a in archives if a["kind"] == "daily"),
        "checksums_verified": sum(1 for a in archives if a["status"] == "ok"),
    }
    (HOME / "archives_manifest.json").write_text(json.dumps(archives, indent=2))
    return archives


def _open_time(raw: str) -> tuple[datetime, str]:
    """Return a raw open time and the unit it was in: 13 digits is ms, 16 is µs."""
    digits = len(raw)
    if digits == MILLISECOND_DIGITS:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC), "ms"
    if digits == MICROSECOND_DIGITS:
        return datetime.fromtimestamp(int(raw) / 1_000_000, tz=UTC), "us"
    msg = f"unrecognised timestamp width {digits}: {raw}"
    raise ValueError(msg)


def parse(archives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read every archive's rows, in chronological order of archive."""
    rows: list[dict[str, Any]] = []
    units = {"ms": 0, "us": 0}
    unparsable = []
    for entry in sorted(archives, key=lambda a: (a["stamp"][:7], a["kind"] == "daily", a["stamp"])):
        name = f"{SYMBOL_RAW}-1h-{entry['stamp']}.zip"
        with zipfile.ZipFile(ARCHIVES / entry["kind"] / name) as archive:
            for member in archive.namelist():
                text = archive.read(member).decode("utf-8")
                for line in csv.reader(io.StringIO(text)):
                    if not line or not line[0].isdigit():
                        continue
                    try:
                        opened, unit = _open_time(line[0])
                        rows.append(
                            {
                                "open_time": opened,
                                "open": Decimal(line[1]),
                                "high": Decimal(line[2]),
                                "low": Decimal(line[3]),
                                "close": Decimal(line[4]),
                                "volume": Decimal(line[5]),
                                "trade_count": int(line[8]),
                                "source": entry["kind"],
                            }
                        )
                        units[unit] += 1
                    except (ValueError, InvalidOperation, IndexError) as exc:
                        unparsable.append({"archive": name, "row": line[:6], "error": str(exc)})
    report["raw_rows_read"] = len(rows)
    report["timestamp_units"] = units
    if unparsable:
        _anomaly("unparsable_row", unparsable[:50])
    return rows


_FIELDS = ("open", "high", "low", "close", "volume", "trade_count")


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one bar per open time; report duplicates, and conflicts among them, never pick."""
    by_time: dict[datetime, list[dict[str, Any]]] = {}
    for row in rows:
        by_time.setdefault(row["open_time"], []).append(row)
    duplicates: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for ts, group in by_time.items():
        if len(group) > 1:
            duplicates.append(ts.isoformat())
            if len({tuple(g[f] for f in _FIELDS) for g in group}) > 1:
                values = [str([g[f] for f in _FIELDS]) for g in group]
                conflicts.append({"open_time": ts.isoformat(), "rows": values})
    if duplicates:
        _anomaly("duplicate_open_time", {"count": len(duplicates), "first": duplicates[:20]})
    if conflicts:
        _anomaly("conflicting_duplicate", conflicts)
    return [by_time[ts][0] for ts in sorted(by_time)]


def _check_gaps(bars: list[dict[str, Any]], end: datetime) -> None:
    """Report every missing hourly slot, grouped into runs so one outage reads as one event."""
    expected, cursor = [], START
    while cursor < end:
        expected.append(cursor)
        cursor += STEP
    present = {b["open_time"] for b in bars}
    missing = [slot for slot in expected if slot not in present]
    report["expected_bar_count"] = len(expected)
    report["actual_bar_count"] = len(bars)
    report["missing_bar_count"] = len(missing)
    if not missing:
        return
    runs: list[dict[str, Any]] = []
    begin = prev = missing[0]
    for slot in [*missing[1:], None]:
        if slot is None or slot - prev != STEP:
            hours = int((prev - begin) / STEP) + 1
            runs.append({"from": begin.isoformat(), "to": prev.isoformat(), "hours": hours})
            if slot is not None:
                begin = slot
        if slot is not None:
            prev = slot
    _anomaly("gap", runs)


def _check_ohlcv(bars: list[dict[str, Any]]) -> None:
    """Report any bar whose prices contradict each other, or that is non-positive."""
    bad: list[dict[str, Any]] = []
    for b in bars:
        o, h, lo, c, v = b["open"], b["high"], b["low"], b["close"], b["volume"]
        reasons = []
        if not (h >= o and h >= c and h >= lo):
            reasons.append("high_not_max")
        if not (lo <= o and lo <= c and lo <= h):
            reasons.append("low_not_min")
        if v < 0:
            reasons.append("negative_volume")
        if min(o, h, lo, c) <= 0:
            reasons.append("non_positive_price")
        if reasons:
            bad.append({"open_time": b["open_time"].isoformat(), "reasons": reasons})
    if bad:
        _anomaly("ohlcv_violation", bad[:50])


def validate(rows: list[dict[str, Any]], end: datetime) -> list[dict[str, Any]]:
    """Apply M10c's checks, plus zero prices; return the ordered, de-duplicated bars."""
    in_range = [r for r in rows if START <= r["open_time"] < end]
    report["rows_outside_requested_range_dropped"] = len(rows) - len(in_range)
    bars = _deduplicate(in_range)
    misaligned = [
        b["open_time"].isoformat()
        for b in bars
        if b["open_time"].minute or b["open_time"].second or b["open_time"].microsecond
    ]
    if misaligned:
        _anomaly("misaligned_open_time", misaligned[:50])
    _check_gaps(bars, end)
    _check_ohlcv(bars)
    report["info"]["zero_volume_bars"] = sum(1 for b in bars if b["volume"] == 0)
    report["info"]["rows_by_source"] = {
        kind: sum(1 for b in bars if b["source"] == kind) for kind in ("monthly", "daily")
    }
    return bars


def to_market_bars(bars: list[dict[str, Any]]) -> tuple[MarketBar, ...]:
    """Return validated bars as the platform's own model, close time derived, not copied."""
    return tuple(
        MarketBar(
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            open_time=b["open_time"],
            close_time=b["open_time"] + STEP,
            open=b["open"],
            high=b["high"],
            low=b["low"],
            close=b["close"],
            volume=b["volume"],
            trade_count=b["trade_count"],
            source="csv_historical",
            is_closed=True,
        )
        for b in bars
    )


def write_csv(path: Path, bars: tuple[MarketBar, ...]) -> str:
    """Write M10c's exact schema and return the file's SHA-256."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for b in bars:
            writer.writerow(
                [
                    SYMBOL,
                    "spot",
                    b.timeframe.value,
                    b.open_time.isoformat(),
                    b.close_time.isoformat(),
                    *(str(x.normalize()) for x in (b.open, b.high, b.low, b.close, b.volume)),
                    b.trade_count,
                ]
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cross_check_m10c(bars: list[dict[str, Any]]) -> None:
    """Every bar M10c holds must be identical here, field for field."""
    ours = {b["open_time"]: b for b in bars}
    compared = mismatched = absent = 0
    with M10C_CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ts = datetime.fromisoformat(row["open_time"])
            mine = ours.get(ts)
            if mine is None:
                absent += 1
                continue
            compared += 1
            theirs = (
                Decimal(row["open"]),
                Decimal(row["high"]),
                Decimal(row["low"]),
                Decimal(row["close"]),
                Decimal(row["volume"]),
                int(row["trade_count"]),
            )
            if theirs != (
                mine["open"],
                mine["high"],
                mine["low"],
                mine["close"],
                mine["volume"],
                mine["trade_count"],
            ):
                mismatched += 1
    report["cross_check_m10c"] = {
        "compared": compared,
        "mismatched": mismatched,
        "absent_here": absent,
    }
    if mismatched or absent:
        _anomaly("m10c_cross_check_failed", report["cross_check_m10c"])


def cross_check_rest(bars: list[dict[str, Any]], last: datetime) -> None:
    """Compare boundary bars with the live REST API, which is independent of Binance Vision."""
    ours = {b["open_time"]: b for b in bars}
    results: list[dict[str, Any]] = []
    for ts in (*REST_SPOT_CHECKS, last):
        body = _fetch(
            f"{REST}?symbol={SYMBOL_RAW}&interval=1h&startTime={int(ts.timestamp() * 1000)}&limit=1"
        )
        if not body:
            results.append({"open_time": ts.isoformat(), "match": None, "note": "no REST response"})
            continue
        k = json.loads(body)[0]
        live = (
            Decimal(k[1]),
            Decimal(k[2]),
            Decimal(k[3]),
            Decimal(k[4]),
            Decimal(k[5]),
            int(k[8]),
        )
        mine = ours.get(ts)
        archived = (
            None
            if mine is None
            else (
                mine["open"],
                mine["high"],
                mine["low"],
                mine["close"],
                mine["volume"],
                mine["trade_count"],
            )
        )
        results.append({"open_time": ts.isoformat(), "match": live == archived})
    report["cross_check_rest"] = results
    if any(r["match"] is False for r in results):
        _anomaly("rest_cross_check_mismatch", results)


OFFICIAL = HOME / "official_klines"


def parse_official(interval: str) -> dict[datetime, tuple[Decimal, ...]]:
    """Read Binance's own ``interval`` klines, keyed by open time."""
    found: dict[datetime, tuple[Decimal, ...]] = {}
    for kind in ("monthly", "daily"):
        for path in sorted((OFFICIAL / interval / kind).glob("*.zip")):
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    for line in csv.reader(io.StringIO(archive.read(member).decode("utf-8"))):
                        if not line or not line[0].isdigit():
                            continue
                        opened, _ = _open_time(line[0])
                        found.setdefault(
                            opened,
                            (
                                Decimal(line[1]),
                                Decimal(line[2]),
                                Decimal(line[3]),
                                Decimal(line[4]),
                                Decimal(line[5]),
                                Decimal(line[8]),
                            ),
                        )
    return found


PRICE_TICK = Decimal("0.01")


def _classify(
    mine: tuple[Decimal, ...], theirs: tuple[Decimal, ...], *, bucket_has_outage: bool
) -> str | None:
    """Explain a disagreement with Binance's own slower bar, or return ``None`` if nothing does.

    Our slower bars are exact sums of Binance's 1h series; Binance's own 4h and 1d series are
    built separately and occasionally disagree with it. Three explanations are accepted, each a
    mechanical rule; anything else stays an anomaly.
    """
    if mine[:4] == theirs[:4]:
        return "prices identical; volume or trade count differs between Binance's own series"
    if max(abs(a - b) for a, b in zip(mine[:4], theirs[:4], strict=True)) <= PRICE_TICK:
        return "prices differ by at most one tick between Binance's own series"
    if bucket_has_outage:
        return "Binance's slower bar includes activity that no endpoint gives an hourly bar"
    return None


def cross_check_official(
    tf: Timeframe, series: tuple[MarketBar, ...], outages: frozenset[datetime]
) -> dict[str, Any]:
    """Compare every resampled bar with Binance's own bar for that interval, field for field."""
    official = parse_official(tf.value)
    in_range = {ts: v for ts, v in official.items() if START <= ts < series[-1].close_time}
    ours = {b.open_time: b for b in series}
    deviations: list[dict[str, Any]] = []
    unexplained: list[dict[str, Any]] = []
    missing_here = [ts.isoformat() for ts in in_range if ts not in ours]
    for ts, theirs in sorted(in_range.items()):
        bar = ours.get(ts)
        if bar is None:
            continue
        mine = (bar.open, bar.high, bar.low, bar.close, bar.volume, Decimal(bar.trade_count or 0))
        if mine == theirs:
            continue
        has_outage = any(ts <= hour < bar.close_time for hour in outages)
        reason = _classify(mine, theirs, bucket_has_outage=has_outage)
        entry = {
            "open_time": ts.isoformat(),
            "ours": [str(x) for x in mine],
            "binance": [str(x) for x in theirs],
            "reason": reason,
        }
        (deviations if reason else unexplained).append(entry)
    extra = [b.open_time.isoformat() for b in series if b.open_time not in in_range]
    if unexplained or extra or missing_here:
        _anomaly(
            f"official_{tf.value}_cross_check_failed",
            {"unexplained": unexplained[:20], "extra": extra[:20], "missing_here": missing_here},
        )
    return {
        "binance_bars": len(in_range),
        "our_bars": len(series),
        "identical": len(in_range) - len(deviations) - len(unexplained) - len(missing_here),
        "explained_deviations": deviations,
        "unexplained": len(unexplained),
        "extra_here": len(extra),
        "missing_here": len(missing_here),
    }


def confirm_outages(missing: list[datetime]) -> dict[str, Any]:
    """Ask the live REST API, independent of the archive, whether each missing hour exists."""
    present: list[str] = []
    for hour in missing:
        start_ms = int(hour.timestamp() * 1000)
        body = _fetch(f"{REST}?symbol={SYMBOL_RAW}&interval=1h&startTime={start_ms}&limit=1")
        if body:
            kline = json.loads(body)
            if kline and _open_time(str(kline[0][0]))[0] == hour:
                present.append(hour.isoformat())
        time.sleep(0.05)
    return {
        "hours": len(missing),
        "absent_on_rest": len(missing) - len(present),
        "present_on_rest": present,
    }


def main() -> int:
    """Download, validate, cross-check, canonicalise and resample; report everything."""
    HOME.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    archives = download()
    rows = parse(archives)
    last_open = max(r["open_time"] for r in rows)
    end = datetime(last_open.year, last_open.month, last_open.day, tzinfo=UTC) + timedelta(days=1)
    if last_open + STEP < end:
        end = datetime(last_open.year, last_open.month, last_open.day, tzinfo=UTC)
    report["requested_range"] = {"start": START.isoformat(), "end_exclusive": end.isoformat()}
    bars = validate(rows, end)
    cross_check_m10c(bars)
    cross_check_rest(bars, bars[-1]["open_time"])

    hourly = to_market_bars(bars)
    stem = f"BTCUSDT_{{tf}}_{START.date()}_{end.date()}"
    outputs: dict[str, Any] = {}
    frames = {"1h": hourly}
    # The hours the gap check found are the only ones a slower bar may be built without, and
    # only because the official cross-check below proves Binance's own bar omits them too.
    present = {b.open_time for b in hourly}
    outages = frozenset(
        START + STEP * i
        for i in range(int((end - START) / STEP))
        if START + STEP * i not in present
    )
    report["documented_outage_hours"] = sorted(ts.isoformat() for ts in outages)
    confirmation = confirm_outages(sorted(outages))
    report["outage_confirmation"] = confirmation
    if not confirmation["present_on_rest"]:
        # Neither Binance endpoint has an hourly bar for any missing hour: exchange downtime,
        # not missing data. Kept on record as outages, removed from the anomaly list.
        report["anomalies"] = [a for a in report["anomalies"] if a["kind"] != "gap"]
        report["info"]["gaps"] = (
            f"{len(outages)} missing hours, all absent from the live REST API as well: outages"
        )
    official_checks: dict[str, Any] = {}
    for tf in (Timeframe.H4, Timeframe.D1):
        first = resample_bars(hourly, tf, allowed_missing=outages)
        again = resample_bars(hourly, tf, allowed_missing=outages)
        if bars_digest(first) != bars_digest(again):
            _anomaly("resample_not_deterministic", tf.value)
        frames[tf.value] = first
        official_checks[tf.value] = cross_check_official(tf, first, outages)
    report["cross_check_official"] = official_checks
    for label, series in frames.items():
        path = OUT / (stem.format(tf=label) + ".csv")
        outputs[label] = {
            "path": str(path.relative_to(ROOT)),
            "bars": len(series),
            "first_open_time": series[0].open_time.isoformat(),
            "last_open_time": series[-1].open_time.isoformat(),
            "csv_sha256": write_csv(path, series),
            "bars_digest": bars_digest(series),
        }
    report["outputs"] = outputs
    (HOME / "validation_report.json").write_text(json.dumps(report, indent=2, default=str))
    sys.stdout.write(
        json.dumps(
            {k: v for k, v in report.items() if k != "info"} | {"info": report["info"]},
            indent=2,
            default=str,
        )
        + "\n"
    )
    return 0 if not report["anomalies"] else 1


if __name__ == "__main__":
    sys.exit(main())
