"""A readable timeline, reconstructed from the session's own structured logs.

**Everything here is log-derived, and callers are expected to say so.** The platform's logs
are JSON Lines written by the platform itself, so reading them is parsing a known format
rather than scraping prose — but they are still not the same kind of fact as a persisted
snapshot. A log file can be rotated, truncated, or written by a session other than the one
being asked about, and none of those are true of `PaperSessionState`. So every figure this
module produces carries :attr:`TimelineEvent.log_derived`, and the UI labels it.

Why it exists at all: per-bar signal, intent, decision and fill counts are logged and
nowhere else. Refusing to read them would mean a dashboard that cannot answer "has the
strategy done anything yet", which is the first question anyone asks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__all__ = ["ActivityCounts", "TimelineEvent", "read_activity", "read_timeline"]

_MAX_LINES = 5_000
"""How far back to read in each log. A long-running session's logs outgrow a timeline that
anyone would scroll, and reading an unbounded file to render a fixed number of rows is how a
status page becomes slower than the thing it observes."""


@dataclass(frozen=True)
class TimelineEvent:
    """One thing that happened, in words rather than in fields."""

    at: datetime
    kind: str
    """A stable machine key — ``session_started``, ``bar_processed``, ``feed_connected`` —
    so the UI can choose an icon without matching on prose."""
    title: str
    detail: str | None = None
    severity: str = "info"
    log_derived: bool = True


@dataclass(frozen=True)
class ActivityCounts:
    """What the strategy and the risk engine have actually done.

    Every field is optional and ``None`` means *not known*, which for these is the state
    before any bar has been logged. Zero means a bar was processed and produced nothing —
    a different and much more informative answer.
    """

    signals: int | None = None
    intents: int | None = None
    decisions: int | None = None
    fills: int | None = None
    bars_seen: int = 0
    log_derived: bool = True


def _tail(path: Path, limit: int = _MAX_LINES) -> list[dict[str, object]]:
    """Return the last parseable JSON records of a log, newest last.

    A line that will not parse is skipped rather than fatal: a log being appended to while
    it is read can hand back a half-written final line, and refusing the whole timeline over
    it would make the dashboard fail exactly when the session is busiest.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _moment(record: dict[str, object]) -> datetime | None:
    """Return a record's timestamp, or nothing when it cannot be understood."""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _extra(record: dict[str, object]) -> dict[str, object]:
    """Return a record's structured payload."""
    extra = record.get("extra")
    return extra if isinstance(extra, dict) else {}


def read_activity(log_directory: Path, *, session_id: str | None = None) -> ActivityCounts:
    """Total what the session has decided, across every bar it has logged.

    Args:
        log_directory: Where the session writes its logs.
        session_id: Count only this session's bars when given. Logs persist across runs, so
            without this a new session would inherit the previous one's totals.

    Returns:
        The totals, with ``None`` for every count when no bar has been logged at all.
    """
    records = [
        record
        for record in _tail(log_directory / "paper.log")
        if record.get("message") == "bar processed"
        and (session_id is None or _extra(record).get("session_id") == session_id)
    ]
    if not records:
        return ActivityCounts()

    def total(field: str) -> int:
        return sum(
            value for record in records if isinstance(value := _extra(record).get(field), int)
        )

    return ActivityCounts(
        signals=total("signals"),
        intents=total("intents"),
        decisions=total("decisions"),
        fills=total("fills"),
        bars_seen=len(records),
    )


def read_timeline(
    log_directory: Path, *, session_id: str | None = None, limit: int = 40
) -> tuple[TimelineEvent, ...]:
    """Return recent events worth showing a person, newest first.

    Deliberately not every log line. A timeline is for reading, so a bar that produced
    nothing is folded away and only bars that did something get a row — otherwise a
    seventy-two hour run buries its four interesting moments under seventy-two identical
    ones.
    """
    events: list[TimelineEvent] = []
    events.extend(_session_events(log_directory, session_id))
    events.extend(_feed_events(log_directory))
    events.extend(_bar_events(log_directory, session_id))
    events.sort(key=lambda event: event.at, reverse=True)
    return tuple(events[:limit])


def _session_events(log_directory: Path, session_id: str | None) -> list[TimelineEvent]:
    """Lifecycle: the session starting, and the lock it took to do so."""
    out: list[TimelineEvent] = []
    for record in _tail(log_directory / "orchestration.log"):
        extra = _extra(record)
        if session_id is not None and extra.get("session_id") != session_id:
            continue
        at = _moment(record)
        if at is None:
            continue
        message = record.get("message")
        if message == "paper session starting":
            resumed = extra.get("resume") is True
            out.append(
                TimelineEvent(
                    at=at,
                    kind="session_started",
                    title="Session started",
                    detail="resumed from a snapshot" if resumed else "fresh start, no resume",
                    severity="good",
                )
            )
        elif message == "session lock released":
            out.append(
                TimelineEvent(
                    at=at,
                    kind="session_stopped",
                    title="Session stopped",
                    detail="the lock was released cleanly",
                    severity="warn",
                )
            )
    return out


_FEED_STATES = {
    "connected": ("feed_connected", "Market feed connected", "good"),
    "streaming": ("feed_streaming", "Market feed streaming", "good"),
    "connecting": ("feed_connecting", "Market feed connecting", "info"),
    "disconnected": ("feed_disconnected", "Market feed disconnected", "bad"),
    "reconnecting": ("feed_reconnecting", "Market feed reconnecting", "warn"),
}


def _feed_events(log_directory: Path) -> list[TimelineEvent]:
    """Connectivity, plus anything the feed complained about."""
    out: list[TimelineEvent] = []
    for record in _tail(log_directory / "marketdata.log"):
        at = _moment(record)
        if at is None:
            continue
        extra = _extra(record)
        message = record.get("message")
        if message == "feed state transition":
            to = extra.get("to")
            known = _FEED_STATES.get(to) if isinstance(to, str) else None
            if known is None:
                continue
            kind, title, severity = known
            out.append(TimelineEvent(at=at, kind=kind, title=title, detail=None, severity=severity))
        elif isinstance(message, str) and "gap" in message.lower():
            out.append(
                TimelineEvent(
                    at=at,
                    kind="data_gap",
                    title="Data gap detected",
                    detail=message,
                    severity="bad",
                )
            )
    return out


def _bar_events(log_directory: Path, session_id: str | None) -> list[TimelineEvent]:
    """Bars, but only the ones that did something, plus the first as a milestone."""
    out: list[TimelineEvent] = []
    seen = 0
    for record in _tail(log_directory / "paper.log"):
        if record.get("message") != "bar processed":
            continue
        extra = _extra(record)
        if session_id is not None and extra.get("session_id") != session_id:
            continue
        at = _moment(record)
        if at is None:
            continue
        seen += 1
        signals = extra.get("signals")
        fills = extra.get("fills")
        decisions = extra.get("decisions")
        interesting = any(isinstance(v, int) and v > 0 for v in (signals, fills, decisions))
        if seen == 1:
            out.append(
                TimelineEvent(
                    at=at,
                    kind="first_bar",
                    title="First candle processed",
                    detail=_bar_detail(extra),
                    severity="good",
                )
            )
        elif interesting:
            out.append(
                TimelineEvent(
                    at=at,
                    kind="bar_activity",
                    title=_activity_title(signals, decisions, fills),
                    detail=_bar_detail(extra),
                    severity="good" if isinstance(fills, int) and fills > 0 else "info",
                )
            )
    return out


def _activity_title(signals: object, decisions: object, fills: object) -> str:
    """Name what a bar actually did, in the order a person would care about it."""
    if isinstance(fills, int) and fills > 0:
        return f"Order filled ({fills})"
    if isinstance(decisions, int) and decisions > 0:
        return f"Risk decision taken ({decisions})"
    if isinstance(signals, int) and signals > 0:
        return f"Signal generated ({signals})"
    return "Candle processed"


def _bar_detail(extra: dict[str, object]) -> str | None:
    """Describe a bar by the instrument and candle it covered."""
    symbol = extra.get("symbol")
    close_time = extra.get("close_time")
    if isinstance(symbol, str) and isinstance(close_time, str):
        return f"{symbol} candle closing {close_time}"
    return None
