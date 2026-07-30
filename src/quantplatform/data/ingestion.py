"""The historical market-data ingestion service.

Orchestrates the whole pipeline::

    CSV source -> checksum -> raw parsing -> validation -> findings -> normalisation
               -> duplicate/revision comparison -> persistence -> ingestion summary

Every dependency is injected, time comes only from the injected
:class:`~quantplatform.core.clock.Clock`, and persistence goes through the
:class:`~quantplatform.core.interfaces.DataUnitOfWork` port, so the whole service runs
deterministically under a simulated clock and an in-memory unit of work.

Two persistence guarantees shape the control flow:

* **No partial bars on failure.** Bars and the run record are written inside one
  transaction. A fatal finding, or any exception during the write, means that transaction
  is never committed, so not a single bar lands.
* **A failed attempt is still auditable.** After a failure the service opens a second,
  independent transaction and records the failed run together with its findings, so the
  reason a load produced nothing is durable even though its data was discarded.

This service knows nothing about strategies, risk, orders or portfolio state, and never
imports those packages.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from quantplatform import __version__
from quantplatform.config.settings import DataSettings
from quantplatform.core.clock import Clock
from quantplatform.core.enums import (
    BarWriteOutcome,
    DataQualityIssue,
    FindingSeverity,
    IngestionStatus,
    MarketType,
    Timeframe,
)
from quantplatform.core.errors import DataError
from quantplatform.core.events import (
    DataQualityIssueDetected,
    IngestionCompleted,
    IngestionFailed,
    IngestionStarted,
)
from quantplatform.core.ids import deterministic_uuid
from quantplatform.core.interfaces import DataUnitOfWork, EventPublisher
from quantplatform.core.logging_config import get_logger, log_context
from quantplatform.core.models.data import DataQualityFinding, IngestionRun
from quantplatform.core.models.market import MarketBar
from quantplatform.data.closed_candle import ClosedCandlePolicy
from quantplatform.data.csv_loader import compute_checksum, load_csv_records
from quantplatform.data.findings import FindingRecorder
from quantplatform.data.normalization import normalize_source
from quantplatform.data.results import IngestionResult
from quantplatform.data.validation import (
    DatasetExpectations,
    DatasetValidator,
    RecordValidator,
)

__all__ = ["IngestionService"]

_LOGGER = get_logger(__name__)
_EVENT_SOURCE = "data.ingestion"

_UNREADABLE_CHECKSUM = "0" * 64
"""Placeholder recorded when the source could not be read at all.

The run record still needs a checksum-shaped value to remain well-formed, and an all-zero
digest is unmistakably not a real SHA-256 of any content.
"""


@dataclass(frozen=True, slots=True)
class _RunContext:
    """The identity and provenance of one ingestion attempt, fixed before it begins."""

    run_id: UUID
    path: Path
    started_at: datetime
    source_id: str
    checksum: str
    configuration_hash: str
    expectations: DatasetExpectations
    reference_time: datetime | None
    dry_run: bool


@dataclass(frozen=True, slots=True)
class _RowCounts:
    """How the source's rows were accounted for."""

    total: int = 0
    valid: int = 0
    rejected: int = 0


@dataclass(frozen=True, slots=True)
class _WriteCounts:
    """How the normalised bars were accounted for at the storage boundary."""

    inserted: int = 0
    exact_duplicates: int = 0
    conflicting: int = 0
    written: tuple[MarketBar, ...] = ()


class IngestionService:
    """Loads a historical CSV file into normalised, persisted market bars.

    Args:
        unit_of_work_factory: Produces a fresh transactional scope. A factory rather than a
            single instance because a failed attempt needs a second, independent
            transaction to record its own failure after the first has been discarded.
        clock: Sole source of time.
        publisher: Receives the ingestion lifecycle and data-quality events.
        settings: Data-layer configuration.
    """

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], DataUnitOfWork],
        clock: Clock,
        publisher: EventPublisher,
        settings: DataSettings,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._publisher = publisher
        self._settings = settings

    async def ingest(
        self,
        path: Path,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        source_id: str | None = None,
        historical_end: datetime | None = None,
        dry_run: bool = False,
    ) -> IngestionResult:
        """Validate, normalise and persist one historical CSV file.

        Args:
            path: CSV file to ingest.
            symbol: Canonical symbol every record must declare.
            market_type: Market every record must declare.
            timeframe: Timeframe every record must declare.
            source_id: Logical source identifier; defaults to the configured one.
            historical_end: Reference instant for closure and staleness. Supply this for a
                backfill so the import is judged against when the data was captured rather
                than against today.
            dry_run: When ``True``, validate and normalise but persist nothing at all —
                neither bars nor the run record. Backs the ``validate`` command.

        Returns:
            The run record, every finding, and the bars actually written.
        """
        started_at = self._clock.now()
        resolved_source = source_id or self._settings.default_source_id
        reference_time = historical_end or self._settings.historical_backfill_end
        expectations = DatasetExpectations(
            symbol=symbol, market_type=market_type, timeframe=timeframe
        )
        configuration_hash = self._configuration_hash(expectations, reference_time)

        try:
            checksum = compute_checksum(path)
        except DataError:
            # The read failure is reported as a fatal finding by the pipeline below; here it
            # only needs to not prevent the run id from being derived.
            checksum = _UNREADABLE_CHECKSUM

        context = _RunContext(
            # An ingestion run is an event, not a derived decision, so its id is drawn
            # rather than content-addressed. Deriving it from the source, configuration and
            # start instant would make two ingestions of the same file at the same instant
            # collide — which a coarse or simulated clock makes entirely reachable — and
            # would also defeat the requirement that repeated ingestion of one file stay
            # separately traceable, since the second attempt could only overwrite the
            # first. What must stay reproducible is the *content*: identical input yields
            # identical bars, findings, counts and status, and finding ids remain derived
            # from the run id so they are stable within an attempt and unique across them.
            run_id=uuid4(),
            path=path,
            started_at=started_at,
            source_id=resolved_source,
            checksum=checksum,
            configuration_hash=configuration_hash,
            expectations=expectations,
            reference_time=reference_time,
            dry_run=dry_run,
        )

        with log_context(
            ingestion_run_id=str(context.run_id),
            symbol=symbol,
            source=resolved_source,
        ):
            return await self._run(context)

    async def _run(self, context: _RunContext) -> IngestionResult:
        """Execute one ingestion attempt and persist whatever it is safe to persist."""
        recorder = FindingRecorder(
            run_id=context.run_id,
            source=context.source_id,
            clock=self._clock,
            symbol=context.expectations.symbol,
            timeframe=context.expectations.timeframe,
        )

        await self._publisher.publish(
            IngestionStarted(
                event_id=deterministic_uuid("event", "ingestion_started", str(context.run_id)),
                occurred_at=context.started_at,
                source=_EVENT_SOURCE,
                correlation_id=context.run_id,
                run_id=context.run_id,
                source_id=context.source_id,
                source_path=str(context.path),
                expected_symbol=context.expectations.symbol,
                expected_market_type=context.expectations.market_type,
                expected_timeframe=context.expectations.timeframe,
            )
        )
        _LOGGER.info("ingestion_started", extra={"path": str(context.path)})

        try:
            source = load_csv_records(
                context.path,
                source_id=context.source_id,
                delimiter=self._settings.csv_delimiter,
                encoding=self._settings.csv_encoding,
            )
        except DataError as exc:
            recorder.record(
                DataQualityIssue.MALFORMED_RECORD,
                FindingSeverity.FATAL,
                f"source could not be read: {exc.message}",
                context={key: str(value) for key, value in exc.details.items()},
            )
            return await self._finish_failed(
                context=context,
                recorder=recorder,
                counts=_RowCounts(),
                error_summary=exc.message,
            )

        policy = ClosedCandlePolicy(
            clock=self._clock,
            grace_period=timedelta(seconds=self._settings.close_grace_period_seconds),
            reference_time=context.reference_time,
        )
        normalization = normalize_source(
            source.records,
            record_validator=RecordValidator(
                expectations=context.expectations,
                closed_candle_policy=policy,
                recorder=recorder,
            ),
            dataset_validator=DatasetValidator(
                expectations=context.expectations,
                recorder=recorder,
                gap_severity=self._settings.gap_severity,
                max_allowed_gap_bars=self._settings.max_allowed_gap_bars,
                staleness_budget=self._staleness_budget(context.expectations.timeframe),
                reference_time=policy.reference_now(),
            ),
            recorder=recorder,
            source=context.source_id,
        )
        counts = _RowCounts(
            total=normalization.total_source_rows,
            valid=normalization.valid_rows,
            rejected=normalization.rejected_rows,
        )

        if recorder.has_fatal:
            return await self._finish_failed(
                context=context,
                recorder=recorder,
                counts=counts,
                error_summary="ingestion halted by a fatal data-quality finding",
            )

        return await self._finish_successful(
            context=context,
            recorder=recorder,
            bars=normalization.bars,
            counts=counts,
        )

    async def _finish_successful(
        self,
        *,
        context: _RunContext,
        recorder: FindingRecorder,
        bars: Sequence[MarketBar],
        counts: _RowCounts,
    ) -> IngestionResult:
        """Write the bars and the run in one transaction, or nothing at all."""
        if context.dry_run:
            run = self._build_run(
                context=context,
                recorder=recorder,
                status=self._status_for(recorder),
                counts=counts,
                writes=_WriteCounts(),
                error_summary=None,
            )
            return await self._announce_completion(run, recorder)

        try:
            async with self._unit_of_work_factory() as unit_of_work:
                writes = await self._write_bars(
                    unit_of_work=unit_of_work,
                    bars=bars,
                    recorder=recorder,
                )
                run = self._build_run(
                    context=context,
                    recorder=recorder,
                    status=self._status_for(recorder),
                    counts=counts,
                    writes=writes,
                    error_summary=None,
                )
                await unit_of_work.runs.record_run(run, recorder.findings)
                await unit_of_work.commit()
        except Exception as exc:
            _LOGGER.exception("ingestion_persistence_failed")
            recorder.record(
                DataQualityIssue.MALFORMED_RECORD,
                FindingSeverity.FATAL,
                f"persistence failed and every bar from this run was rolled back: {exc}",
                context={"error_type": type(exc).__name__},
            )
            return await self._finish_failed(
                context=context,
                recorder=recorder,
                counts=counts,
                error_summary=f"persistence failed: {type(exc).__name__}",
            )

        _LOGGER.info(
            "ingestion_completed",
            extra={
                "status": run.status.value,
                "inserted_bars": run.inserted_bars,
                "rejected_rows": run.rejected_rows,
            },
        )
        return await self._announce_completion(run, recorder, written=writes.written)

    async def _finish_failed(
        self,
        *,
        context: _RunContext,
        recorder: FindingRecorder,
        counts: _RowCounts,
        error_summary: str,
    ) -> IngestionResult:
        """Record a failed run in its own transaction, having persisted no bars."""
        run = self._build_run(
            context=context,
            recorder=recorder,
            status=IngestionStatus.FAILED,
            counts=counts,
            writes=_WriteCounts(),
            error_summary=error_summary,
        )

        if not context.dry_run:
            try:
                async with self._unit_of_work_factory() as unit_of_work:
                    await unit_of_work.runs.record_run(run, recorder.findings)
                    await unit_of_work.commit()
            except Exception:
                # Provenance is best-effort once the run has already failed: losing the
                # audit row must not replace the original failure with a different one.
                _LOGGER.exception("failed_run_could_not_be_recorded")

        await self._publish_findings(recorder.findings, context.run_id)
        await self._publisher.publish(
            IngestionFailed(
                event_id=deterministic_uuid("event", "ingestion_failed", str(context.run_id)),
                occurred_at=run.completed_at,
                source=_EVENT_SOURCE,
                correlation_id=context.run_id,
                run=run,
            )
        )
        _LOGGER.warning("ingestion_failed", extra={"error_summary": error_summary})
        return IngestionResult(run=run, findings=recorder.findings, bars_written=())

    async def _announce_completion(
        self,
        run: IngestionRun,
        recorder: FindingRecorder,
        written: tuple[MarketBar, ...] = (),
    ) -> IngestionResult:
        """Publish the findings and completion event for a successful run."""
        await self._publish_findings(recorder.findings, run.run_id)
        await self._publisher.publish(
            IngestionCompleted(
                event_id=deterministic_uuid("event", "ingestion_completed", str(run.run_id)),
                occurred_at=run.completed_at,
                source=_EVENT_SOURCE,
                correlation_id=run.run_id,
                run=run,
            )
        )
        return IngestionResult(run=run, findings=recorder.findings, bars_written=written)

    async def _write_bars(
        self,
        *,
        unit_of_work: DataUnitOfWork,
        bars: Sequence[MarketBar],
        recorder: FindingRecorder,
    ) -> _WriteCounts:
        """Write bars in batches and turn each conflict into a finding."""
        inserted = 0
        exact_duplicates = 0
        conflicting = 0
        written: list[MarketBar] = []

        batch_size = self._settings.batch_size
        for start in range(0, len(bars), batch_size):
            batch = bars[start : start + batch_size]
            for result in await unit_of_work.bars.add_bars(batch):
                if result.outcome is BarWriteOutcome.INSERTED:
                    inserted += 1
                    written.append(result.bar)
                elif result.outcome is BarWriteOutcome.EXACT_DUPLICATE:
                    exact_duplicates += 1
                else:
                    conflicting += 1
                    self._record_conflict(recorder, result.bar, result.existing_bar)

        return _WriteCounts(
            inserted=inserted,
            exact_duplicates=exact_duplicates,
            conflicting=conflicting,
            written=tuple(written),
        )

    def _record_conflict(
        self,
        recorder: FindingRecorder,
        incoming: MarketBar,
        existing: MarketBar | None,
    ) -> None:
        """Record that an incoming bar disagreed with one already stored."""
        context = {
            "incoming_open": str(incoming.open),
            "incoming_high": str(incoming.high),
            "incoming_low": str(incoming.low),
            "incoming_close": str(incoming.close),
            "incoming_volume": str(incoming.volume),
        }
        if existing is not None:
            context |= {
                "stored_open": str(existing.open),
                "stored_high": str(existing.high),
                "stored_low": str(existing.low),
                "stored_close": str(existing.close),
                "stored_volume": str(existing.volume),
            }
        recorder.record(
            DataQualityIssue.REVISED_BAR,
            FindingSeverity.WARNING,
            "incoming bar conflicts with the stored bar for the same open time; the stored "
            "version was preserved and the incoming values were not written",
            open_time=incoming.open_time,
            context=context,
        )

    async def _publish_findings(
        self,
        findings: Sequence[DataQualityFinding],
        run_id: UUID,
    ) -> None:
        """Publish one event per finding, correlated to the run that raised it."""
        await self._publisher.publish_many(
            [
                DataQualityIssueDetected(
                    event_id=deterministic_uuid("event", "data_quality", str(finding.finding_id)),
                    occurred_at=finding.detected_at,
                    source=_EVENT_SOURCE,
                    correlation_id=run_id,
                    finding=finding,
                )
                for finding in findings
            ]
        )

    def _build_run(
        self,
        *,
        context: _RunContext,
        recorder: FindingRecorder,
        status: IngestionStatus,
        counts: _RowCounts,
        writes: _WriteCounts,
        error_summary: str | None,
    ) -> IngestionRun:
        """Assemble the provenance record for this attempt."""
        return IngestionRun(
            run_id=context.run_id,
            source_id=context.source_id,
            source_path=str(context.path),
            source_checksum=context.checksum,
            expected_symbol=context.expectations.symbol,
            expected_market_type=context.expectations.market_type,
            expected_timeframe=context.expectations.timeframe,
            started_at=context.started_at,
            completed_at=self._clock.now(),
            status=status,
            total_source_rows=counts.total,
            valid_rows=counts.valid,
            rejected_rows=counts.rejected,
            inserted_bars=writes.inserted,
            exact_duplicate_bars=writes.exact_duplicates,
            conflicting_duplicate_bars=writes.conflicting,
            info_finding_count=recorder.count(FindingSeverity.INFO),
            warning_finding_count=recorder.count(FindingSeverity.WARNING),
            error_finding_count=recorder.count(FindingSeverity.ERROR),
            fatal_finding_count=recorder.count(FindingSeverity.FATAL),
            configuration_hash=context.configuration_hash,
            application_version=__version__,
            error_summary=error_summary,
        )

    def _status_for(self, recorder: FindingRecorder) -> IngestionStatus:
        """Derive the terminal status from the findings raised."""
        if recorder.findings:
            return IngestionStatus.SUCCEEDED_WITH_FINDINGS
        return IngestionStatus.SUCCEEDED

    def _staleness_budget(self, timeframe: Timeframe) -> timedelta:
        """Return how old the newest candle may be before it counts as stale."""
        multiplier = self._settings.max_data_age_multiplier
        return timedelta(seconds=int(timeframe.seconds * multiplier))

    def _configuration_hash(
        self,
        expectations: DatasetExpectations,
        reference_time: datetime | None,
    ) -> str:
        """Return a stable digest of every setting that affects this run's outcome.

        Two runs over an identical file can still legitimately differ — a changed grace
        period or gap threshold changes what is accepted — so provenance records the
        configuration alongside the source checksum rather than treating the checksum alone
        as identifying.
        """
        payload = {
            "symbol": expectations.symbol,
            "market_type": expectations.market_type.value,
            "timeframe": expectations.timeframe.value,
            "csv_delimiter": self._settings.csv_delimiter,
            "csv_encoding": self._settings.csv_encoding,
            "close_grace_period_seconds": self._settings.close_grace_period_seconds,
            "gap_severity": self._settings.gap_severity.value,
            "max_allowed_gap_bars": self._settings.max_allowed_gap_bars,
            "max_data_age_multiplier": str(self._settings.max_data_age_multiplier),
            "reference_time": reference_time.isoformat() if reference_time else None,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
