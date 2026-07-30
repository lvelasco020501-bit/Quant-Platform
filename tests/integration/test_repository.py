"""The SQLAlchemy repositories against a real database engine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from quantplatform.core.enums import (
    BarWriteOutcome,
    DataQualityIssue,
    FindingSeverity,
    IngestionStatus,
)
from quantplatform.core.models.data import DataQualityFinding, IngestionRun
from quantplatform.core.models.market import MarketBar
from quantplatform.storage.orm import MarketBarRow
from quantplatform.storage.repository import (
    SqlAlchemyIngestionRunRepository,
    SqlAlchemyMarketBarRepository,
)
from tests.data_helpers import MARKET_TYPE, SYMBOL, TIMEFRAME, make_bar

_LOOKUP = {"symbol": SYMBOL, "market_type": MARKET_TYPE, "timeframe": TIMEFRAME}
_DAY_START = datetime(2026, 1, 1, tzinfo=UTC)
_DAY_END = datetime(2026, 1, 2, tzinfo=UTC)


def _run(run_id: object, *, status: IngestionStatus = IngestionStatus.SUCCEEDED) -> IngestionRun:
    return IngestionRun(
        run_id=run_id,  # type: ignore[arg-type]
        source_id="csv_historical",
        source_path="/tmp/example.csv",  # noqa: S108 - a recorded path, not a file this test opens
        source_checksum="a" * 64,
        expected_symbol=SYMBOL,
        expected_market_type=MARKET_TYPE,
        expected_timeframe=TIMEFRAME,
        started_at=_DAY_START,
        completed_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        status=status,
        total_source_rows=1,
        valid_rows=1,
        rejected_rows=0,
        inserted_bars=1 if status is not IngestionStatus.FAILED else 0,
        exact_duplicate_bars=0,
        conflicting_duplicate_bars=0,
        info_finding_count=0,
        warning_finding_count=0,
        error_finding_count=0,
        fatal_finding_count=0,
        configuration_hash="b" * 64,
        application_version="0.1.0",
        error_summary="failed" if status is IngestionStatus.FAILED else None,
    )


async def test_bars_round_trip_as_domain_models(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = make_bar(hour=0)
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars([original])
        await session.commit()

    async with session_factory() as session:
        stored = await SqlAlchemyMarketBarRepository(session).get_bars(
            **_LOOKUP, start=_DAY_START, end=_DAY_END
        )

    assert len(stored) == 1
    assert isinstance(stored[0], MarketBar)
    assert stored[0] == original


async def test_decimal_precision_survives_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    precise = make_bar(hour=0).model_copy(
        update={
            "open": Decimal("50000.123456789012345678"),
            "close": Decimal("50100.876543210987654321"),
            "volume": Decimal("12.000000000000000001"),
        }
    )
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars([precise])
        await session.commit()

    async with session_factory() as session:
        stored = await SqlAlchemyMarketBarRepository(session).get_bars(
            **_LOOKUP, start=_DAY_START, end=_DAY_END
        )

    assert stored[0].open == Decimal("50000.123456789012345678")
    assert stored[0].close == Decimal("50100.876543210987654321")
    assert stored[0].volume == Decimal("12.000000000000000001")


async def test_stored_timestamps_come_back_timezone_aware(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars([make_bar(hour=0)])
        await session.commit()

    async with session_factory() as session:
        stored = await SqlAlchemyMarketBarRepository(session).get_bars(
            **_LOOKUP, start=_DAY_START, end=_DAY_END
        )

    assert stored[0].open_time.tzinfo is not None
    assert stored[0].open_time == _DAY_START


async def test_re_adding_an_identical_bar_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bar = make_bar(hour=0)
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars([bar])
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyMarketBarRepository(session)
        results = await repository.add_bars([bar])
        await session.commit()
        assert results[0].outcome is BarWriteOutcome.EXACT_DUPLICATE
        assert await repository.count_bars(**_LOOKUP) == 1


async def test_conflicting_bar_is_reported_and_never_overwrites(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = make_bar(hour=0, close="50100")
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars([original])
        await session.commit()

    revised = make_bar(hour=0, close="50177")
    async with session_factory() as session:
        results = await SqlAlchemyMarketBarRepository(session).add_bars([revised])
        await session.commit()

    assert results[0].outcome is BarWriteOutcome.CONFLICTING
    assert results[0].existing_bar is not None
    assert results[0].existing_bar.close == Decimal("50100")

    async with session_factory() as session:
        stored = await SqlAlchemyMarketBarRepository(session).get_bars(
            **_LOOKUP, start=_DAY_START, end=_DAY_END
        )
    assert stored[0].close == Decimal("50100")


async def test_the_unique_constraint_is_enforced_by_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Bypassing the repository proves the guarantee is in the schema, not just in Python.
    bar = make_bar(hour=0)
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars([bar])
        await session.commit()

    async with session_factory() as session:
        session.add(
            MarketBarRow(
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
                quote_volume=None,
                trade_count=bar.trade_count,
                source=bar.source,
                is_closed=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_time_range_query_is_half_open_and_ordered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bars = [make_bar(hour=hour) for hour in (3, 0, 2, 1)]
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars(bars)
        await session.commit()

    async with session_factory() as session:
        selected = await SqlAlchemyMarketBarRepository(session).get_bars(
            **_LOOKUP,
            start=datetime(2026, 1, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 1, 3, tzinfo=UTC),
        )

    assert [bar.open_time.hour for bar in selected] == [1, 2]


async def test_latest_bar_is_deterministic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars(
            [make_bar(hour=hour) for hour in (0, 3, 1)]
        )
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyMarketBarRepository(session)
        first = await repository.get_latest_bar(**_LOOKUP)
        second = await repository.get_latest_bar(**_LOOKUP)

    assert first is not None
    assert first.open_time.hour == 3
    assert first == second


async def test_latest_bar_is_none_when_nothing_is_stored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await SqlAlchemyMarketBarRepository(session).get_latest_bar(**_LOOKUP) is None


async def test_exists_and_count_reflect_the_natural_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repository = SqlAlchemyMarketBarRepository(session)
        await repository.add_bars([make_bar(hour=0)])
        await session.commit()

        assert await repository.exists(**_LOOKUP, open_time=_DAY_START)
        assert not await repository.exists(**_LOOKUP, open_time=datetime(2026, 1, 1, 5, tzinfo=UTC))
        assert await repository.count_bars(**_LOOKUP) == 1
        assert (
            await repository.count_bars(
                symbol="ETH/USDT", market_type=MARKET_TYPE, timeframe=TIMEFRAME
            )
            == 0
        )


async def test_bars_for_other_instruments_are_not_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars(
            [make_bar(hour=0), make_bar(hour=0, symbol="ETH/USDT")]
        )
        await session.commit()

    async with session_factory() as session:
        stored = await SqlAlchemyMarketBarRepository(session).get_bars(
            **_LOOKUP, start=_DAY_START, end=_DAY_END
        )

    assert len(stored) == 1
    assert stored[0].symbol == SYMBOL


async def test_repository_does_not_commit_on_its_own(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await SqlAlchemyMarketBarRepository(session).add_bars([make_bar(hour=0)])
        # Deliberately no commit: the caller owns the transaction boundary.

    async with session_factory() as session:
        assert await SqlAlchemyMarketBarRepository(session).count_bars(**_LOOKUP) == 0


async def test_runs_and_findings_round_trip_and_stay_linked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = uuid4()
    run = _run(run_id)
    finding = DataQualityFinding(
        finding_id=uuid4(),
        ingestion_run_id=run_id,
        code=DataQualityIssue.MISSING_BAR,
        severity=FindingSeverity.WARNING,
        message="one bar missing",
        source="csv_historical",
        source_row=None,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        open_time=_DAY_START,
        context={"count": "1"},
        detected_at=_DAY_START,
    )

    async with session_factory() as session:
        await SqlAlchemyIngestionRunRepository(session).record_run(run, [finding])
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyIngestionRunRepository(session)
        stored_run = await repository.get_run(run_id)
        stored_findings = await repository.get_findings(run_id)

    assert stored_run == run
    assert len(stored_findings) == 1
    assert stored_findings[0].ingestion_run_id == run_id
    assert stored_findings[0].context == {"count": "1"}


async def test_unknown_run_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await SqlAlchemyIngestionRunRepository(session).get_run(uuid4()) is None


async def test_failed_runs_are_recorded_too(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = _run(uuid4(), status=IngestionStatus.FAILED)
    async with session_factory() as session:
        await SqlAlchemyIngestionRunRepository(session).record_run(run, [])
        await session.commit()

    async with session_factory() as session:
        stored = await SqlAlchemyIngestionRunRepository(session).get_run(run.run_id)

    assert stored is not None
    assert stored.status is IngestionStatus.FAILED
    assert stored.error_summary == "failed"
