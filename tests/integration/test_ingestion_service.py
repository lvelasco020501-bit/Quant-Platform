"""The ingestion service end to end, over both an in-memory and a real database."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import (
    DataQualityIssue,
    FindingSeverity,
    IngestionStatus,
)
from quantplatform.core.events import (
    DataQualityIssueDetected,
    IngestionCompleted,
    IngestionFailed,
    IngestionStarted,
)
from quantplatform.core.interfaces import DataUnitOfWork
from quantplatform.data.ingestion import IngestionService
from quantplatform.data.results import IngestionResult
from quantplatform.monitoring.publishers import InMemoryEventPublisher
from quantplatform.storage.repository import (
    SqlAlchemyIngestionRunRepository,
    SqlAlchemyMarketBarRepository,
)
from quantplatform.storage.unit_of_work import SqlAlchemyDataUnitOfWork
from tests.data_helpers import (
    AFTER_ALL_FIXTURES,
    MARKET_TYPE,
    SYMBOL,
    TIMEFRAME,
    InMemoryDataUnitOfWork,
    InMemoryIngestionRunRepository,
    InMemoryMarketBarRepository,
    data_settings,
    fixture,
)

_LOOKUP = {"symbol": SYMBOL, "market_type": MARKET_TYPE, "timeframe": TIMEFRAME}


class _Harness:
    """An ingestion service wired to in-memory storage, for behaviour-focused tests."""

    def __init__(self, *, now: datetime = AFTER_ALL_FIXTURES, **settings: object) -> None:
        self.bars = InMemoryMarketBarRepository()
        self.runs = InMemoryIngestionRunRepository()
        self.publisher = InMemoryEventPublisher()
        self.clock = SimulatedClock(now)
        self.units: list[InMemoryDataUnitOfWork] = []
        self.service = IngestionService(
            unit_of_work_factory=self._make_unit,
            clock=self.clock,
            publisher=self.publisher,
            settings=data_settings(**settings),
        )

    def _make_unit(self) -> DataUnitOfWork:
        unit = InMemoryDataUnitOfWork(self.bars, self.runs)
        self.units.append(unit)
        return unit  # type: ignore[return-value]

    async def ingest(self, name: str, **kwargs: object) -> IngestionResult:
        """Ingest a named fixture."""
        return await self.service.ingest(
            fixture(name),
            symbol=SYMBOL,
            market_type=MARKET_TYPE,
            timeframe=TIMEFRAME,
            **kwargs,  # type: ignore[arg-type]
        )


def _codes(result: IngestionResult) -> set[DataQualityIssue]:
    return {finding.code for finding in result.findings}


# --- Happy path -----------------------------------------------------------------------------


async def test_valid_file_is_ingested_and_persisted() -> None:
    harness = _Harness()
    result = await harness.ingest("valid.csv")

    assert result.succeeded
    assert result.run.status is IngestionStatus.SUCCEEDED
    assert result.run.total_source_rows == 4
    assert result.run.valid_rows == 4
    assert result.run.rejected_rows == 0
    assert result.run.inserted_bars == 4
    assert len(harness.bars.bars) == 4


async def test_run_records_full_provenance() -> None:
    harness = _Harness()
    result = await harness.ingest("valid.csv")
    run = result.run

    assert run.source_id == "csv_historical"
    assert run.source_path.endswith("valid.csv")
    assert len(run.source_checksum) == 64
    assert len(run.configuration_hash) == 64
    assert run.application_version == "0.1.0"
    assert run.expected_symbol == SYMBOL
    assert run.completed_at >= run.started_at


async def test_configuration_hash_changes_with_configuration() -> None:
    first = await _Harness(close_grace_period_seconds=0).ingest("valid.csv")
    second = await _Harness(close_grace_period_seconds=30).ingest("valid.csv")

    assert first.run.source_checksum == second.run.source_checksum
    assert first.run.configuration_hash != second.run.configuration_hash


async def test_lifecycle_events_are_published() -> None:
    harness = _Harness()
    await harness.ingest("valid.csv")

    kinds = [type(event) for event in harness.publisher.events]
    assert IngestionStarted in kinds
    assert IngestionCompleted in kinds
    assert IngestionFailed not in kinds


async def test_each_finding_is_published_and_correlated_to_the_run() -> None:
    harness = _Harness()
    result = await harness.ingest("missing_interval.csv")

    published = [
        event for event in harness.publisher.events if isinstance(event, DataQualityIssueDetected)
    ]
    assert len(published) == len(result.findings)
    assert all(event.correlation_id == result.run.run_id for event in published)


# --- Idempotency and revisions ----------------------------------------------------------------


async def test_re_ingesting_the_same_file_inserts_nothing_further() -> None:
    harness = _Harness()
    first = await harness.ingest("valid.csv")
    second = await harness.ingest("valid.csv")

    assert first.run.inserted_bars == 4
    assert second.run.inserted_bars == 0
    assert second.run.exact_duplicate_bars == 4
    assert second.succeeded
    assert len(harness.bars.bars) == 4


async def test_re_ingestion_produces_a_distinct_traceable_run() -> None:
    harness = _Harness()
    first = await harness.ingest("valid.csv")
    second = await harness.ingest("valid.csv")

    assert first.run.run_id != second.run.run_id
    assert set(harness.runs.runs) == {first.run.run_id, second.run.run_id}


async def test_conflicting_revision_is_reported_and_never_overwrites() -> None:
    harness = _Harness()
    await harness.ingest("valid.csv")
    result = await harness.ingest("revised.csv")

    assert result.run.conflicting_duplicate_bars == 1
    assert result.run.inserted_bars == 0
    assert DataQualityIssue.REVISED_BAR in _codes(result)

    stored = harness.bars.bars[(SYMBOL, MARKET_TYPE, TIMEFRAME, datetime(2026, 1, 1, tzinfo=UTC))]
    assert stored.close == Decimal("50100")


async def test_revision_finding_records_both_versions() -> None:
    harness = _Harness()
    await harness.ingest("valid.csv")
    result = await harness.ingest("revised.csv")

    revision = next(f for f in result.findings if f.code is DataQualityIssue.REVISED_BAR)
    assert revision.context["stored_close"] == "50100"
    assert revision.context["incoming_close"] == "50188"
    assert revision.severity is FindingSeverity.WARNING


# --- Data quality outcomes --------------------------------------------------------------------


async def test_duplicate_rows_within_a_file_are_collapsed() -> None:
    harness = _Harness()
    result = await harness.ingest("exact_duplicate.csv")

    assert result.run.inserted_bars == 1
    assert DataQualityIssue.DUPLICATE_BAR in _codes(result)


async def test_conflicting_rows_within_a_file_keep_the_first() -> None:
    harness = _Harness()
    result = await harness.ingest("conflicting_duplicate.csv")

    assert result.run.inserted_bars == 1
    stored = next(iter(harness.bars.bars.values()))
    assert stored.close == Decimal("50100")


async def test_gaps_are_reported_without_manufacturing_bars() -> None:
    harness = _Harness()
    result = await harness.ingest("missing_interval.csv")

    assert result.run.inserted_bars == 2
    assert DataQualityIssue.MISSING_BAR in _codes(result)
    assert result.run.status is IngestionStatus.SUCCEEDED_WITH_FINDINGS


async def test_excessive_gaps_fail_the_run_and_persist_nothing() -> None:
    harness = _Harness(max_allowed_gap_bars=0)
    result = await harness.ingest("missing_interval.csv")

    assert not result.succeeded
    assert result.run.status is IngestionStatus.FAILED
    assert result.run.inserted_bars == 0
    assert harness.bars.bars == {}


async def test_invalid_rows_are_rejected_while_valid_ones_proceed() -> None:
    harness = _Harness()
    result = await harness.ingest("mixed_symbols.csv")

    assert result.run.valid_rows == 1
    assert result.run.rejected_rows == 1
    assert result.run.inserted_bars == 1
    assert result.succeeded


async def test_a_file_whose_every_row_is_rejected_fails() -> None:
    harness = _Harness()
    result = await harness.ingest("malformed_numeric.csv")

    assert not result.succeeded
    assert DataQualityIssue.EMPTY_DATASET in _codes(result)
    assert harness.bars.bars == {}


async def test_open_candles_are_never_persisted() -> None:
    harness = _Harness(now=datetime(2026, 1, 1, 4, 30, tzinfo=UTC))
    result = await harness.ingest(
        "unclosed_final.csv",
        historical_end=datetime(2026, 1, 1, 4, 30, tzinfo=UTC),
    )

    assert result.run.inserted_bars == 1
    assert DataQualityIssue.OPEN_CANDLE in _codes(result)
    assert all(bar.is_closed for bar in harness.bars.bars.values())


async def test_out_of_order_input_is_reported_yet_stored_in_order() -> None:
    harness = _Harness()
    result = await harness.ingest("out_of_order.csv")

    assert DataQualityIssue.OUT_OF_ORDER_BAR in _codes(result)
    stored = await harness.bars.get_bars(
        **_LOOKUP, start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert [bar.open_time.hour for bar in stored] == [0, 1]


async def test_historical_backfill_is_not_reported_stale() -> None:
    harness = _Harness(now=datetime(2030, 1, 1, tzinfo=UTC))
    result = await harness.ingest("valid.csv", historical_end=datetime(2026, 1, 1, 5, tzinfo=UTC))

    assert DataQualityIssue.STALE_DATA not in _codes(result)
    assert result.succeeded


async def test_live_like_import_of_old_data_is_reported_stale() -> None:
    harness = _Harness(now=datetime(2030, 1, 1, tzinfo=UTC))
    result = await harness.ingest("valid.csv")

    assert DataQualityIssue.STALE_DATA in _codes(result)


# --- Failure handling -------------------------------------------------------------------------


async def test_unreadable_source_fails_without_persisting_bars(tmp_path: Path) -> None:
    harness = _Harness()
    result = await harness.service.ingest(
        tmp_path / "absent.csv",
        symbol=SYMBOL,
        market_type=MARKET_TYPE,
        timeframe=TIMEFRAME,
    )

    assert not result.succeeded
    assert harness.bars.bars == {}
    assert result.run.error_summary is not None


async def test_missing_required_column_fails_the_run() -> None:
    harness = _Harness()
    result = await harness.ingest("missing_column.csv")

    assert not result.succeeded
    assert result.run.fatal_finding_count >= 1


async def test_a_failed_run_is_still_recorded_for_audit() -> None:
    harness = _Harness()
    result = await harness.ingest("missing_column.csv")

    assert result.run.run_id in harness.runs.runs
    assert harness.runs.runs[result.run.run_id].status is IngestionStatus.FAILED
    assert len(harness.runs.findings[result.run.run_id]) >= 1


async def test_a_failed_run_publishes_the_failure_event() -> None:
    harness = _Harness()
    await harness.ingest("missing_column.csv")

    assert any(isinstance(event, IngestionFailed) for event in harness.publisher.events)


async def test_storage_failure_rolls_back_every_bar() -> None:
    harness = _Harness()
    harness.bars.fail_on_add = True
    result = await harness.ingest("valid.csv")

    assert not result.succeeded
    assert harness.bars.bars == {}
    assert result.run.inserted_bars == 0
    assert "persistence failed" in (result.run.error_summary or "")


async def test_a_fatal_run_never_commits_its_bar_transaction() -> None:
    harness = _Harness(max_allowed_gap_bars=0)
    await harness.ingest("missing_interval.csv")

    # Only the failure-recording scope commits; the bar-writing scope is never opened.
    assert [unit.committed for unit in harness.units] == [True]
    assert harness.bars.bars == {}


# --- Dry run --------------------------------------------------------------------------------


async def test_dry_run_persists_nothing_at_all() -> None:
    harness = _Harness()
    result = await harness.ingest("valid.csv", dry_run=True)

    assert result.succeeded
    assert result.run.inserted_bars == 0
    assert harness.bars.bars == {}
    assert harness.runs.runs == {}
    assert harness.units == []


async def test_dry_run_still_reports_every_finding() -> None:
    harness = _Harness()
    result = await harness.ingest("missing_interval.csv", dry_run=True)

    assert DataQualityIssue.MISSING_BAR in _codes(result)


# --- Against a real database ------------------------------------------------------------------


def _sql_harness(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime = AFTER_ALL_FIXTURES,
) -> tuple[IngestionService, InMemoryEventPublisher]:
    publisher = InMemoryEventPublisher()

    def factory() -> DataUnitOfWork:
        return SqlAlchemyDataUnitOfWork(session_factory)

    return (
        IngestionService(
            unit_of_work_factory=factory,
            clock=SimulatedClock(now),
            publisher=publisher,
            settings=data_settings(),
        ),
        publisher,
    )


async def test_ingestion_persists_through_a_real_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, _ = _sql_harness(session_factory)
    result = await service.ingest(
        fixture("valid.csv"), symbol=SYMBOL, market_type=MARKET_TYPE, timeframe=TIMEFRAME
    )

    assert result.succeeded
    async with session_factory() as session:
        stored = await SqlAlchemyMarketBarRepository(session).get_bars(
            **_LOOKUP,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
    assert [bar.open_time.hour for bar in stored] == [0, 1, 2, 3]
    assert stored[0].open == Decimal("50000.10")


async def test_re_ingestion_through_a_real_database_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, _ = _sql_harness(session_factory)
    for _ in range(2):
        await service.ingest(
            fixture("valid.csv"), symbol=SYMBOL, market_type=MARKET_TYPE, timeframe=TIMEFRAME
        )

    async with session_factory() as session:
        assert await SqlAlchemyMarketBarRepository(session).count_bars(**_LOOKUP) == 4


async def test_fatal_run_leaves_a_real_database_untouched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    publisher = InMemoryEventPublisher()
    service = IngestionService(
        unit_of_work_factory=lambda: SqlAlchemyDataUnitOfWork(session_factory),
        clock=SimulatedClock(AFTER_ALL_FIXTURES),
        publisher=publisher,
        settings=data_settings(max_allowed_gap_bars=0),
    )
    result = await service.ingest(
        fixture("missing_interval.csv"),
        symbol=SYMBOL,
        market_type=MARKET_TYPE,
        timeframe=TIMEFRAME,
    )

    assert not result.succeeded
    async with session_factory() as session:
        assert await SqlAlchemyMarketBarRepository(session).count_bars(**_LOOKUP) == 0


async def test_findings_are_linked_to_their_run_in_a_real_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, _ = _sql_harness(session_factory)
    result = await service.ingest(
        fixture("missing_interval.csv"),
        symbol=SYMBOL,
        market_type=MARKET_TYPE,
        timeframe=TIMEFRAME,
    )

    async with session_factory() as session:
        repository = SqlAlchemyIngestionRunRepository(session)
        stored_run = await repository.get_run(result.run.run_id)
        stored_findings = await repository.get_findings(result.run.run_id)

    assert stored_run is not None
    assert len(stored_findings) == len(result.findings)
    assert all(f.ingestion_run_id == result.run.run_id for f in stored_findings)


@pytest.mark.parametrize("name", ["valid.csv", "missing_interval.csv", "exact_duplicate.csv"])
async def test_ingestion_is_deterministic_under_a_simulated_clock(name: str) -> None:
    first = await _Harness().ingest(name)
    second = await _Harness().ingest(name)

    assert first.run.source_checksum == second.run.source_checksum
    assert first.run.configuration_hash == second.run.configuration_hash
    assert first.run.status is second.run.status
    assert first.run.inserted_bars == second.run.inserted_bars
    assert [f.code for f in first.findings] == [f.code for f in second.findings]
    assert [f.message for f in first.findings] == [f.message for f in second.findings]
    assert first.bars_written == second.bars_written
