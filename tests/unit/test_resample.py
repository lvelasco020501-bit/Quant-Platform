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


# --- Documented exchange outages (M15) ----------------------------------------------------------
#
# Binance halted trading for a few hours at a time between 2020 and 2023. Those hours have no
# kline because nothing traded, so the slower bar is built from the hours that did trade — which
# is exactly what the exchange's own 4h and 1d klines contain. Only hours named explicitly may be
# absent: an undocumented hole is refused exactly as before.


def test_a_documented_outage_hour_is_left_out_of_its_bucket_not_invented() -> None:
    bars = _hours(8)
    outage = bars[2].open_time
    first, second = resample_bars(
        (*bars[:2], *bars[3:]), Timeframe.H4, allowed_missing=frozenset({outage})
    )
    traded = (bars[0], bars[1], bars[3])
    assert first.open_time == ANCHOR
    assert first.open == bars[0].open
    assert first.close == bars[3].close
    assert first.high == max(b.high for b in traded)
    assert first.low == min(b.low for b in traded)
    assert first.volume == Decimal(3)
    assert second.volume == Decimal(4)


def test_an_outage_in_the_first_hour_opens_the_bar_at_the_first_trade() -> None:
    bars = _hours(8)
    (bar, _) = resample_bars(bars[1:], Timeframe.H4, allowed_missing=frozenset({ANCHOR}))
    assert bar.open_time == ANCHOR
    assert bar.open == bars[1].open


def test_an_undocumented_hole_is_still_refused_when_others_are_allowed() -> None:
    bars = _hours(8)
    with pytest.raises(ValueError, match="missing"):
        resample_bars(
            (*bars[:2], *bars[3:]), Timeframe.H4, allowed_missing=frozenset({bars[5].open_time})
        )


def test_a_bucket_in_which_nothing_traded_is_left_out_like_the_exchange_does() -> None:
    # Binance's own 4h series has no bar at 2020-02-19 12:00: all four hours were an outage.
    # When every hour of a bucket is a documented outage the honest output is no bar at all —
    # not an error, and certainly not an invented one.
    bars = _hours(12)
    outage = frozenset(b.open_time for b in bars[4:8])
    out = resample_bars((*bars[:4], *bars[8:]), Timeframe.H4, allowed_missing=outage)
    assert [b.open_time for b in out] == [ANCHOR, ANCHOR + timedelta(hours=8)]


def test_a_wholly_missing_undocumented_bucket_is_still_refused() -> None:
    bars = _hours(12)
    with pytest.raises(ValueError, match="missing"):
        resample_bars((*bars[:4], *bars[8:]), Timeframe.H4)


def test_a_wholly_missing_bucket_only_partly_documented_is_refused() -> None:
    bars = _hours(12)
    partly = frozenset(b.open_time for b in bars[4:7])  # 07:00 is not documented
    with pytest.raises(ValueError, match="missing"):
        resample_bars((*bars[:4], *bars[8:]), Timeframe.H4, allowed_missing=partly)
