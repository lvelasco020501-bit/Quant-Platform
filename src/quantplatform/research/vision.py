"""Reading Binance Vision's hourly archives into the platform's own bars.

The checks are M10c's, proven over BTC in M15 and generalised here so several markets can be
built the same way: one bar per open time, duplicates reported rather than picked between,
missing hours grouped into the outages they usually are, and prices that contradict each
other refused. Nothing here touches the network — downloading, checksums and the live REST
cross-checks belong to the script that calls this, so every rule below can be tested directly.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, NamedTuple

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar

__all__ = [
    "HEADER",
    "HOUR",
    "Row",
    "deduplicate",
    "gap_runs",
    "missing_hours",
    "ohlcv_violations",
    "open_time",
    "parse_kline",
    "to_market_bars",
    "write_csv",
]

HOUR: Final[timedelta] = timedelta(hours=1)
MILLISECOND_DIGITS: Final[int] = 13
MICROSECOND_DIGITS: Final[int] = 16
_TRADE_COUNT_COLUMN: Final[int] = 8
HEADER: Final[tuple[str, ...]] = (
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
)
"""M10c's schema, unchanged: the canonical CSV every study in this repository reads."""


class Row(NamedTuple):
    """One raw hourly kline, already typed but not yet the platform's model."""

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int
    source: str


def open_time(raw: str) -> tuple[datetime, str]:
    """Return an open time and the unit it arrived in: 13 digits is ms, 16 is µs.

    Binance changed the unit inside the archive — 2024 and earlier are milliseconds, 2025
    onward microseconds — so the unit is read per row. A width that is neither is refused:
    guessing would silently move a bar by three orders of magnitude.
    """
    digits = len(raw)
    if digits == MILLISECOND_DIGITS:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC), "ms"
    if digits == MICROSECOND_DIGITS:
        return datetime.fromtimestamp(int(raw) / 1_000_000, tz=UTC), "us"
    msg = f"unrecognised timestamp width {digits}: {raw}"
    raise ValueError(msg)


def parse_kline(line: Sequence[str], *, source: str) -> Row | None:
    """Return one archive line as a :class:`Row`, or ``None`` if it is not a bar at all.

    Raises:
        ValueError: If the line looks like a bar but cannot be read as one. A malformed row
            is reported by the caller, never dropped quietly.
    """
    if not line or not line[0].isdigit():
        return None
    try:
        opened, _ = open_time(line[0])
        return Row(
            open_time=opened,
            open=Decimal(line[1]),
            high=Decimal(line[2]),
            low=Decimal(line[3]),
            close=Decimal(line[4]),
            volume=Decimal(line[5]),
            trade_count=int(line[_TRADE_COUNT_COLUMN]),
            source=source,
        )
    except (InvalidOperation, IndexError) as exc:
        msg = f"unreadable kline row {list(line[:6])}: {exc}"
        raise ValueError(msg) from exc


_COMPARED: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume", "trade_count")


def deduplicate(rows: Iterable[Row]) -> tuple[list[Row], list[str], list[dict[str, Any]]]:
    """Return one bar per open time, in time order, with duplicates and conflicts reported.

    Where two archives disagree about the same hour, both are reported and the first is kept.
    Choosing between them here would be an undocumented edit to the data; the caller decides
    what a conflict means, and in M15 and M16 it means the run stops.
    """
    by_time: dict[datetime, list[Row]] = {}
    for row in rows:
        by_time.setdefault(row.open_time, []).append(row)
    duplicates: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for stamp, group in by_time.items():
        if len(group) == 1:
            continue
        duplicates.append(stamp.isoformat())
        distinct = {tuple(getattr(row, field) for field in _COMPARED) for row in group}
        if len(distinct) > 1:
            conflicts.append(
                {
                    "open_time": stamp.isoformat(),
                    "rows": [str([getattr(row, f) for f in _COMPARED]) for row in group],
                }
            )
    return [by_time[stamp][0] for stamp in sorted(by_time)], duplicates, conflicts


def missing_hours(rows: Iterable[Row], *, start: datetime, end: datetime) -> list[datetime]:
    """Return every hourly slot in ``[start, end)`` that has no bar."""
    present = {row.open_time for row in rows}
    slots: list[datetime] = []
    cursor = start
    while cursor < end:
        if cursor not in present:
            slots.append(cursor)
        cursor += HOUR
    return slots


def gap_runs(missing: Sequence[datetime]) -> list[dict[str, Any]]:
    """Group consecutive missing hours, so one outage reads as one event rather than many."""
    if not missing:
        return []
    ordered = sorted(missing)
    runs: list[dict[str, Any]] = []
    begin = previous = ordered[0]
    for slot in [*ordered[1:], None]:
        if slot is None or slot - previous != HOUR:
            runs.append(
                {
                    "from": begin.isoformat(),
                    "to": previous.isoformat(),
                    "hours": int((previous - begin) / HOUR) + 1,
                }
            )
            if slot is not None:
                begin = slot
        if slot is not None:
            previous = slot
    return runs


def ohlcv_violations(rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Return every bar whose prices contradict each other, or that is non-positive."""
    bad: list[dict[str, Any]] = []
    for row in rows:
        reasons: list[str] = []
        if not (row.high >= row.open and row.high >= row.close and row.high >= row.low):
            reasons.append("high_not_max")
        if not (row.low <= row.open and row.low <= row.close and row.low <= row.high):
            reasons.append("low_not_min")
        if row.volume < 0:
            reasons.append("negative_volume")
        if min(row.open, row.high, row.low, row.close) <= 0:
            reasons.append("non_positive_price")
        if reasons:
            bad.append({"open_time": row.open_time.isoformat(), "reasons": reasons})
    return bad


def to_market_bars(rows: Iterable[Row], *, symbol: str) -> tuple[MarketBar, ...]:
    """Return validated rows as the platform's model, close time derived rather than copied."""
    return tuple(
        MarketBar(
            symbol=symbol,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            open_time=row.open_time,
            close_time=row.open_time + HOUR,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            trade_count=row.trade_count,
            source="csv_historical",
            is_closed=True,
        )
        for row in rows
    )


def write_csv(path: Path, bars: Sequence[MarketBar]) -> str:
    """Write M10c's schema and return the file's SHA-256."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for bar in bars:
            writer.writerow(
                [
                    bar.symbol,
                    bar.market_type.value,
                    bar.timeframe.value,
                    bar.open_time.isoformat(),
                    bar.close_time.isoformat(),
                    *(
                        str(value.normalize())
                        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
                    ),
                    bar.trade_count,
                ]
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()
