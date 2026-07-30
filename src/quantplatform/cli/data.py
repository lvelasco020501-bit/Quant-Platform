"""Data-layer command line surface.

Three commands cover the historical pipeline: ``validate`` inspects a file without writing
anything, ``ingest`` persists it transactionally, and ``inspect`` reports what is already
stored. Output is deliberately terse and structured, and never includes the database DSN or
any other credential material.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.clock import SystemClock
from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.errors import QuantPlatformError
from quantplatform.core.models.market import MarketBar
from quantplatform.data.ingestion import IngestionService
from quantplatform.data.results import IngestionResult
from quantplatform.data.timeframes import missing_open_times, parse_timeframe
from quantplatform.monitoring.publishers import LoggingEventPublisher
from quantplatform.storage.repository import SqlAlchemyMarketBarRepository
from quantplatform.storage.session import create_engine, create_session_factory
from quantplatform.storage.unit_of_work import SqlAlchemyDataUnitOfWork

__all__ = ["app"]

app = typer.Typer(
    name="data",
    help="Historical market-data validation, ingestion and inspection.",
    no_args_is_help=True,
)

EXIT_FATAL = 1
EXIT_CONFIGURATION_ERROR = 2

_FileOption = Annotated[
    Path,
    typer.Option("--file", exists=True, dir_okay=False, readable=True, help="CSV file path."),
]
_SymbolOption = Annotated[str, typer.Option("--symbol", help="Canonical symbol, e.g. BTC/USDT.")]
_TimeframeOption = Annotated[str, typer.Option("--timeframe", help="Timeframe, e.g. 1h.")]
_MarketTypeOption = Annotated[str, typer.Option("--market-type", help="Market type.")]
_SourceOption = Annotated[
    str | None,
    typer.Option("--source-id", help="Logical source identifier; defaults to configuration."),
]
_HistoricalEndOption = Annotated[
    datetime | None,
    typer.Option(
        "--historical-end",
        help=(
            "ISO-8601 instant with offset that a historical backfill is judged against, so "
            "an intentionally old dataset is not reported as stale."
        ),
    ),
]


def _load() -> Settings:
    """Load configuration, exiting with a distinct code when it is unusable.

    Raises:
        typer.Exit: With :data:`EXIT_CONFIGURATION_ERROR` when configuration is invalid.
    """
    try:
        return load_settings()
    except QuantPlatformError as exc:
        typer.echo(json.dumps(exc.to_dict(), default=str), err=True)
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc


def _parse_market_type(value: str) -> MarketType:
    """Parse a market type, exiting with a clear message when it is unknown.

    Raises:
        typer.Exit: With :data:`EXIT_CONFIGURATION_ERROR` when the value is not a market type.
    """
    try:
        return MarketType(value)
    except ValueError as exc:
        typer.echo(
            f"unknown market type {value!r}; expected one of "
            f"{', '.join(member.value for member in MarketType)}",
            err=True,
        )
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc


def _parse_timeframe_or_exit(value: str) -> Timeframe:
    """Parse a timeframe, exiting with a clear message when it is unsupported.

    Raises:
        typer.Exit: With :data:`EXIT_CONFIGURATION_ERROR` when the value is unsupported.
    """
    try:
        return parse_timeframe(value)
    except QuantPlatformError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc


def _summarise(result: IngestionResult, *, persisted: bool) -> dict[str, object]:
    """Build a credential-free summary of an ingestion attempt."""
    run = result.run
    return {
        "run_id": str(run.run_id),
        "status": run.status.value,
        "persisted": persisted,
        "source_checksum": run.source_checksum,
        "configuration_hash": run.configuration_hash,
        "rows": {
            "total": run.total_source_rows,
            "valid": run.valid_rows,
            "rejected": run.rejected_rows,
        },
        "bars": {
            "inserted": run.inserted_bars,
            "exact_duplicates": run.exact_duplicate_bars,
            "conflicting": run.conflicting_duplicate_bars,
        },
        "findings": {
            "info": run.info_finding_count,
            "warning": run.warning_finding_count,
            "error": run.error_finding_count,
            "fatal": run.fatal_finding_count,
        },
        "error_summary": run.error_summary,
        "detail": [
            {
                "severity": finding.severity.value,
                "code": finding.code.value,
                "row": finding.source_row,
                "message": finding.message,
            }
            for finding in result.findings
        ],
    }


async def _ingest(
    *,
    settings: Settings,
    path: Path,
    symbol: str,
    market_type: MarketType,
    timeframe: Timeframe,
    source_id: str | None,
    historical_end: datetime | None,
    dry_run: bool,
) -> IngestionResult:
    """Run one ingestion attempt against a freshly built engine."""
    engine = create_engine(settings.database)
    try:
        session_factory = create_session_factory(engine)
        service = IngestionService(
            unit_of_work_factory=lambda: SqlAlchemyDataUnitOfWork(session_factory),
            clock=SystemClock(),
            publisher=LoggingEventPublisher(),
            settings=settings.data,
        )
        return await service.ingest(
            path,
            symbol=symbol,
            market_type=market_type,
            timeframe=timeframe,
            source_id=source_id,
            historical_end=historical_end,
            dry_run=dry_run,
        )
    finally:
        await engine.dispose()


@app.command()
def validate(
    file: _FileOption,
    symbol: _SymbolOption = "BTC/USDT",
    timeframe: _TimeframeOption = "1h",
    market_type: _MarketTypeOption = "spot",
    source_id: _SourceOption = None,
    historical_end: _HistoricalEndOption = None,
) -> None:
    """Validate a CSV file without persisting anything.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when the file would fail ingestion.
    """
    settings = _load()
    result = asyncio.run(
        _ingest(
            settings=settings,
            path=file,
            symbol=symbol,
            market_type=_parse_market_type(market_type),
            timeframe=_parse_timeframe_or_exit(timeframe),
            source_id=source_id,
            historical_end=historical_end,
            dry_run=True,
        )
    )
    typer.echo(json.dumps(_summarise(result, persisted=False), indent=2, default=str))
    if not result.succeeded:
        raise typer.Exit(code=EXIT_FATAL)


@app.command()
def ingest(
    file: _FileOption,
    symbol: _SymbolOption = "BTC/USDT",
    timeframe: _TimeframeOption = "1h",
    market_type: _MarketTypeOption = "spot",
    source_id: _SourceOption = None,
    historical_end: _HistoricalEndOption = None,
) -> None:
    """Validate and transactionally persist a CSV file.

    Raises:
        typer.Exit: With :data:`EXIT_FATAL` when ingestion failed and nothing was persisted.
    """
    settings = _load()
    result = asyncio.run(
        _ingest(
            settings=settings,
            path=file,
            symbol=symbol,
            market_type=_parse_market_type(market_type),
            timeframe=_parse_timeframe_or_exit(timeframe),
            source_id=source_id,
            historical_end=historical_end,
            dry_run=False,
        )
    )
    typer.echo(json.dumps(_summarise(result, persisted=result.succeeded), indent=2, default=str))
    if not result.succeeded:
        raise typer.Exit(code=EXIT_FATAL)


async def _load_stored_bars(
    *,
    settings: Settings,
    symbol: str,
    market_type: MarketType,
    timeframe: Timeframe,
) -> tuple[int, MarketBar | None, list[datetime]]:
    """Return the stored bar count, the latest bar, and every stored open time."""
    engine = create_engine(settings.database)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            repository = SqlAlchemyMarketBarRepository(session)
            count = await repository.count_bars(
                symbol=symbol, market_type=market_type, timeframe=timeframe
            )
            latest = await repository.get_latest_bar(
                symbol=symbol, market_type=market_type, timeframe=timeframe
            )
            open_times: list[datetime] = []
            if latest is not None:
                bars = await repository.get_bars(
                    symbol=symbol,
                    market_type=market_type,
                    timeframe=timeframe,
                    start=datetime.min.replace(tzinfo=latest.open_time.tzinfo),
                    end=latest.open_time + timeframe.duration,
                )
                open_times = [bar.open_time for bar in bars]
            return count, latest, open_times
    finally:
        await engine.dispose()


@app.command()
def inspect(
    symbol: _SymbolOption = "BTC/USDT",
    timeframe: _TimeframeOption = "1h",
    market_type: _MarketTypeOption = "spot",
) -> None:
    """Report what is stored for a symbol: count, range and gap summary."""
    settings = _load()
    resolved_timeframe = _parse_timeframe_or_exit(timeframe)
    count, latest, open_times = asyncio.run(
        _load_stored_bars(
            settings=settings,
            symbol=symbol,
            market_type=_parse_market_type(market_type),
            timeframe=resolved_timeframe,
        )
    )

    gaps = missing_open_times(open_times, resolved_timeframe) if open_times else ()
    typer.echo(
        json.dumps(
            {
                "symbol": symbol,
                "market_type": market_type,
                "timeframe": resolved_timeframe.value,
                "bar_count": count,
                "first_open_time": open_times[0].isoformat() if open_times else None,
                "last_open_time": latest.open_time.isoformat() if latest else None,
                "last_close_time": latest.close_time.isoformat() if latest else None,
                "gap_runs": len(gaps),
                "missing_bars": sum(run.count for run in gaps),
                "gaps": [
                    {
                        "from": run.first_missing_open_time.isoformat(),
                        "to": run.last_missing_open_time.isoformat(),
                        "count": run.count,
                    }
                    for run in gaps
                ],
            },
            indent=2,
            default=str,
        )
    )
