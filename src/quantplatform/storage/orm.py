"""SQLAlchemy 2.x declarative mappings for the Phase 2 data tables.

Three tables back the data layer: ``market_bars`` holds normalised OHLCV data,
``ingestion_runs`` holds provenance for each ingestion attempt, and
``data_quality_findings`` holds every observation raised while producing a run, linked back
to it by foreign key. Nothing here is imported outside :mod:`quantplatform.storage`: ORM
rows never leave this package, since :mod:`quantplatform.core.interfaces` repository
protocols return domain models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import UniqueConstraint

from quantplatform.core.enums import (
    DataQualityIssue,
    FindingSeverity,
    IngestionStatus,
    MarketType,
    Timeframe,
)
from quantplatform.storage.types import ExactNumeric

__all__ = ["Base", "DataQualityFindingRow", "IngestionRunRow", "MarketBarRow"]

_SYMBOL_LENGTH = 41
"""Two 2-20 character asset codes joined by ``/``: at most 20 + 1 + 20 characters."""

_TEXT_LENGTH = 500
"""Matches the shared ``Text`` domain alias used across every free-text domain field."""

_PRICE_PRECISION = 38
_PRICE_SCALE = 18
"""Ample headroom for both satoshi-level BTC quantities and low-value altcoin prices."""

_CHECKSUM_LENGTH = 64
"""Exact length of a SHA-256 hex digest."""


def _enum_column(enum_cls: type, *, length: int, name: str) -> SqlEnum:
    """Build a portable enum column that stores the stable ``.value``, not the member name.

    Args:
        enum_cls: The ``StrEnum`` to map.
        length: Column length, sized to the longest member value.
        name: Stable constraint name for the generated ``CHECK``.

    Returns:
        A non-native enum column (a ``VARCHAR`` with a ``CHECK`` constraint), which behaves
        identically on PostgreSQL and SQLite and avoids the migration overhead of a native
        Postgres enum type.
    """
    return SqlEnum(
        enum_cls,
        name=name,
        length=length,
        native_enum=False,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    """Declarative base shared by every Phase 2 table."""


class MarketBarRow(Base):
    """A single normalised OHLCV bar.

    The natural key ``(symbol, market_type, timeframe, open_time)`` is enforced by a unique
    constraint rather than doubling as the primary key: a surrogate integer key keeps
    foreign-key references and ORM identity simple, while the unique constraint is what
    actually guarantees idempotency and, being a composite index with ``open_time`` last,
    also serves every range and latest-bar query the repository performs. No separate
    index is added, since one covering the same leftmost columns would be redundant.
    """

    __tablename__ = "market_bars"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "market_type",
            "timeframe",
            "open_time",
            name="uq_market_bars_natural_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(_SYMBOL_LENGTH), nullable=False)
    market_type: Mapped[MarketType] = mapped_column(
        _enum_column(MarketType, length=16, name="ck_market_bars_market_type"),
        nullable=False,
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        _enum_column(Timeframe, length=8, name="ck_market_bars_timeframe"),
        nullable=False,
    )
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(ExactNumeric(_PRICE_PRECISION, _PRICE_SCALE))
    high: Mapped[Decimal] = mapped_column(ExactNumeric(_PRICE_PRECISION, _PRICE_SCALE))
    low: Mapped[Decimal] = mapped_column(ExactNumeric(_PRICE_PRECISION, _PRICE_SCALE))
    close: Mapped[Decimal] = mapped_column(ExactNumeric(_PRICE_PRECISION, _PRICE_SCALE))
    volume: Mapped[Decimal] = mapped_column(ExactNumeric(_PRICE_PRECISION, _PRICE_SCALE))
    quote_volume: Mapped[Decimal | None] = mapped_column(
        ExactNumeric(_PRICE_PRECISION, _PRICE_SCALE), nullable=True
    )
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(_TEXT_LENGTH), nullable=False)
    is_closed: Mapped[bool] = mapped_column(nullable=False)


class IngestionRunRow(Base):
    """Provenance and outcome of a single market-data ingestion attempt."""

    __tablename__ = "ingestion_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(String(_TEXT_LENGTH), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(_CHECKSUM_LENGTH), nullable=False)
    expected_symbol: Mapped[str] = mapped_column(String(_SYMBOL_LENGTH), nullable=False)
    expected_market_type: Mapped[MarketType] = mapped_column(
        _enum_column(MarketType, length=16, name="ck_ingestion_runs_market_type"),
        nullable=False,
    )
    expected_timeframe: Mapped[Timeframe] = mapped_column(
        _enum_column(Timeframe, length=8, name="ck_ingestion_runs_timeframe"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[IngestionStatus] = mapped_column(
        _enum_column(IngestionStatus, length=32, name="ck_ingestion_runs_status"),
        nullable=False,
    )
    total_source_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exact_duplicate_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflicting_duplicate_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    info_finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fatal_finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    configuration_hash: Mapped[str] = mapped_column(String(_CHECKSUM_LENGTH), nullable=False)
    application_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(_TEXT_LENGTH), nullable=True)


class DataQualityFindingRow(Base):
    """A single integrity or quality observation raised while processing market data."""

    __tablename__ = "data_quality_findings"
    __table_args__ = (Index("ix_data_quality_findings_ingestion_run_id", "ingestion_run_id"),)

    finding_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("ingestion_runs.run_id"),
        nullable=False,
    )
    code: Mapped[DataQualityIssue] = mapped_column(
        _enum_column(DataQualityIssue, length=32, name="ck_data_quality_findings_code"),
        nullable=False,
    )
    severity: Mapped[FindingSeverity] = mapped_column(
        _enum_column(FindingSeverity, length=16, name="ck_data_quality_findings_severity"),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(String(_TEXT_LENGTH), nullable=False)
    source: Mapped[str] = mapped_column(String(_TEXT_LENGTH), nullable=False)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(_SYMBOL_LENGTH), nullable=True)
    timeframe: Mapped[Timeframe | None] = mapped_column(
        _enum_column(Timeframe, length=8, name="ck_data_quality_findings_timeframe"),
        nullable=True,
    )
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
