"""Create Phase 2 data tables.

Creates the three tables the historical market-data pipeline persists to:
``market_bars``, ``ingestion_runs`` and ``data_quality_findings``. Schema, constraints and
column types mirror :mod:`quantplatform.storage.orm` exactly, since that module is the
single source of truth for the mapped schema and this migration is how it reaches a real
database.

Revision ID: 67a5fddd9b84
Revises:
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quantplatform.storage.types import ExactNumeric

revision: str = "67a5fddd9b84"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRICE_PRECISION = 38
_PRICE_SCALE = 18


def upgrade() -> None:
    """Create the ingestion_runs, market_bars and data_quality_findings tables."""
    op.create_table(
        "ingestion_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("expected_symbol", sa.String(length=41), nullable=False),
        sa.Column(
            "expected_market_type",
            sa.Enum(
                "spot",
                "margin",
                "futures",
                "perpetual",
                name="ck_ingestion_runs_market_type",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "expected_timeframe",
            sa.Enum(
                "1m",
                "3m",
                "5m",
                "15m",
                "30m",
                "1h",
                "2h",
                "4h",
                "6h",
                "12h",
                "1d",
                "1w",
                name="ck_ingestion_runs_timeframe",
                native_enum=False,
                length=8,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "succeeded",
                "succeeded_with_findings",
                "failed",
                name="ck_ingestion_runs_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("total_source_rows", sa.Integer(), nullable=False),
        sa.Column("valid_rows", sa.Integer(), nullable=False),
        sa.Column("rejected_rows", sa.Integer(), nullable=False),
        sa.Column("inserted_bars", sa.Integer(), nullable=False),
        sa.Column("exact_duplicate_bars", sa.Integer(), nullable=False),
        sa.Column("conflicting_duplicate_bars", sa.Integer(), nullable=False),
        sa.Column("info_finding_count", sa.Integer(), nullable=False),
        sa.Column("warning_finding_count", sa.Integer(), nullable=False),
        sa.Column("error_finding_count", sa.Integer(), nullable=False),
        sa.Column("fatal_finding_count", sa.Integer(), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("application_version", sa.String(length=255), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "market_bars",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=41), nullable=False),
        sa.Column(
            "market_type",
            sa.Enum(
                "spot",
                "margin",
                "futures",
                "perpetual",
                name="ck_market_bars_market_type",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "timeframe",
            sa.Enum(
                "1m",
                "3m",
                "5m",
                "15m",
                "30m",
                "1h",
                "2h",
                "4h",
                "6h",
                "12h",
                "1d",
                "1w",
                name="ck_market_bars_timeframe",
                native_enum=False,
                length=8,
            ),
            nullable=False,
        ),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "open", ExactNumeric(precision=_PRICE_PRECISION, scale=_PRICE_SCALE), nullable=False
        ),
        sa.Column(
            "high", ExactNumeric(precision=_PRICE_PRECISION, scale=_PRICE_SCALE), nullable=False
        ),
        sa.Column(
            "low", ExactNumeric(precision=_PRICE_PRECISION, scale=_PRICE_SCALE), nullable=False
        ),
        sa.Column(
            "close", ExactNumeric(precision=_PRICE_PRECISION, scale=_PRICE_SCALE), nullable=False
        ),
        sa.Column(
            "volume", ExactNumeric(precision=_PRICE_PRECISION, scale=_PRICE_SCALE), nullable=False
        ),
        sa.Column(
            "quote_volume",
            ExactNumeric(precision=_PRICE_PRECISION, scale=_PRICE_SCALE),
            nullable=True,
        ),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=500), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol", "market_type", "timeframe", "open_time", name="uq_market_bars_natural_key"
        ),
    )
    op.create_table(
        "data_quality_findings",
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "code",
            sa.Enum(
                "missing_bar",
                "duplicate_bar",
                "out_of_order_bar",
                "stale_data",
                "invalid_ohlc",
                "negative_volume",
                "unexpected_timeframe",
                "unexpected_symbol",
                "unexpected_market_type",
                "open_candle",
                "closure_conflict",
                "malformed_record",
                "revised_bar",
                name="ck_data_quality_findings_code",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum(
                "info",
                "warning",
                "error",
                "fatal",
                name="ck_data_quality_findings_severity",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=500), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=41), nullable=True),
        sa.Column(
            "timeframe",
            sa.Enum(
                "1m",
                "3m",
                "5m",
                "15m",
                "30m",
                "1h",
                "2h",
                "4h",
                "6h",
                "12h",
                "1d",
                "1w",
                name="ck_data_quality_findings_timeframe",
                native_enum=False,
                length=8,
            ),
            nullable=True,
        ),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.run_id"]),
        sa.PrimaryKeyConstraint("finding_id"),
    )
    op.create_index(
        "ix_data_quality_findings_ingestion_run_id",
        "data_quality_findings",
        ["ingestion_run_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the Phase 2 tables in dependency order (findings before their FK targets)."""
    op.drop_index("ix_data_quality_findings_ingestion_run_id", table_name="data_quality_findings")
    op.drop_table("data_quality_findings")
    op.drop_table("market_bars")
    op.drop_table("ingestion_runs")
