"""SQLAlchemy-backed implementations of the core repository protocols.

Both repositories are constructed with an externally managed
:class:`~sqlalchemy.ext.asyncio.AsyncSession` and never call ``commit`` or ``rollback``
themselves. Transaction ownership belongs to the caller — the ingestion service — which is
what lets it roll back a bar-write attempt on a fatal finding while still persisting the
run and its findings afterward in a separate, always-attempted transaction.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quantplatform.core.enums import BarWriteOutcome, MarketType, Timeframe
from quantplatform.core.models.data import BarWriteResult, DataQualityFinding, IngestionRun
from quantplatform.core.models.market import MarketBar
from quantplatform.storage.orm import DataQualityFindingRow, IngestionRunRow, MarketBarRow

__all__ = ["SqlAlchemyIngestionRunRepository", "SqlAlchemyMarketBarRepository"]

_NaturalKey = tuple[str, MarketType, Timeframe, datetime]


def _ensure_aware_utc(value: datetime) -> datetime:
    """Reattach UTC to a timestamp SQLite returned naive; pass through an already-aware one.

    PostgreSQL's ``timestamptz`` always round-trips as timezone-aware. SQLite has no such
    type and returns naive datetimes for a ``DateTime(timezone=True)`` column, even though
    every value written through this repository originated from a UTC-validated domain
    model. Reattaching UTC here, rather than trusting the driver, keeps behaviour identical
    across both backends.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _row_to_bar(row: MarketBarRow) -> MarketBar:
    """Map a persisted row to the domain model the repository contract promises to return."""
    return MarketBar(
        symbol=row.symbol,
        market_type=row.market_type,
        timeframe=row.timeframe,
        open_time=_ensure_aware_utc(row.open_time),
        close_time=_ensure_aware_utc(row.close_time),
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        quote_volume=row.quote_volume,
        trade_count=row.trade_count,
        source=row.source,
        is_closed=row.is_closed,
    )


def _bar_to_row(bar: MarketBar) -> MarketBarRow:
    """Map a domain model to a new, unattached row ready to be added to a session."""
    return MarketBarRow(
        symbol=bar.symbol,
        market_type=bar.market_type,
        timeframe=bar.timeframe,
        open_time=bar.open_time,
        close_time=bar.close_time,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        quote_volume=bar.quote_volume,
        trade_count=bar.trade_count,
        source=bar.source,
        is_closed=bar.is_closed,
    )


def _bars_have_equal_values(stored: MarketBar, incoming: MarketBar) -> bool:
    """Return whether two bars sharing a natural key carry identical OHLCV values.

    ``source`` is deliberately excluded: re-ingesting the same candle from a differently
    named source with identical prices and volume is still an exact duplicate, not a
    conflict, because the natural key governs identity and the OHLCV values govern
    equality, not the provenance label.
    """
    return (
        stored.open == incoming.open
        and stored.high == incoming.high
        and stored.low == incoming.low
        and stored.close == incoming.close
        and stored.volume == incoming.volume
        and stored.quote_volume == incoming.quote_volume
        and stored.trade_count == incoming.trade_count
    )


class SqlAlchemyMarketBarRepository:
    """Persists and retrieves :class:`~quantplatform.core.models.market.MarketBar` rows.

    Implements :class:`~quantplatform.core.interfaces.MarketBarRepository`.

    Args:
        session: An externally managed session; this repository never commits it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_bars(self, bars: Sequence[MarketBar]) -> Sequence[BarWriteResult]:
        """Idempotently stage bars for persistence.

        Existing rows sharing a natural key with any incoming bar are fetched first (one
        query per distinct symbol/market/timeframe combination in the batch, covering every
        requested open time at once) so each incoming bar can be classified as inserted, an
        exact duplicate, or conflicting before anything is written.
        """
        if not bars:
            return []

        groups: dict[tuple[str, MarketType, Timeframe], list[MarketBar]] = defaultdict(list)
        for bar in bars:
            groups[(bar.symbol, bar.market_type, bar.timeframe)].append(bar)

        existing_by_key: dict[_NaturalKey, MarketBar] = {}
        for (symbol, market_type, timeframe), group in groups.items():
            open_times = [bar.open_time for bar in group]
            stmt = select(MarketBarRow).where(
                MarketBarRow.symbol == symbol,
                MarketBarRow.market_type == market_type,
                MarketBarRow.timeframe == timeframe,
                MarketBarRow.open_time.in_(open_times),
            )
            rows = (await self._session.execute(stmt)).scalars().all()
            for row in rows:
                stored = _row_to_bar(row)
                stored_key = (stored.symbol, stored.market_type, stored.timeframe, stored.open_time)
                existing_by_key[stored_key] = stored

        results: list[BarWriteResult] = []
        to_insert: list[MarketBar] = []
        for bar in bars:
            key: _NaturalKey = (bar.symbol, bar.market_type, bar.timeframe, bar.open_time)
            matching_stored_bar = existing_by_key.get(key)
            if matching_stored_bar is None:
                to_insert.append(bar)
                results.append(BarWriteResult(bar=bar, outcome=BarWriteOutcome.INSERTED))
            elif _bars_have_equal_values(matching_stored_bar, bar):
                results.append(BarWriteResult(bar=bar, outcome=BarWriteOutcome.EXACT_DUPLICATE))
            else:
                results.append(
                    BarWriteResult(
                        bar=bar,
                        outcome=BarWriteOutcome.CONFLICTING,
                        existing_bar=matching_stored_bar,
                    )
                )

        if to_insert:
            self._session.add_all(_bar_to_row(bar) for bar in to_insert)
            await self._session.flush()

        return results

    async def get_bars(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketBar]:
        """Return stored bars in ``[start, end)``, ordered deterministically by open time."""
        stmt = (
            select(MarketBarRow)
            .where(
                MarketBarRow.symbol == symbol,
                MarketBarRow.market_type == market_type,
                MarketBarRow.timeframe == timeframe,
                MarketBarRow.open_time >= start,
                MarketBarRow.open_time < end,
            )
            .order_by(MarketBarRow.open_time.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return tuple(_row_to_bar(row) for row in rows)

    async def get_latest_bar(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
    ) -> MarketBar | None:
        """Return the most recent stored bar, or ``None`` if none is stored."""
        stmt = (
            select(MarketBarRow)
            .where(
                MarketBarRow.symbol == symbol,
                MarketBarRow.market_type == market_type,
                MarketBarRow.timeframe == timeframe,
            )
            .order_by(MarketBarRow.open_time.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _row_to_bar(row) if row is not None else None

    async def exists(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        open_time: datetime,
    ) -> bool:
        """Return whether a bar is already stored under this natural key."""
        stmt = select(MarketBarRow.id).where(
            MarketBarRow.symbol == symbol,
            MarketBarRow.market_type == market_type,
            MarketBarRow.timeframe == timeframe,
            MarketBarRow.open_time == open_time,
        )
        return (await self._session.execute(stmt)).scalars().first() is not None

    async def count_bars(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
    ) -> int:
        """Return the number of stored bars for a symbol, market and timeframe."""
        stmt = select(MarketBarRow.id).where(
            MarketBarRow.symbol == symbol,
            MarketBarRow.market_type == market_type,
            MarketBarRow.timeframe == timeframe,
        )
        return len((await self._session.execute(stmt)).all())


class SqlAlchemyIngestionRunRepository:
    """Persists ingestion provenance and findings.

    Implements :class:`~quantplatform.core.interfaces.IngestionRunRepository`.

    Args:
        session: An externally managed session; this repository never commits it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_run(
        self,
        run: IngestionRun,
        findings: Sequence[DataQualityFinding],
    ) -> None:
        """Stage a concluded run and every finding it raised for persistence.

        The run row is flushed before the findings that reference it. SQLAlchemy derives
        flush ordering from ``relationship()`` declarations, not from a bare
        :class:`~sqlalchemy.ForeignKey` constraint, so without this explicit ordering the
        findings can be inserted first and violate the foreign key. PostgreSQL enforces
        that immediately; ordering the two writes is what makes the mapping correct rather
        than accidentally working. Both flushes share the caller's transaction, so the pair
        remains atomic.
        """
        self._session.add(
            IngestionRunRow(
                run_id=run.run_id,
                source_id=run.source_id,
                source_path=run.source_path,
                source_checksum=run.source_checksum,
                expected_symbol=run.expected_symbol,
                expected_market_type=run.expected_market_type,
                expected_timeframe=run.expected_timeframe,
                started_at=run.started_at,
                completed_at=run.completed_at,
                status=run.status,
                total_source_rows=run.total_source_rows,
                valid_rows=run.valid_rows,
                rejected_rows=run.rejected_rows,
                inserted_bars=run.inserted_bars,
                exact_duplicate_bars=run.exact_duplicate_bars,
                conflicting_duplicate_bars=run.conflicting_duplicate_bars,
                info_finding_count=run.info_finding_count,
                warning_finding_count=run.warning_finding_count,
                error_finding_count=run.error_finding_count,
                fatal_finding_count=run.fatal_finding_count,
                configuration_hash=run.configuration_hash,
                application_version=run.application_version,
                error_summary=run.error_summary,
            )
        )
        await self._session.flush()

        self._session.add_all(
            DataQualityFindingRow(
                finding_id=finding.finding_id,
                ingestion_run_id=finding.ingestion_run_id,
                code=finding.code,
                severity=finding.severity,
                message=finding.message,
                source=finding.source,
                source_row=finding.source_row,
                symbol=finding.symbol,
                timeframe=finding.timeframe,
                open_time=finding.open_time,
                context=dict(finding.context),
                detected_at=finding.detected_at,
            )
            for finding in findings
        )
        await self._session.flush()

    async def get_run(self, run_id: UUID) -> IngestionRun | None:
        """Return a previously persisted run by id, if it exists."""
        row = await self._session.get(IngestionRunRow, run_id)
        if row is None:
            return None
        return IngestionRun(
            run_id=row.run_id,
            source_id=row.source_id,
            source_path=row.source_path,
            source_checksum=row.source_checksum,
            expected_symbol=row.expected_symbol,
            expected_market_type=row.expected_market_type,
            expected_timeframe=row.expected_timeframe,
            started_at=_ensure_aware_utc(row.started_at),
            completed_at=_ensure_aware_utc(row.completed_at),
            status=row.status,
            total_source_rows=row.total_source_rows,
            valid_rows=row.valid_rows,
            rejected_rows=row.rejected_rows,
            inserted_bars=row.inserted_bars,
            exact_duplicate_bars=row.exact_duplicate_bars,
            conflicting_duplicate_bars=row.conflicting_duplicate_bars,
            info_finding_count=row.info_finding_count,
            warning_finding_count=row.warning_finding_count,
            error_finding_count=row.error_finding_count,
            fatal_finding_count=row.fatal_finding_count,
            configuration_hash=row.configuration_hash,
            application_version=row.application_version,
            error_summary=row.error_summary,
        )

    async def get_findings(self, run_id: UUID) -> Sequence[DataQualityFinding]:
        """Return every finding recorded against a run."""
        stmt = select(DataQualityFindingRow).where(DataQualityFindingRow.ingestion_run_id == run_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return tuple(
            DataQualityFinding(
                finding_id=row.finding_id,
                ingestion_run_id=row.ingestion_run_id,
                code=row.code,
                severity=row.severity,
                message=row.message,
                source=row.source,
                source_row=row.source_row,
                symbol=row.symbol,
                timeframe=row.timeframe,
                open_time=_ensure_aware_utc(row.open_time) if row.open_time is not None else None,
                context=dict(row.context),
                detected_at=_ensure_aware_utc(row.detected_at),
            )
            for row in rows
        )
