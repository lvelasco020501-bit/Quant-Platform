"""CSV parsing, timeframe utilities, closed-candle policy and gap detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quantplatform.core.enums import Timeframe
from quantplatform.core.errors import DataIntegrityError, DataProviderError
from quantplatform.data.csv_loader import compute_checksum, load_csv_records
from quantplatform.data.records import CANONICAL_COLUMNS
from quantplatform.data.timeframes import (
    missing_open_times,
    next_open_time,
    parse_timeframe,
)
from tests.data_helpers import fixture, make_clock, make_policy

# --- CSV parsing ----------------------------------------------------------------------------


def test_canonical_file_parses_every_row() -> None:
    source = load_csv_records(fixture("valid.csv"), source_id="test")
    assert len(source.records) == 4
    assert source.header[: len(CANONICAL_COLUMNS)] == CANONICAL_COLUMNS


def test_parsed_values_are_kept_as_original_strings() -> None:
    source = load_csv_records(fixture("valid.csv"), source_id="test")
    first = source.records[0]
    assert first.open == "50000.10"
    assert first.symbol == "BTC/USDT"
    assert first.source_row == 1
    assert first.source == "test"


def test_empty_trade_count_is_preserved_as_empty_string() -> None:
    source = load_csv_records(fixture("valid.csv"), source_id="test")
    assert source.records[3].trade_count == ""


def test_missing_required_column_fails_the_whole_file() -> None:
    with pytest.raises(DataProviderError, match="missing required columns"):
        load_csv_records(fixture("missing_column.csv"), source_id="test")


def test_completely_empty_file_fails() -> None:
    with pytest.raises(DataProviderError, match="no header row"):
        load_csv_records(fixture("empty.csv"), source_id="test")


def test_header_only_file_parses_to_zero_records() -> None:
    source = load_csv_records(fixture("header_only.csv"), source_id="test")
    assert source.records == ()


def test_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(DataProviderError):
        load_csv_records(tmp_path / "absent.csv", source_id="test")


def test_unknown_extra_columns_are_retained_but_do_not_alter_canonical_values() -> None:
    source = load_csv_records(fixture("extra_column.csv"), source_id="test")
    record = source.records[0]
    assert record.extra_fields["vwap"] == "50050"
    assert record.close == "50100"


def test_configurable_delimiter(tmp_path: Path) -> None:
    path = tmp_path / "semicolon.csv"
    path.write_text(
        ";".join(CANONICAL_COLUMNS)
        + "\n"
        + ";".join(
            [
                "BTC/USDT",
                "spot",
                "1h",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T01:00:00+00:00",
                "50000",
                "50200",
                "49900",
                "50100",
                "12.5",
                "100",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    source = load_csv_records(path, source_id="test", delimiter=";")
    assert source.records[0].close == "50100"


def test_undecodable_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "latin.csv"
    path.write_bytes((",".join(CANONICAL_COLUMNS) + "\n").encode() + b"\xff\xfe binary garbage\n")
    with pytest.raises(DataProviderError, match="decode"):
        load_csv_records(path, source_id="test", encoding="utf-8")


def test_checksum_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    first.write_text("same", encoding="utf-8")
    second.write_text("same", encoding="utf-8")
    assert compute_checksum(first) == compute_checksum(second)

    second.write_text("different", encoding="utf-8")
    assert compute_checksum(first) != compute_checksum(second)


def test_checksum_of_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(DataProviderError, match="checksum"):
        compute_checksum(tmp_path / "absent.csv")


# --- Timeframes -----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_every_required_timeframe_parses(value: str) -> None:
    assert parse_timeframe(value).value == value


def test_timeframe_parsing_ignores_surrounding_whitespace() -> None:
    assert parse_timeframe("  1h  ") is Timeframe.H1


@pytest.mark.parametrize("value", ["", "1y", "2w", "hourly", "60"])
def test_unsupported_timeframes_are_rejected(value: str) -> None:
    with pytest.raises(DataIntegrityError, match="unsupported timeframe"):
        parse_timeframe(value)


def test_next_open_time_is_one_interval_later() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert next_open_time(start, Timeframe.H1) == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert next_open_time(start, Timeframe.D1) == datetime(2026, 1, 2, tzinfo=UTC)


@given(
    timeframe=st.sampled_from(list(Timeframe)),
    steps=st.integers(min_value=1, max_value=50),
)
def test_repeated_next_open_advances_by_exact_multiples(timeframe: Timeframe, steps: int) -> None:
    # Anchored on a Monday so weekly bars start on their own grid.
    cursor = start = datetime(2026, 1, 5, tzinfo=UTC)
    for _ in range(steps):
        cursor = next_open_time(cursor, timeframe)
    assert cursor - start == timeframe.duration * steps


# --- Gap detection --------------------------------------------------------------------------


def _hours(*hours: int) -> list[datetime]:
    return [datetime(2026, 1, 1, hour, tzinfo=UTC) for hour in hours]


def test_contiguous_series_has_no_gaps() -> None:
    assert missing_open_times(_hours(0, 1, 2, 3), Timeframe.H1) == ()


def test_series_shorter_than_two_bars_has_no_gaps() -> None:
    assert missing_open_times(_hours(0), Timeframe.H1) == ()
    assert missing_open_times([], Timeframe.H1) == ()


def test_single_missing_bar_is_reported() -> None:
    runs = missing_open_times(_hours(0, 1, 3), Timeframe.H1)
    assert len(runs) == 1
    assert runs[0].count == 1
    assert runs[0].first_missing_open_time == datetime(2026, 1, 1, 2, tzinfo=UTC)
    assert runs[0].last_missing_open_time == datetime(2026, 1, 1, 2, tzinfo=UTC)


def test_contiguous_missing_bars_collapse_into_one_run() -> None:
    runs = missing_open_times(_hours(0, 5), Timeframe.H1)
    assert len(runs) == 1
    assert runs[0].count == 4
    assert runs[0].first_missing_open_time == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert runs[0].last_missing_open_time == datetime(2026, 1, 1, 4, tzinfo=UTC)


def test_separate_gaps_are_reported_separately() -> None:
    runs = missing_open_times(_hours(0, 2, 5), Timeframe.H1)
    assert [run.count for run in runs] == [1, 2]


def test_unordered_input_is_handled() -> None:
    runs = missing_open_times(_hours(3, 0, 1), Timeframe.H1)
    assert len(runs) == 1
    assert runs[0].first_missing_open_time == datetime(2026, 1, 1, 2, tzinfo=UTC)


def test_off_grid_open_time_is_refused_rather_than_miscounted() -> None:
    off_grid = [datetime(2026, 1, 1, 0, 7, tzinfo=UTC), datetime(2026, 1, 1, 2, tzinfo=UTC)]
    with pytest.raises(DataIntegrityError, match="off the timeframe grid"):
        missing_open_times(off_grid, Timeframe.H1)


@given(present=st.lists(st.integers(min_value=0, max_value=23), min_size=2, unique=True))
def test_gap_count_always_completes_the_grid(present: list[int]) -> None:
    open_times = _hours(*sorted(present))
    runs = missing_open_times(open_times, Timeframe.H1)
    span = max(present) - min(present) + 1
    assert sum(run.count for run in runs) == span - len(present)


# --- Closed-candle policy -------------------------------------------------------------------


def test_candle_is_open_before_its_close_time() -> None:
    policy = make_policy(clock=make_clock(datetime(2026, 1, 1, 0, 59, tzinfo=UTC)))
    assert not policy.is_closed(datetime(2026, 1, 1, 1, tzinfo=UTC))


def test_candle_is_closed_exactly_at_its_close_time() -> None:
    policy = make_policy(clock=make_clock(datetime(2026, 1, 1, 1, tzinfo=UTC)))
    assert policy.is_closed(datetime(2026, 1, 1, 1, tzinfo=UTC))


def test_grace_period_defers_closure() -> None:
    close_time = datetime(2026, 1, 1, 1, tzinfo=UTC)
    at_close = make_policy(
        clock=make_clock(close_time),
        grace_seconds=30,
    )
    assert not at_close.is_closed(close_time)
    assert at_close.finalises_at(close_time) == close_time + timedelta(seconds=30)

    after_grace = make_policy(
        clock=make_clock(close_time + timedelta(seconds=30)),
        grace_seconds=30,
    )
    assert after_grace.is_closed(close_time)


def test_reference_time_overrides_the_clock() -> None:
    policy = make_policy(
        clock=make_clock(datetime(2020, 1, 1, tzinfo=UTC)),
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert policy.reference_now() == datetime(2026, 6, 1, tzinfo=UTC)
    assert policy.is_closed(datetime(2026, 1, 1, 1, tzinfo=UTC))


def test_policy_advances_with_the_clock() -> None:
    clock = make_clock(datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
    policy = make_policy(clock=clock)
    close_time = datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert not policy.is_closed(close_time)

    clock.advance(timedelta(minutes=30))
    assert policy.is_closed(close_time)


# --- Decimal fidelity -----------------------------------------------------------------------


@given(
    value=st.decimals(
        min_value=Decimal("0.000000000000000001"),
        max_value=Decimal("1000000"),
        places=18,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_decimal_strings_round_trip_through_the_raw_record(value: Decimal) -> None:
    # The loader keeps values as text, so no precision can be lost before validation.
    rendered = format(value, "f")
    assert Decimal(rendered) == value
