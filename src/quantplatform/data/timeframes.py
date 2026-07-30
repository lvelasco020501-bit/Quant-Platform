"""Timeframe parsing and bar-grid arithmetic used by the data layer.

This module deliberately holds only what :mod:`quantplatform.core.timeutils` and
:class:`quantplatform.core.enums.Timeframe` do not already provide. Duration, grid
alignment and close-time arithmetic all live in core and are reused here rather than
reimplemented; what is added is string parsing with a domain error, next-open arithmetic,
and the expected-interval enumeration that gap detection needs.

Only fixed-duration intervals are supported. Calendar-variable intervals (calendar months,
quarters, years) are out of scope for this phase and are not representable by
:class:`~quantplatform.core.enums.Timeframe`, whose longest member is a fixed seven-day
week anchored to Monday.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from quantplatform.core.enums import Timeframe
from quantplatform.core.errors import DataIntegrityError
from quantplatform.core.timeutils import bar_close_time, ensure_utc, is_on_timeframe_grid

__all__ = ["GapRun", "missing_open_times", "next_open_time", "parse_timeframe"]

_MIN_BARS_FOR_GAP_DETECTION = 2
"""A gap is only meaningful between two bars, so a shorter series can have none."""


def parse_timeframe(value: str) -> Timeframe:
    """Parse a timeframe string such as ``"1h"`` into a :class:`Timeframe`.

    Args:
        value: Candidate timeframe string; surrounding whitespace is ignored.

    Returns:
        The matching timeframe member.

    Raises:
        DataIntegrityError: If the value does not name a supported timeframe.
    """
    candidate = value.strip()
    try:
        return Timeframe(candidate)
    except ValueError as exc:
        raise DataIntegrityError(
            "unsupported timeframe",
            value=candidate,
            supported=[member.value for member in Timeframe],
        ) from exc


def next_open_time(open_time: datetime, timeframe: Timeframe) -> datetime:
    """Return the opening timestamp of the bar immediately following ``open_time``.

    Because every supported interval is fixed-duration, the next open time is also the
    current bar's close time.

    Args:
        open_time: Opening timestamp of the current bar.
        timeframe: Bar interval.

    Returns:
        The next bar's opening timestamp.
    """
    return bar_close_time(open_time, timeframe)


@dataclass(frozen=True, slots=True)
class GapRun:
    """A contiguous run of missing bars between two present bars.

    Gaps are reported per contiguous run rather than per missing bar so that a long outage
    produces one finding describing the whole window instead of thousands of identical
    ones.
    """

    first_missing_open_time: datetime
    last_missing_open_time: datetime
    count: int


def _iter_expected_open_times(
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
) -> Iterator[datetime]:
    """Yield every grid open time in the half-open interval ``[start, end)``."""
    cursor = start
    while cursor < end:
        yield cursor
        cursor = next_open_time(cursor, timeframe)


def missing_open_times(
    present_open_times: list[datetime],
    timeframe: Timeframe,
) -> tuple[GapRun, ...]:
    """Detect gaps between the earliest and latest supplied bar open times.

    Only interior gaps are reported: a dataset is never faulted for starting later or
    ending earlier than some external expectation, because the data layer has no way to
    know what range the caller intended to cover. Detection therefore spans exactly the
    range the data itself claims.

    Args:
        present_open_times: Open times of the bars actually present, in any order. They
            must already be aligned to the timeframe grid.
        timeframe: Bar interval defining the expected spacing.

    Returns:
        One :class:`GapRun` per contiguous run of missing bars, ordered by time. Empty when
        the series is contiguous or holds fewer than two bars.

    Raises:
        DataIntegrityError: If any supplied open time is not aligned to the grid, since gap
            arithmetic would otherwise silently produce nonsense.
    """
    if len(present_open_times) < _MIN_BARS_FOR_GAP_DETECTION:
        return ()

    normalised = sorted(ensure_utc(value) for value in present_open_times)
    for value in normalised:
        if not is_on_timeframe_grid(value, timeframe):
            raise DataIntegrityError(
                "cannot compute gaps from an open time that is off the timeframe grid",
                open_time=value.isoformat(),
                timeframe=timeframe.value,
            )

    present = set(normalised)
    runs: list[GapRun] = []
    run_start: datetime | None = None
    run_end: datetime | None = None
    run_count = 0

    for expected in _iter_expected_open_times(normalised[0], normalised[-1], timeframe):
        if expected in present:
            if run_start is not None and run_end is not None:
                runs.append(GapRun(run_start, run_end, run_count))
                run_start = None
                run_end = None
                run_count = 0
            continue
        if run_start is None:
            run_start = expected
        run_end = expected
        run_count += 1

    if run_start is not None and run_end is not None:
        runs.append(GapRun(run_start, run_end, run_count))
    return tuple(runs)
