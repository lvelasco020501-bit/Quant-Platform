"""Turning validated records into normalised domain bars.

By the time a record reaches this module it has already been proven sound, so normalisation
is assembly rather than repair. Nothing here fills a missing value, interpolates a price,
sorts away an ordering problem before it has been reported, or corrects an inconsistent
candle: those would all destroy the very signal the validation layer exists to surface.

The pipeline this module runs is deliberately ordered so that observation always precedes
transformation — heterogeneity, then per-record validation, then out-of-order detection,
then deduplication, then sorting, then gap and staleness analysis over the final series.
"""

from __future__ import annotations

from collections.abc import Sequence

from quantplatform.core.models.market import MarketBar
from quantplatform.data.findings import FindingRecorder
from quantplatform.data.records import RawBarRecord
from quantplatform.data.results import (
    NormalizationResult,
    RejectedRecord,
    ValidatedRecord,
)
from quantplatform.data.validation import DatasetValidator, RecordValidator

__all__ = ["normalize_record", "normalize_source"]


def normalize_record(record: ValidatedRecord, *, source: str) -> MarketBar:
    """Assemble a validated record into a :class:`~quantplatform.core.models.market.MarketBar`.

    ``is_closed`` is unconditionally ``True``: an unclosed candle is rejected during
    validation and can never reach this function, so every persisted bar is by construction
    a finalised one.

    Args:
        record: A record that has passed record-level validation.
        source: Logical source identifier stamped onto the bar.

    Returns:
        The normalised bar.
    """
    return MarketBar(
        symbol=record.symbol,
        market_type=record.market_type,
        timeframe=record.timeframe,
        open_time=record.open_time,
        close_time=record.close_time,
        open=record.open,
        high=record.high,
        low=record.low,
        close=record.close,
        volume=record.volume,
        quote_volume=None,
        trade_count=record.trade_count,
        source=source,
        is_closed=True,
    )


def normalize_source(
    records: Sequence[RawBarRecord],
    *,
    record_validator: RecordValidator,
    dataset_validator: DatasetValidator,
    recorder: FindingRecorder,
    source: str,
) -> NormalizationResult:
    """Run the full validation and normalisation pipeline over one source's rows.

    Args:
        records: Raw rows in the order the source supplied them.
        record_validator: Applies record-level checks.
        dataset_validator: Applies whole-dataset checks.
        recorder: The recorder both validators write findings to; snapshotted into the
            result once the pipeline has finished.
        source: Logical source identifier stamped onto every bar.

    Returns:
        The normalised bars, the rejected records, every finding raised, and the source row
        count.
    """
    dataset_validator.inspect_source_heterogeneity(records)

    validated: list[ValidatedRecord] = []
    rejected: list[RejectedRecord] = []
    for record in records:
        outcome = record_validator.validate(record)
        if isinstance(outcome, ValidatedRecord):
            validated.append(outcome)
        else:
            rejected.append(outcome)

    # Ordering is inspected on the source's own sequence, before anything is reordered.
    dataset_validator.check_ordering(validated)
    deduplicated = dataset_validator.deduplicate(validated)
    ordered = tuple(sorted(deduplicated, key=lambda record: record.open_time))

    dataset_validator.check_gaps(ordered)
    dataset_validator.check_staleness(ordered)
    dataset_validator.check_non_empty(ordered, source_rows=len(records))

    bars = tuple(normalize_record(record, source=source) for record in ordered)
    return NormalizationResult(
        bars=bars,
        rejected=tuple(rejected),
        findings=recorder.findings,
        total_source_rows=len(records),
    )
