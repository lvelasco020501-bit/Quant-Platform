"""Validation of raw market-data records, at record and dataset level.

Validation is where a raw string row either earns the right to become a
:class:`~quantplatform.core.models.market.MarketBar` or is rejected with findings that
still quote its original text and row number. Nothing is repaired: a bad value is never
coerced, a missing bar is never synthesised, and an inconsistent candle is never adjusted.

Two properties are enforced here rather than by a dedicated later check, because they make
overlapping candle windows unrepresentable rather than merely detectable: every open time
must sit exactly on the timeframe grid, and every candle's duration must equal its
timeframe. Together with duplicate detection, those guarantee that consecutive distinct
bars are spaced by exactly one interval, so no two accepted candles can overlap.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from quantplatform.core.enums import (
    DataQualityIssue,
    FindingSeverity,
    MarketType,
    Timeframe,
)
from quantplatform.core.timeutils import bar_close_time, ensure_utc, is_on_timeframe_grid
from quantplatform.data.closed_candle import ClosedCandlePolicy
from quantplatform.data.findings import FindingRecorder
from quantplatform.data.records import RawBarRecord
from quantplatform.data.results import RejectedRecord, ValidatedRecord
from quantplatform.data.timeframes import missing_open_times

__all__ = ["DatasetExpectations", "DatasetValidator", "RecordValidator"]

_REQUIRED_NON_EMPTY_FIELDS = (
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
)


@dataclass(frozen=True, slots=True)
class DatasetExpectations:
    """What a single ingestion run expects every record in its source to be."""

    symbol: str
    market_type: MarketType
    timeframe: Timeframe


@dataclass(frozen=True, slots=True)
class _ParsedFields:
    """Successfully parsed field values of one record."""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None


def _parse_timestamp(text: str) -> datetime:
    """Parse an ISO-8601 timestamp that must carry an explicit timezone offset.

    A naive timestamp is refused rather than assumed to be UTC: silently guessing a
    timezone is exactly the class of error that produces bars misaligned by whole hours.

    Args:
        text: Candidate ISO-8601 timestamp.

    Returns:
        The instant normalised to UTC.

    Raises:
        ValueError: If the text is unparseable or carries no timezone offset.
    """
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "timestamp has no timezone offset"
        raise ValueError(msg)
    return ensure_utc(parsed)


def _parse_decimal(text: str) -> Decimal:
    """Parse an exact decimal, rejecting NaN and infinity.

    ``Decimal("NaN")`` and ``Decimal("Infinity")`` parse without error but are meaningless
    as prices and would poison every downstream comparison, so they are refused here.

    Raises:
        ValueError: If the text is not a finite decimal.
    """
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        msg = f"not a valid decimal: {text!r}"
        raise ValueError(msg) from exc
    if not value.is_finite():
        msg = f"decimal must be finite, got {text!r}"
        raise ValueError(msg)
    return value


class RecordValidator:
    """Validates one raw record at a time, independently of its neighbours.

    Args:
        expectations: The symbol, market type and timeframe the run is ingesting.
        closed_candle_policy: Decides whether a candle has finalised.
        recorder: Collects the findings produced while validating.
    """

    def __init__(
        self,
        *,
        expectations: DatasetExpectations,
        closed_candle_policy: ClosedCandlePolicy,
        recorder: FindingRecorder,
    ) -> None:
        self._expectations = expectations
        self._policy = closed_candle_policy
        self._recorder = recorder

    def validate(self, record: RawBarRecord) -> ValidatedRecord | RejectedRecord:
        """Validate one record.

        Every problem with the record is reported, not just the first, so one pass over a
        bad file tells the operator everything wrong with it.

        Args:
            record: The raw row to validate.

        Returns:
            A :class:`~quantplatform.data.results.ValidatedRecord` when the record is
            sound, otherwise a :class:`~quantplatform.data.results.RejectedRecord` carrying
            the findings that rejected it.
        """
        before = len(self._recorder.findings)

        self._check_required_fields(record)
        self._check_identity(record)
        parsed = self._parse(record)
        if parsed is not None:
            self._check_semantics(record, parsed)

        new_findings = self._recorder.findings[before:]
        blocking = tuple(finding for finding in new_findings if finding.blocks_record)
        if blocking or parsed is None:
            return RejectedRecord(record=record, findings=new_findings)

        return ValidatedRecord(
            source_row=record.source_row,
            symbol=self._expectations.symbol,
            market_type=self._expectations.market_type,
            timeframe=self._expectations.timeframe,
            open_time=parsed.open_time,
            close_time=parsed.close_time,
            open=parsed.open,
            high=parsed.high,
            low=parsed.low,
            close=parsed.close,
            volume=parsed.volume,
            trade_count=parsed.trade_count,
        )

    def _check_required_fields(self, record: RawBarRecord) -> None:
        """Report any required field that is absent or blank."""
        for name in _REQUIRED_NON_EMPTY_FIELDS:
            if not getattr(record, name):
                self._recorder.record(
                    DataQualityIssue.MALFORMED_RECORD,
                    FindingSeverity.ERROR,
                    f"required field {name!r} is missing or empty",
                    source_row=record.source_row,
                    context={"field": name},
                )

    def _check_identity(self, record: RawBarRecord) -> None:
        """Report a record that describes a different instrument than the run expects."""
        expected = self._expectations
        if record.symbol and record.symbol != expected.symbol:
            self._recorder.record(
                DataQualityIssue.UNEXPECTED_SYMBOL,
                FindingSeverity.ERROR,
                f"record symbol {record.symbol!r} does not match the expected {expected.symbol!r}",
                source_row=record.source_row,
                context={"found": record.symbol, "expected": expected.symbol},
            )
        if record.market_type and record.market_type != expected.market_type.value:
            self._recorder.record(
                DataQualityIssue.UNEXPECTED_MARKET_TYPE,
                FindingSeverity.ERROR,
                f"record market type {record.market_type!r} does not match the expected "
                f"{expected.market_type.value!r}",
                source_row=record.source_row,
                context={"found": record.market_type, "expected": expected.market_type.value},
            )
        if record.timeframe and record.timeframe != expected.timeframe.value:
            self._recorder.record(
                DataQualityIssue.UNEXPECTED_TIMEFRAME,
                FindingSeverity.ERROR,
                f"record timeframe {record.timeframe!r} does not match the expected "
                f"{expected.timeframe.value!r}",
                source_row=record.source_row,
                context={"found": record.timeframe, "expected": expected.timeframe.value},
            )

    def _parse(self, record: RawBarRecord) -> _ParsedFields | None:
        """Parse every typed field, reporting each failure; ``None`` if any failed."""
        failed = False

        timestamps: dict[str, datetime] = {}
        for name in ("open_time", "close_time"):
            raw = getattr(record, name)
            try:
                timestamps[name] = _parse_timestamp(raw)
            except ValueError as exc:
                failed = True
                self._recorder.record(
                    DataQualityIssue.MALFORMED_RECORD,
                    FindingSeverity.ERROR,
                    f"field {name!r} is not an ISO-8601 timestamp with an explicit "
                    f"timezone offset: {exc}",
                    source_row=record.source_row,
                    context={"field": name, "value": raw},
                )

        numbers: dict[str, Decimal] = {}
        for name in ("open", "high", "low", "close", "volume"):
            raw = getattr(record, name)
            try:
                numbers[name] = _parse_decimal(raw)
            except ValueError as exc:
                failed = True
                self._recorder.record(
                    DataQualityIssue.MALFORMED_RECORD,
                    FindingSeverity.ERROR,
                    f"field {name!r} is not a finite decimal: {exc}",
                    source_row=record.source_row,
                    context={"field": name, "value": raw},
                )

        trade_count: int | None = None
        if record.trade_count:
            try:
                trade_count = int(record.trade_count)
            except ValueError:
                failed = True
                self._recorder.record(
                    DataQualityIssue.MALFORMED_RECORD,
                    FindingSeverity.ERROR,
                    f"field 'trade_count' is not an integer: {record.trade_count!r}",
                    source_row=record.source_row,
                    context={"field": "trade_count", "value": record.trade_count},
                )

        if failed:
            return None
        return _ParsedFields(
            open_time=timestamps["open_time"],
            close_time=timestamps["close_time"],
            open=numbers["open"],
            high=numbers["high"],
            low=numbers["low"],
            close=numbers["close"],
            volume=numbers["volume"],
            trade_count=trade_count,
        )

    def _check_semantics(self, record: RawBarRecord, parsed: _ParsedFields) -> None:
        """Report violations of the value rules a sound candle must satisfy."""
        self._check_timing(record, parsed)
        self._check_prices(record, parsed)
        self._check_quantities(record, parsed)
        self._check_closed(record, parsed)

    def _check_timing(self, record: RawBarRecord, parsed: _ParsedFields) -> None:
        """Report open/close ordering, grid alignment and duration problems."""
        timeframe = self._expectations.timeframe
        if parsed.open_time >= parsed.close_time:
            self._recorder.record(
                DataQualityIssue.MALFORMED_RECORD,
                FindingSeverity.ERROR,
                "open_time must be strictly before close_time",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={
                    "open_time": parsed.open_time.isoformat(),
                    "close_time": parsed.close_time.isoformat(),
                },
            )
            return

        if not is_on_timeframe_grid(parsed.open_time, timeframe):
            self._recorder.record(
                DataQualityIssue.MALFORMED_RECORD,
                FindingSeverity.ERROR,
                f"open_time is not aligned to the {timeframe.value} grid",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={
                    "open_time": parsed.open_time.isoformat(),
                    "timeframe": timeframe.value,
                },
            )

        expected_close = bar_close_time(parsed.open_time, timeframe)
        if parsed.close_time != expected_close:
            self._recorder.record(
                DataQualityIssue.UNEXPECTED_TIMEFRAME,
                FindingSeverity.ERROR,
                f"candle duration does not match {timeframe.value}: expected close "
                f"{expected_close.isoformat()}",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={
                    "expected_close_time": expected_close.isoformat(),
                    "found_close_time": parsed.close_time.isoformat(),
                    "timeframe": timeframe.value,
                },
            )

    def _check_prices(self, record: RawBarRecord, parsed: _ParsedFields) -> None:
        """Report non-positive prices and inconsistent OHLC relationships."""
        prices = {
            "open": parsed.open,
            "high": parsed.high,
            "low": parsed.low,
            "close": parsed.close,
        }
        non_positive = sorted(name for name, value in prices.items() if value <= 0)
        if non_positive:
            self._recorder.record(
                DataQualityIssue.INVALID_OHLC,
                FindingSeverity.ERROR,
                f"price fields must be strictly positive: {', '.join(non_positive)}",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={name: str(prices[name]) for name in non_positive},
            )
            return

        highest = max(parsed.open, parsed.close, parsed.low)
        lowest = min(parsed.open, parsed.close, parsed.high)
        if parsed.high < highest:
            self._recorder.record(
                DataQualityIssue.INVALID_OHLC,
                FindingSeverity.ERROR,
                "high must be at least the maximum of open, close and low",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={name: str(value) for name, value in prices.items()},
            )
        if parsed.low > lowest:
            self._recorder.record(
                DataQualityIssue.INVALID_OHLC,
                FindingSeverity.ERROR,
                "low must be at most the minimum of open, close and high",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={name: str(value) for name, value in prices.items()},
            )

    def _check_quantities(self, record: RawBarRecord, parsed: _ParsedFields) -> None:
        """Report negative volume or trade count."""
        if parsed.volume < 0:
            self._recorder.record(
                DataQualityIssue.NEGATIVE_VOLUME,
                FindingSeverity.ERROR,
                "volume must not be negative",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={"volume": str(parsed.volume)},
            )
        if parsed.trade_count is not None and parsed.trade_count < 0:
            self._recorder.record(
                DataQualityIssue.MALFORMED_RECORD,
                FindingSeverity.ERROR,
                "trade_count must not be negative",
                source_row=record.source_row,
                open_time=parsed.open_time,
                context={"trade_count": str(parsed.trade_count)},
            )

    def _check_closed(self, record: RawBarRecord, parsed: _ParsedFields) -> None:
        """Reject a candle that has not finalised under the closed-candle policy."""
        if self._policy.is_closed(parsed.close_time):
            return
        self._recorder.record(
            DataQualityIssue.OPEN_CANDLE,
            FindingSeverity.ERROR,
            "candle has not closed yet and must not be persisted",
            source_row=record.source_row,
            open_time=parsed.open_time,
            context={
                "close_time": parsed.close_time.isoformat(),
                "finalises_at": self._policy.finalises_at(parsed.close_time).isoformat(),
                "evaluated_at": self._policy.reference_now().isoformat(),
            },
        )


class DatasetValidator:
    """Validates a set of records as a whole, after each has passed record-level checks.

    Args:
        expectations: The symbol, market type and timeframe the run is ingesting.
        recorder: Collects the findings produced while validating.
        gap_severity: Severity recorded for each contiguous run of missing bars.
        max_allowed_gap_bars: Total missing bars beyond which the dataset is unusable.
        staleness_budget: Maximum permitted age of the newest candle.
        reference_time: Instant that age is measured against.
    """

    def __init__(
        self,
        *,
        expectations: DatasetExpectations,
        recorder: FindingRecorder,
        gap_severity: FindingSeverity,
        max_allowed_gap_bars: int,
        staleness_budget: timedelta,
        reference_time: datetime,
    ) -> None:
        self._expectations = expectations
        self._recorder = recorder
        self._gap_severity = gap_severity
        self._max_allowed_gap_bars = max_allowed_gap_bars
        self._staleness_budget = staleness_budget
        self._reference_time = reference_time

    def inspect_source_heterogeneity(self, records: Sequence[RawBarRecord]) -> None:
        """Report a source that mixes instruments, markets or timeframes.

        Each offending record is also rejected individually by
        :class:`RecordValidator`; this check exists because "the file contains two symbols"
        is a different, more actionable statement than "row 7 has the wrong symbol", and
        only a whole-dataset view can make it.
        """
        for field_name, expected, code in (
            ("symbol", self._expectations.symbol, DataQualityIssue.UNEXPECTED_SYMBOL),
            (
                "market_type",
                self._expectations.market_type.value,
                DataQualityIssue.UNEXPECTED_MARKET_TYPE,
            ),
            (
                "timeframe",
                self._expectations.timeframe.value,
                DataQualityIssue.UNEXPECTED_TIMEFRAME,
            ),
        ):
            distinct = sorted(
                {getattr(record, field_name) for record in records if getattr(record, field_name)}
            )
            if len(distinct) > 1:
                self._recorder.record(
                    code,
                    FindingSeverity.ERROR,
                    f"source mixes {len(distinct)} distinct {field_name} values in a "
                    f"single-{field_name} ingestion",
                    context={"found": ", ".join(distinct), "expected": expected},
                )

    def deduplicate(self, records: Sequence[ValidatedRecord]) -> tuple[ValidatedRecord, ...]:
        """Collapse records that share an open time, preserving the first occurrence.

        An exact repeat is informational and idempotent. A repeat carrying different values
        is a conflict: the first occurrence is kept and the later one is rejected, because
        the pipeline has no basis for deciding which version is correct and must not guess.
        """
        kept: dict[datetime, ValidatedRecord] = {}
        for record in records:
            existing = kept.get(record.open_time)
            if existing is None:
                kept[record.open_time] = record
                continue
            if _same_values(existing, record):
                self._recorder.record(
                    DataQualityIssue.DUPLICATE_BAR,
                    FindingSeverity.INFO,
                    f"row {record.source_row} exactly repeats row {existing.source_row}; "
                    f"ignored as idempotent",
                    source_row=record.source_row,
                    open_time=record.open_time,
                    context={"first_seen_row": str(existing.source_row)},
                )
            else:
                self._recorder.record(
                    DataQualityIssue.DUPLICATE_BAR,
                    FindingSeverity.ERROR,
                    f"row {record.source_row} repeats the open time of row "
                    f"{existing.source_row} with different values; the first was kept",
                    source_row=record.source_row,
                    open_time=record.open_time,
                    context={
                        "first_seen_row": str(existing.source_row),
                        "kept_close": str(existing.close),
                        "rejected_close": str(record.close),
                    },
                )
        return tuple(kept.values())

    def check_ordering(self, records: Sequence[ValidatedRecord]) -> None:
        """Report records that arrive out of chronological order.

        Called before any sorting, so the finding reflects what the source actually
        contained rather than what the pipeline made of it.
        """
        for previous, current in itertools.pairwise(records):
            if current.open_time < previous.open_time:
                self._recorder.record(
                    DataQualityIssue.OUT_OF_ORDER_BAR,
                    FindingSeverity.WARNING,
                    f"row {current.source_row} opens before the preceding row "
                    f"{previous.source_row}",
                    source_row=current.source_row,
                    open_time=current.open_time,
                    context={
                        "previous_open_time": previous.open_time.isoformat(),
                        "open_time": current.open_time.isoformat(),
                    },
                )

    def check_gaps(self, records: Sequence[ValidatedRecord]) -> None:
        """Report missing intervals, escalating to fatal past the configured threshold."""
        runs = missing_open_times(
            [record.open_time for record in records], self._expectations.timeframe
        )
        if not runs:
            return

        for run in runs:
            self._recorder.record(
                DataQualityIssue.MISSING_BAR,
                self._gap_severity,
                f"{run.count} missing {self._expectations.timeframe.value} bar(s) between "
                f"{run.first_missing_open_time.isoformat()} and "
                f"{run.last_missing_open_time.isoformat()}",
                open_time=run.first_missing_open_time,
                context={
                    "first_missing_open_time": run.first_missing_open_time.isoformat(),
                    "last_missing_open_time": run.last_missing_open_time.isoformat(),
                    "count": str(run.count),
                },
            )

        total_missing = sum(run.count for run in runs)
        if total_missing > self._max_allowed_gap_bars:
            self._recorder.record(
                DataQualityIssue.MISSING_BAR,
                FindingSeverity.FATAL,
                f"dataset is missing {total_missing} bars, above the maximum of "
                f"{self._max_allowed_gap_bars}; it is too incomplete to ingest",
                context={
                    "missing_bars": str(total_missing),
                    "max_allowed_gap_bars": str(self._max_allowed_gap_bars),
                },
            )

    def check_staleness(self, records: Sequence[ValidatedRecord]) -> None:
        """Report a dataset whose newest candle is older than the freshness budget.

        Age is measured against the reference time the run was given. For a historical
        backfill that reference is the supplied end boundary, so a deliberately old dataset
        is not faulted merely for being old.
        """
        if not records:
            return
        newest = max(record.close_time for record in records)
        age = self._reference_time - newest
        if age > self._staleness_budget:
            self._recorder.record(
                DataQualityIssue.STALE_DATA,
                FindingSeverity.WARNING,
                f"newest candle closed {age} before the reference time, exceeding the "
                f"freshness budget of {self._staleness_budget}",
                open_time=max(record.open_time for record in records),
                context={
                    "newest_close_time": newest.isoformat(),
                    "reference_time": self._reference_time.isoformat(),
                    "age": str(age),
                    "budget": str(self._staleness_budget),
                },
            )

    def check_non_empty(self, records: Sequence[ValidatedRecord], *, source_rows: int) -> None:
        """Report a dataset that has nothing left to ingest."""
        if records:
            return
        message = (
            "source contained no data rows"
            if source_rows == 0
            else f"all {source_rows} source rows were rejected; nothing is left to ingest"
        )
        self._recorder.record(
            DataQualityIssue.EMPTY_DATASET,
            FindingSeverity.FATAL,
            message,
            context={"source_rows": str(source_rows)},
        )


def _same_values(left: ValidatedRecord, right: ValidatedRecord) -> bool:
    """Return whether two records carry identical OHLCV and trade count."""
    return (
        left.open == right.open
        and left.high == right.high
        and left.low == right.low
        and left.close == right.close
        and left.volume == right.volume
        and left.trade_count == right.trade_count
    )
