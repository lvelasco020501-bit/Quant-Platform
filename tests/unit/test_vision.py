"""The Binance Vision reader: the checks a downloaded archive has to survive.

M15 proved these rules against six and a half years of BTC inside a one-off script. M16 runs
them over six markets, so they live here instead, where they can be tested directly rather
than only by the dataset they produce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research.digest import bars_digest
from quantplatform.research.vision import (
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

HOUR = timedelta(hours=1)


def row(hour: int, *, close: str = "100", volume: str = "1", high: str = "101") -> Row:
    return Row(
        open_time=datetime(2020, 1, 1, hour, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal(volume),
        trade_count=10,
        source="monthly",
    )


# --- Timestamps ------------------------------------------------------------------------------


def test_thirteen_digits_are_milliseconds_and_sixteen_are_microseconds() -> None:
    # Binance switched units mid-archive: 2024 and earlier are ms, 2025 onward µs.
    assert open_time("1577836800000") == (datetime(2020, 1, 1, tzinfo=UTC), "ms")
    assert open_time("1735689600000000") == (datetime(2025, 1, 1, tzinfo=UTC), "us")


def test_a_timestamp_of_an_unknown_width_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="unrecognised timestamp width"):
        open_time("15778368000")


def test_a_kline_row_is_read_into_the_fields_the_platform_stores() -> None:
    line = ["1577836800000", "7195.24", "7196.25", "7175.46", "7177.02", "511.8", "x", "y", "982"]
    parsed = parse_kline(line, source="monthly")
    assert parsed is not None
    assert parsed.open_time == datetime(2020, 1, 1, tzinfo=UTC)
    assert (parsed.open, parsed.high, parsed.low, parsed.close) == (
        Decimal("7195.24"),
        Decimal("7196.25"),
        Decimal("7175.46"),
        Decimal("7177.02"),
    )
    assert parsed.trade_count == 982


def test_a_header_or_blank_line_is_skipped_not_parsed_as_a_bar() -> None:
    assert parse_kline([], source="monthly") is None
    assert parse_kline(["open_time", "open"], source="monthly") is None


# --- Duplicates ------------------------------------------------------------------------------


def test_one_bar_per_open_time_survives_and_duplicates_are_reported() -> None:
    kept, duplicates, conflicts = deduplicate([row(0), row(1), row(1)])
    assert [r.open_time.hour for r in kept] == [0, 1]
    assert len(duplicates) == 1
    assert conflicts == []


def test_duplicates_that_disagree_are_reported_as_conflicts_never_silently_picked() -> None:
    kept, duplicates, conflicts = deduplicate([row(1, close="100"), row(1, close="777")])
    assert len(kept) == 1
    assert len(duplicates) == 1
    assert len(conflicts) == 1
    assert "777" in str(conflicts[0])


def test_bars_come_back_in_time_order_whatever_order_they_arrived_in() -> None:
    kept, _, _ = deduplicate([row(5), row(2), row(9)])
    assert [r.open_time.hour for r in kept] == [2, 5, 9]


# --- Gaps ------------------------------------------------------------------------------------


def test_every_missing_hour_in_the_span_is_found() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    missing = missing_hours([row(0), row(1), row(4)], start=start, end=start + 5 * HOUR)
    assert missing == [start + 2 * HOUR, start + 3 * HOUR]


def test_consecutive_missing_hours_are_grouped_so_one_outage_reads_as_one_event() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    runs = gap_runs([start, start + HOUR, start + 2 * HOUR, start + 8 * HOUR])
    assert len(runs) == 2
    assert runs[0]["hours"] == 3
    assert runs[1]["hours"] == 1


def test_no_gaps_means_no_runs() -> None:
    assert gap_runs([]) == []


# --- OHLCV -----------------------------------------------------------------------------------


def test_a_high_below_the_close_is_a_violation() -> None:
    bad = ohlcv_violations([row(0, close="500", high="101")])
    assert len(bad) == 1
    assert "high_not_max" in bad[0]["reasons"]


def test_a_negative_volume_and_a_zero_price_are_violations() -> None:
    zero_price = Row(
        open_time=datetime(2020, 1, 1, tzinfo=UTC),
        open=Decimal("0"),
        high=Decimal("0"),
        low=Decimal("0"),
        close=Decimal("0"),
        volume=Decimal("-1"),
        trade_count=0,
        source="monthly",
    )
    reasons = ohlcv_violations([zero_price])[0]["reasons"]
    assert "negative_volume" in reasons
    assert "non_positive_price" in reasons


def test_an_ordinary_bar_passes() -> None:
    assert ohlcv_violations([row(0), row(1)]) == []


# --- Output ----------------------------------------------------------------------------------


def test_bars_carry_the_assets_own_symbol_and_a_derived_close_time() -> None:
    bars = to_market_bars([row(0)], symbol="SOL/USDT")
    assert bars[0].symbol == "SOL/USDT"
    assert bars[0].timeframe is Timeframe.H1
    assert bars[0].close_time == bars[0].open_time + HOUR


def test_writing_the_same_bars_twice_gives_the_same_file_and_the_same_digest(
    tmp_path: Path,
) -> None:
    bars = to_market_bars([row(0), row(1)], symbol="ETH/USDT")
    first = write_csv(tmp_path / "a.csv", bars)
    second = write_csv(tmp_path / "b.csv", bars)
    assert first == second
    assert bars_digest(bars) == bars_digest(bars)
    assert (tmp_path / "a.csv").read_text(encoding="utf-8").splitlines()[0].startswith("symbol,")
