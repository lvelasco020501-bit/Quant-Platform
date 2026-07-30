"""Record-level and dataset-level validation, and normalisation into domain bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quantplatform.core.enums import DataQualityIssue, FindingSeverity
from quantplatform.data.csv_loader import load_csv_records
from quantplatform.data.normalization import normalize_record, normalize_source
from quantplatform.data.results import RejectedRecord, ValidatedRecord
from quantplatform.data.validation import DatasetValidator, RecordValidator
from tests.data_helpers import (
    EXPECTATIONS,
    SYMBOL,
    TIMEFRAME,
    fixture,
    make_clock,
    make_policy,
    make_raw_record,
    make_recorder,
)


def _validator(
    recorder: object = None,
    *,
    now: datetime | None = None,
    grace_seconds: int = 0,
) -> tuple[RecordValidator, object]:
    """Build a record validator and return it with its recorder."""
    clock = make_clock(now) if now is not None else make_clock()
    resolved = recorder if recorder is not None else make_recorder(clock=clock)
    return (
        RecordValidator(
            expectations=EXPECTATIONS,
            closed_candle_policy=make_policy(clock=clock, grace_seconds=grace_seconds),
            recorder=resolved,  # type: ignore[arg-type]
        ),
        resolved,
    )


def _codes(outcome: RejectedRecord) -> set[DataQualityIssue]:
    return {finding.code for finding in outcome.findings}


# --- Record-level validation ------------------------------------------------------------------


def test_sound_record_is_accepted() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record())
    assert isinstance(result, ValidatedRecord)
    assert result.open == Decimal("50000")
    assert result.trade_count == 100


def test_empty_trade_count_normalises_to_none() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(trade_count=""))
    assert isinstance(result, ValidatedRecord)
    assert result.trade_count is None


@pytest.mark.parametrize(
    "field",
    ["symbol", "market_type", "timeframe", "open_time", "close_time", "volume"],
)
def test_missing_required_field_is_rejected(field: str) -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(**{field: ""}))  # type: ignore[arg-type]
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.MALFORMED_RECORD in _codes(result)


def test_naive_timestamp_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(
        make_raw_record(open_time="2026-01-01T00:00:00", close_time="2026-01-01T01:00:00")
    )
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.MALFORMED_RECORD in _codes(result)
    assert any("timezone offset" in finding.message for finding in result.findings)


def test_non_utc_offset_is_normalised_to_utc() -> None:
    validator, _ = _validator()
    result = validator.validate(
        make_raw_record(
            open_time="2026-01-01T02:00:00+02:00",
            close_time="2026-01-01T03:00:00+02:00",
        )
    )
    assert isinstance(result, ValidatedRecord)
    assert result.open_time == datetime(2026, 1, 1, 0, tzinfo=UTC)
    assert result.open_time.tzinfo is UTC


@pytest.mark.parametrize("value", ["not-a-number", "", "1.2.3"])
def test_malformed_numeric_field_is_rejected(value: str) -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(open_price=value))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.MALFORMED_RECORD in _codes(result)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_decimals_are_rejected(value: str) -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(open_price=value))
    assert isinstance(result, RejectedRecord)
    assert any("finite" in finding.message for finding in result.findings)


def test_wrong_symbol_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(symbol="ETH/USDT"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.UNEXPECTED_SYMBOL in _codes(result)


def test_wrong_market_type_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(market_type="futures"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.UNEXPECTED_MARKET_TYPE in _codes(result)


def test_wrong_timeframe_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(timeframe="15m"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.UNEXPECTED_TIMEFRAME in _codes(result)


def test_open_time_after_close_time_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(
        make_raw_record(
            open_time="2026-01-01T02:00:00+00:00",
            close_time="2026-01-01T01:00:00+00:00",
        )
    )
    assert isinstance(result, RejectedRecord)
    assert any("strictly before" in finding.message for finding in result.findings)


def test_duration_not_matching_the_timeframe_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(close_time="2026-01-01T02:00:00+00:00"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.UNEXPECTED_TIMEFRAME in _codes(result)


def test_open_time_off_the_grid_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(
        make_raw_record(
            open_time="2026-01-01T00:07:00+00:00",
            close_time="2026-01-01T01:07:00+00:00",
        )
    )
    assert isinstance(result, RejectedRecord)
    assert any("grid" in finding.message for finding in result.findings)


@pytest.mark.parametrize("field", ["open_price", "high", "low", "close"])
def test_non_positive_prices_are_rejected(field: str) -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(**{field: "0"}))  # type: ignore[arg-type]
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.INVALID_OHLC in _codes(result)


def test_high_below_the_body_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(high="49000"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.INVALID_OHLC in _codes(result)


def test_low_above_the_body_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(low="50500"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.INVALID_OHLC in _codes(result)


def test_negative_volume_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(volume="-1"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.NEGATIVE_VOLUME in _codes(result)


def test_zero_volume_is_accepted() -> None:
    validator, _ = _validator()
    assert isinstance(validator.validate(make_raw_record(volume="0")), ValidatedRecord)


def test_negative_trade_count_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(trade_count="-5"))
    assert isinstance(result, RejectedRecord)
    assert any("trade_count" in finding.message for finding in result.findings)


def test_non_integer_trade_count_is_rejected() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(trade_count="abc"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.MALFORMED_RECORD in _codes(result)


def test_unclosed_candle_is_rejected() -> None:
    validator, _ = _validator(now=datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
    result = validator.validate(make_raw_record())
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.OPEN_CANDLE in _codes(result)


def test_candle_within_the_grace_period_is_still_open() -> None:
    validator, _ = _validator(now=datetime(2026, 1, 1, 1, tzinfo=UTC), grace_seconds=60)
    result = validator.validate(make_raw_record())
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.OPEN_CANDLE in _codes(result)


def test_every_problem_with_a_record_is_reported_not_just_the_first() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(symbol="ETH/USDT", volume="-1"))
    assert isinstance(result, RejectedRecord)
    assert DataQualityIssue.UNEXPECTED_SYMBOL in _codes(result)
    assert DataQualityIssue.NEGATIVE_VOLUME in _codes(result)


def test_findings_carry_the_source_row() -> None:
    validator, _ = _validator()
    result = validator.validate(make_raw_record(source_row=7, volume="-1"))
    assert isinstance(result, RejectedRecord)
    assert all(finding.source_row == 7 for finding in result.findings)


# --- Dataset-level validation -----------------------------------------------------------------


def _dataset_validator(
    recorder: object,
    *,
    gap_severity: FindingSeverity = FindingSeverity.WARNING,
    max_allowed_gap_bars: int = 24,
    staleness_budget: timedelta = timedelta(hours=2),
    reference_time: datetime | None = None,
) -> DatasetValidator:
    return DatasetValidator(
        expectations=EXPECTATIONS,
        recorder=recorder,  # type: ignore[arg-type]
        gap_severity=gap_severity,
        max_allowed_gap_bars=max_allowed_gap_bars,
        staleness_budget=staleness_budget,
        reference_time=reference_time or datetime(2026, 1, 1, 5, tzinfo=UTC),
    )


def _validated(hour: int, *, close: str = "50100", row: int = 1) -> ValidatedRecord:
    open_time = datetime(2026, 1, 1, hour, tzinfo=UTC)
    return ValidatedRecord(
        source_row=row,
        symbol=SYMBOL,
        market_type=EXPECTATIONS.market_type,
        timeframe=TIMEFRAME,
        open_time=open_time,
        close_time=open_time + TIMEFRAME.duration,
        open=Decimal("50000"),
        high=Decimal("60000"),
        low=Decimal("40000"),
        close=Decimal(close),
        volume=Decimal("1"),
        trade_count=None,
    )


def test_mixed_symbols_are_detected_across_the_dataset() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder).inspect_source_heterogeneity(
        [make_raw_record(symbol=SYMBOL), make_raw_record(symbol="ETH/USDT")]
    )
    findings = recorder.findings  # type: ignore[attr-defined]
    assert any(finding.code is DataQualityIssue.UNEXPECTED_SYMBOL for finding in findings)
    assert any("mixes 2 distinct symbol" in finding.message for finding in findings)


def test_mixed_timeframes_are_detected_across_the_dataset() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder).inspect_source_heterogeneity(
        [make_raw_record(timeframe="1h"), make_raw_record(timeframe="15m")]
    )
    assert any(
        finding.code is DataQualityIssue.UNEXPECTED_TIMEFRAME
        for finding in recorder.findings  # type: ignore[attr-defined]
    )


def test_homogeneous_dataset_raises_no_heterogeneity_finding() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder).inspect_source_heterogeneity(
        [make_raw_record(), make_raw_record()]
    )
    assert recorder.findings == ()  # type: ignore[attr-defined]


def test_out_of_order_input_is_detected_before_sorting() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder).check_ordering([_validated(1, row=1), _validated(0, row=2)])
    findings = recorder.findings  # type: ignore[attr-defined]
    assert len(findings) == 1
    assert findings[0].code is DataQualityIssue.OUT_OF_ORDER_BAR
    assert findings[0].severity is FindingSeverity.WARNING


def test_ordered_input_raises_no_ordering_finding() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder).check_ordering([_validated(0), _validated(1)])
    assert recorder.findings == ()  # type: ignore[attr-defined]


def test_exact_duplicate_is_informational_and_collapsed() -> None:
    recorder = make_recorder()
    kept = _dataset_validator(recorder).deduplicate([_validated(0, row=1), _validated(0, row=2)])
    assert len(kept) == 1
    findings = recorder.findings  # type: ignore[attr-defined]
    assert findings[0].severity is FindingSeverity.INFO
    assert findings[0].code is DataQualityIssue.DUPLICATE_BAR


def test_conflicting_duplicate_keeps_the_first_and_reports_an_error() -> None:
    recorder = make_recorder()
    kept = _dataset_validator(recorder).deduplicate(
        [_validated(0, close="50100", row=1), _validated(0, close="50177", row=2)]
    )
    assert len(kept) == 1
    assert kept[0].close == Decimal("50100")
    findings = recorder.findings  # type: ignore[attr-defined]
    assert findings[0].severity is FindingSeverity.ERROR
    assert findings[0].context["first_seen_row"] == "1"


def test_missing_interval_is_reported_at_the_configured_severity() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder, gap_severity=FindingSeverity.ERROR).check_gaps(
        [_validated(0), _validated(2)]
    )
    findings = recorder.findings  # type: ignore[attr-defined]
    assert findings[0].code is DataQualityIssue.MISSING_BAR
    assert findings[0].severity is FindingSeverity.ERROR
    assert findings[0].context["count"] == "1"


def test_gaps_beyond_the_threshold_escalate_to_fatal() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder, max_allowed_gap_bars=1).check_gaps([_validated(0), _validated(5)])
    findings = recorder.findings  # type: ignore[attr-defined]
    assert any(finding.severity is FindingSeverity.FATAL for finding in findings)


def test_gaps_within_the_threshold_do_not_escalate() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder, max_allowed_gap_bars=10).check_gaps([_validated(0), _validated(5)])
    assert not any(
        finding.severity is FindingSeverity.FATAL
        for finding in recorder.findings  # type: ignore[attr-defined]
    )


def test_stale_dataset_is_reported() -> None:
    recorder = make_recorder()
    _dataset_validator(
        recorder,
        staleness_budget=timedelta(hours=1),
        reference_time=datetime(2026, 1, 1, 12, tzinfo=UTC),
    ).check_staleness([_validated(0)])
    assert any(
        finding.code is DataQualityIssue.STALE_DATA
        for finding in recorder.findings  # type: ignore[attr-defined]
    )


def test_historical_dataset_judged_against_its_own_end_is_not_stale() -> None:
    # The data ends at 01:00; judged against a historical reference just after it, a
    # deliberately old backfill must not be reported as stale.
    recorder = make_recorder()
    _dataset_validator(
        recorder,
        staleness_budget=timedelta(hours=2),
        reference_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
    ).check_staleness([_validated(0)])
    assert recorder.findings == ()  # type: ignore[attr-defined]


def test_empty_dataset_after_validation_is_fatal() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder).check_non_empty([], source_rows=5)
    findings = recorder.findings  # type: ignore[attr-defined]
    assert findings[0].code is DataQualityIssue.EMPTY_DATASET
    assert findings[0].severity is FindingSeverity.FATAL
    assert "all 5 source rows were rejected" in findings[0].message


def test_source_with_no_rows_is_fatal() -> None:
    recorder = make_recorder()
    _dataset_validator(recorder).check_non_empty([], source_rows=0)
    assert "no data rows" in recorder.findings[0].message  # type: ignore[attr-defined]


# --- Normalisation --------------------------------------------------------------------------


def test_normalised_bar_is_always_closed() -> None:
    bar = normalize_record(_validated(0), source="test")
    assert bar.is_closed is True
    assert bar.source == "test"


def test_normalisation_preserves_decimal_values_exactly() -> None:
    record = ValidatedRecord(
        source_row=1,
        symbol=SYMBOL,
        market_type=EXPECTATIONS.market_type,
        timeframe=TIMEFRAME,
        open_time=datetime(2026, 1, 1, tzinfo=UTC),
        close_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
        open=Decimal("50000.123456789012345678"),
        high=Decimal("60000"),
        low=Decimal("40000"),
        close=Decimal("50100.87654321"),
        volume=Decimal("12.000000000000000001"),
        trade_count=None,
    )
    bar = normalize_record(record, source="test")
    assert bar.open == Decimal("50000.123456789012345678")
    assert bar.volume == Decimal("12.000000000000000001")
    assert not isinstance(bar.open, float)


def _normalize_fixture(
    name: str,
    *,
    now: datetime | None = None,
    reference_time: datetime | None = None,
    **dataset_kwargs: object,
) -> tuple[object, object]:
    """Run the full normalisation pipeline over a fixture and return result and recorder."""
    clock = make_clock(now) if now is not None else make_clock()
    recorder = make_recorder(clock=clock)
    source = load_csv_records(fixture(name), source_id="test")
    result = normalize_source(
        source.records,
        record_validator=RecordValidator(
            expectations=EXPECTATIONS,
            closed_candle_policy=make_policy(clock=clock, reference_time=reference_time),
            recorder=recorder,
        ),
        dataset_validator=_dataset_validator(
            recorder,
            reference_time=reference_time or datetime(2026, 1, 1, 5, tzinfo=UTC),
            **dataset_kwargs,  # type: ignore[arg-type]
        ),
        recorder=recorder,
        source="test",
    )
    return result, recorder


def test_valid_fixture_normalises_into_ordered_bars() -> None:
    result, _ = _normalize_fixture("valid.csv")
    bars = result.bars  # type: ignore[attr-defined]
    assert len(bars) == 4
    assert [bar.open_time.hour for bar in bars] == [0, 1, 2, 3]
    assert all(bar.is_closed for bar in bars)


def test_out_of_order_fixture_is_reported_then_sorted_deterministically() -> None:
    result, recorder = _normalize_fixture("out_of_order.csv")
    assert any(
        finding.code is DataQualityIssue.OUT_OF_ORDER_BAR
        for finding in recorder.findings  # type: ignore[attr-defined]
    )
    assert [bar.open_time.hour for bar in result.bars] == [0, 1]  # type: ignore[attr-defined]


def test_high_precision_fixture_survives_normalisation() -> None:
    result, _ = _normalize_fixture("high_precision.csv")
    bar = result.bars[0]  # type: ignore[attr-defined]
    assert bar.open == Decimal("50000.123456789012345678")
    assert bar.volume == Decimal("12.000000000000000001")


def test_offset_timestamp_fixture_normalises_to_utc() -> None:
    result, _ = _normalize_fixture("offset_timestamp.csv")
    bar = result.bars[0]  # type: ignore[attr-defined]
    assert bar.open_time == datetime(2026, 1, 1, tzinfo=UTC)


def test_unclosed_final_candle_is_dropped_but_the_closed_one_is_kept() -> None:
    result, recorder = _normalize_fixture(
        "unclosed_final.csv",
        now=datetime(2026, 1, 1, 4, 30, tzinfo=UTC),
        reference_time=datetime(2026, 1, 1, 4, 30, tzinfo=UTC),
    )
    bars = result.bars  # type: ignore[attr-defined]
    assert len(bars) == 1
    assert bars[0].open_time.hour == 3
    assert any(
        finding.code is DataQualityIssue.OPEN_CANDLE
        for finding in recorder.findings  # type: ignore[attr-defined]
    )


def test_mixed_symbol_fixture_rejects_the_foreign_row() -> None:
    result, _ = _normalize_fixture("mixed_symbols.csv")
    assert result.valid_rows == 1  # type: ignore[attr-defined]
    assert result.rejected_rows == 1  # type: ignore[attr-defined]


def test_malformed_numeric_fixture_rejects_both_rows() -> None:
    result, _ = _normalize_fixture("malformed_numeric.csv")
    assert result.rejected_rows == 2  # type: ignore[attr-defined]
    assert result.bars == ()  # type: ignore[attr-defined]


def test_no_bar_is_ever_manufactured_to_fill_a_gap() -> None:
    result, recorder = _normalize_fixture("missing_interval.csv")
    bars = result.bars  # type: ignore[attr-defined]
    assert [bar.open_time.hour for bar in bars] == [0, 3]
    assert any(
        finding.code is DataQualityIssue.MISSING_BAR
        for finding in recorder.findings  # type: ignore[attr-defined]
    )


@given(hours=st.lists(st.integers(min_value=0, max_value=23), min_size=1, unique=True))
def test_normalised_bars_are_always_sorted_and_unique(hours: list[int]) -> None:
    recorder = make_recorder()
    records = [_validated(hour, row=index) for index, hour in enumerate(hours, start=1)]
    kept = _dataset_validator(recorder).deduplicate(records)
    ordered = sorted(kept, key=lambda record: record.open_time)
    bars = [normalize_record(record, source="test") for record in ordered]
    open_times = [bar.open_time for bar in bars]
    assert open_times == sorted(open_times)
    assert len(set(open_times)) == len(open_times)
