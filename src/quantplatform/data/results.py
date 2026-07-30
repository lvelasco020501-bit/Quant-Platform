"""Result types produced along the ingestion pipeline.

These are internal data-layer structures rather than persisted domain models, so they are
frozen dataclasses instead of pydantic models. The persisted, auditable outputs are
:class:`~quantplatform.core.models.data.IngestionRun` and
:class:`~quantplatform.core.models.data.DataQualityFinding`, which these types carry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.data import DataQualityFinding, IngestionRun
from quantplatform.core.models.market import MarketBar
from quantplatform.data.records import RawBarRecord

__all__ = [
    "IngestionResult",
    "NormalizationResult",
    "RejectedRecord",
    "ValidatedRecord",
]


@dataclass(frozen=True, slots=True)
class ValidatedRecord:
    """A raw record whose every field has been parsed and checked.

    Reaching this type means the record is individually sound: types parsed, timestamps
    timezone-aware and UTC, OHLC relationships consistent, and the candle closed. It does
    not mean the record survives dataset-level checks, which run afterwards.
    """

    source_row: int
    symbol: str
    market_type: MarketType
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    """A raw record that failed validation, kept alongside the findings that rejected it."""

    record: RawBarRecord
    findings: tuple[DataQualityFinding, ...]


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Outcome of turning a parsed source file into normalised bars.

    Bars are ordered by ascending open time regardless of the order the source supplied
    them in; out-of-order input is detected and reported before that sorting happens, never
    silently repaired.
    """

    bars: tuple[MarketBar, ...]
    rejected: tuple[RejectedRecord, ...]
    findings: tuple[DataQualityFinding, ...]
    total_source_rows: int

    @property
    def valid_rows(self) -> int:
        """Return how many source rows produced a normalised bar."""
        return len(self.bars)

    @property
    def rejected_rows(self) -> int:
        """Return how many source rows were rejected."""
        return len(self.rejected)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """What an ingestion attempt produced, returned to the caller.

    ``run`` is the same record persisted to ``ingestion_runs``, so a caller and an auditor
    reading the database see identical numbers.
    """

    run: IngestionRun
    findings: tuple[DataQualityFinding, ...]
    bars_written: tuple[MarketBar, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        """Return whether the run persisted its data."""
        return self.run.status.is_successful
