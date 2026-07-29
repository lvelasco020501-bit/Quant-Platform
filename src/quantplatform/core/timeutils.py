"""Timezone-aware UTC time helpers and bar grid arithmetic.

Every timestamp inside the platform is timezone-aware and expressed in UTC. Naive
datetimes are rejected at the boundary rather than silently assumed to be local time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from quantplatform.core.enums import Timeframe
from quantplatform.core.errors import DomainValidationError

__all__ = [
    "bar_close_time",
    "ensure_utc",
    "floor_to_timeframe",
    "is_bar_closed",
    "is_on_timeframe_grid",
    "utc_now",
]

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_WEEK_ANCHOR_OFFSET_SECONDS: Final[int] = 4 * 86_400
"""Seconds from the Unix epoch (a Thursday) to the first Monday, 1970-01-05."""


def utc_now() -> datetime:
    """Return the current wall-clock time as a timezone-aware UTC datetime.

    Application code must obtain time from an injected :class:`~quantplatform.core.clock.Clock`
    instead of calling this directly; it exists for the real clock implementation.

    Returns:
        The current UTC time.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Validate and normalise a datetime to UTC.

    Args:
        value: Datetime to normalise. Must carry timezone information.

    Returns:
        The equivalent instant expressed in UTC.

    Raises:
        DomainValidationError: If ``value`` is naive.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(
            "naive datetimes are not accepted; supply a timezone-aware UTC datetime",
            value=value.isoformat(),
        )
    return value.astimezone(UTC)


def floor_to_timeframe(value: datetime, timeframe: Timeframe) -> datetime:
    """Snap a timestamp down to the opening boundary of its bar.

    Sub-daily and daily grids are anchored to the Unix epoch. Weekly bars are anchored to
    Monday 00:00 UTC, matching the convention used by major crypto venues.

    Args:
        value: Timezone-aware timestamp.
        timeframe: Bar interval defining the grid.

    Returns:
        The opening timestamp of the bar containing ``value``.

    Raises:
        DomainValidationError: If ``value`` is naive.
    """
    normalised = ensure_utc(value)
    elapsed = int((normalised - _EPOCH).total_seconds())
    interval = timeframe.seconds
    anchor = _WEEK_ANCHOR_OFFSET_SECONDS if timeframe is Timeframe.W1 else 0
    floored = ((elapsed - anchor) // interval) * interval + anchor
    return _EPOCH + timedelta(seconds=floored)


def is_on_timeframe_grid(value: datetime, timeframe: Timeframe) -> bool:
    """Return whether a timestamp lies exactly on a bar boundary.

    Args:
        value: Timezone-aware timestamp.
        timeframe: Bar interval defining the grid.

    Returns:
        ``True`` when ``value`` is a valid bar opening timestamp.
    """
    return floor_to_timeframe(value, timeframe) == ensure_utc(value)


def bar_close_time(open_time: datetime, timeframe: Timeframe) -> datetime:
    """Return the exclusive closing timestamp of a bar.

    Args:
        open_time: Opening timestamp of the bar.
        timeframe: Bar interval.

    Returns:
        ``open_time + timeframe.duration``.
    """
    return ensure_utc(open_time) + timeframe.duration


def is_bar_closed(close_time: datetime, now: datetime) -> bool:
    """Return whether a bar is closed as of ``now``.

    A bar is closed only once the current instant has reached its exclusive close
    timestamp. Strategies must never act on an open bar.

    Args:
        close_time: Exclusive closing timestamp of the bar.
        now: Current instant, obtained from the injected clock.

    Returns:
        ``True`` when the bar has completed.
    """
    return ensure_utc(now) >= ensure_utc(close_time)
