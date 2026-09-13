"""Building slower bars from hourly ones, without inventing or losing any data."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.research.resample import resample_bars
from tests.factories import ANCHOR, make_bar


def _hours(count: int, *, start: int = 0) -> tuple[MarketBar, ...]:
    return tuple(
        make_bar(
            index=start + i,
            open_price=Decimal(100 + i),
            close=Decimal(101 + i),
            high=Decimal(110 + i),
            low=Decimal(90 + i),
            volume=Decimal(1),
        )
        for i in range(count)
    )


def test_four_hourly_bars_become_one_four_hour_bar() -> None:
    (bar,) = resample_bars(_hours(4), Timeframe.H4)

    assert bar.timeframe is Timeframe.H4
    assert bar.open_time == ANCHOR
    assert bar.close_time == ANCHOR + timedelta(hours=4)
    assert bar.open == Decimal(100)  # first open
    assert bar.close == Decimal(104)  # last close
    assert bar.high == Decimal(113)  # highest high
    assert bar.low == Decimal(90)  # lowest low
    assert bar.volume == Decimal(4)
    assert bar.is_closed


def test_a_day_of_hours_becomes_one_daily_bar() -> None:
    bars = resample_bars(_hours(48), Timeframe.D1)
    assert [b.open_time for b in bars] == [ANCHOR, ANCHOR + timedelta(days=1)]


def test_an_incomplete_leading_or_trailing_bucket_is_dropped_not_padded() -> None:
    # Hours 01:00-11:00: [00,04) lacks 00:00 and is the first bucket; [08,12) is complete;
    # nothing is padded to make [00,04) look whole.
    bars = resample_bars(_hours(11, start=1), Timeframe.H4)
    assert [b.open_time for b in bars] == [ANCHOR + timedelta(hours=4), ANCHOR + timedelta(hours=8)]


def test_an_incomplete_trailing_bucket_is_dropped() -> None:
    bars = resample_bars(_hours(6), Timeframe.H4)
    assert [b.open_time for b in bars] == [ANCHOR]


def test_a_missing_hour_inside_a_bucket_is_refused() -> None:
    bars = _hours(8)
    with pytest.raises(ValueError, match="missing"):
        resample_bars((*bars[:2], *bars[3:]), Timeframe.H4)


def test_resampling_to_the_same_timeframe_is_refused() -> None:
    with pytest.raises(ValueError, match="slower"):
        resample_bars(_hours(4), Timeframe.H1)
