"""Persisted data-ingestion domain models.

These models live in ``core`` rather than in ``data`` because both the data layer (which
produces them) and the storage layer (whose repositories persist and return them) need to
share a single definition, and storage is not permitted to depend on data. Keeping them
here preserves the rule that repository methods return domain models, never ORM entities.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from quantplatform.core.enums import (
    BarWriteOutcome,
    DataQualityIssue,
    FindingSeverity,
    IngestionStatus,
    MarketType,
    Timeframe,
)
from quantplatform.core.models.base import (
    DomainModel,
    Symbol,
    Text,
    UtcDatetime,
    VenueId,
)
from quantplatform.core.models.market import MarketBar

__all__ = ["BarWriteResult", "DataQualityFinding", "IngestionRun"]


class DataQualityFinding(DomainModel):
    """A single integrity or quality observation raised while processing market data.

    Every finding is recorded, including ones that do not block anything, so an ingestion
    run can be fully explained after the fact from persisted findings alone.
    """

    finding_id: UUID
    ingestion_run_id: UUID
    code: DataQualityIssue
    severity: FindingSeverity
    message: Text
    source: Text
    source_row: int | None = Field(default=None, ge=0)
    symbol: Symbol | None = None
    timeframe: Timeframe | None = None
    open_time: UtcDatetime | None = None
    context: dict[str, str] = Field(default_factory=dict)
    detected_at: UtcDatetime

    @property
    def blocks_record(self) -> bool:
        """Return whether this finding causes its record to be rejected."""
        return self.severity.blocks_record

    @property
    def blocks_ingestion(self) -> bool:
        """Return whether this finding fails the entire ingestion run."""
        return self.severity.blocks_ingestion


class IngestionRun(DomainModel):
    """Provenance and outcome of a single market-data ingestion attempt.

    A run record is written exactly once, after the attempt has concluded (successfully or
    not), which is why every field describing its outcome is required rather than optional:
    there is no persisted "in progress" state. This keeps :class:`IngestionStatus` limited
    to its three original terminal values instead of requiring a fourth "running" member.
    """

    run_id: UUID
    source_id: VenueId
    source_path: Text
    source_checksum: Text
    expected_symbol: Symbol
    expected_market_type: MarketType
    expected_timeframe: Timeframe
    started_at: UtcDatetime
    completed_at: UtcDatetime
    status: IngestionStatus
    total_source_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    inserted_bars: int = Field(ge=0)
    exact_duplicate_bars: int = Field(ge=0)
    conflicting_duplicate_bars: int = Field(ge=0)
    info_finding_count: int = Field(ge=0)
    warning_finding_count: int = Field(ge=0)
    error_finding_count: int = Field(ge=0)
    fatal_finding_count: int = Field(ge=0)
    configuration_hash: Text
    application_version: Text | None = None
    error_summary: Text | None = None

    @model_validator(mode="after")
    def _validate_run(self) -> Self:
        """Check timestamp ordering, row accounting and the failure/summary pairing."""
        if self.completed_at < self.started_at:
            msg = "completed_at must not precede started_at"
            raise ValueError(msg)
        if self.valid_rows + self.rejected_rows > self.total_source_rows:
            msg = "valid_rows plus rejected_rows must not exceed total_source_rows"
            raise ValueError(msg)
        if self.status is IngestionStatus.FAILED and self.error_summary is None:
            msg = "a failed run requires an error_summary"
            raise ValueError(msg)
        if self.status is not IngestionStatus.FAILED and self.error_summary is not None:
            msg = "only a failed run may carry an error_summary"
            raise ValueError(msg)
        if self.status is IngestionStatus.FAILED and self.inserted_bars != 0:
            msg = "a failed run must not report inserted bars"
            raise ValueError(msg)
        return self

    @property
    def total_finding_count(self) -> int:
        """Return the total number of findings recorded across every severity."""
        return (
            self.info_finding_count
            + self.warning_finding_count
            + self.error_finding_count
            + self.fatal_finding_count
        )


class BarWriteResult(DomainModel):
    """Outcome of attempting to persist one normalised bar.

    Returned by the bar repository for every bar it is asked to add, so the caller can
    build precise findings (for example a ``REVISED_BAR`` finding naming the previously
    stored values) without the repository leaking ORM state to do so.
    """

    bar: MarketBar
    outcome: BarWriteOutcome
    existing_bar: MarketBar | None = None

    @model_validator(mode="after")
    def _validate_existing_bar(self) -> Self:
        """Require the stored bar exactly when the outcome is a conflict."""
        if self.outcome is BarWriteOutcome.CONFLICTING and self.existing_bar is None:
            msg = "a conflicting outcome requires the existing stored bar"
            raise ValueError(msg)
        if self.outcome is not BarWriteOutcome.CONFLICTING and self.existing_bar is not None:
            msg = "only a conflicting outcome may carry the existing stored bar"
            raise ValueError(msg)
        return self
