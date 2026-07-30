"""Construction and accounting of data-quality findings.

Findings are the audit record of everything the pipeline noticed. They are built through a
recorder rather than ad hoc so that identifiers are deterministic, severity accounting is
consistent, and every finding carries the run, source and row context needed to explain
itself later without re-reading the source file.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from quantplatform.core.clock import Clock
from quantplatform.core.enums import (
    DataQualityIssue,
    FindingSeverity,
    Timeframe,
)
from quantplatform.core.ids import deterministic_uuid
from quantplatform.core.models.data import DataQualityFinding

__all__ = ["FindingRecorder"]

_MAX_MESSAGE_LENGTH = 500
"""Matches the ``Text`` domain alias bound, so a verbose message can never fail validation."""

_TRUNCATION_SUFFIX = "..."


def _fit_message(message: str) -> str:
    """Return a message guaranteed to satisfy the domain's non-empty, bounded text rule."""
    collapsed = " ".join(message.split()) or "unspecified data quality finding"
    if len(collapsed) <= _MAX_MESSAGE_LENGTH:
        return collapsed
    keep = _MAX_MESSAGE_LENGTH - len(_TRUNCATION_SUFFIX)
    return collapsed[:keep] + _TRUNCATION_SUFFIX


class FindingRecorder:
    """Collects findings for one ingestion run and tracks their severity counts.

    The recorder is bound to the run's *expected* symbol and timeframe. A finding about a
    record whose symbol is wrong therefore still files under the symbol the run was
    ingesting, with the offending value in ``context`` — which is what makes findings
    queryable by the instrument the operator cares about.

    Args:
        run_id: Identifier of the ingestion run the findings belong to.
        source: Logical source identifier recorded on every finding.
        clock: Supplies each finding's detection timestamp.
        symbol: The run's expected canonical symbol.
        timeframe: The run's expected timeframe.
    """

    def __init__(
        self,
        *,
        run_id: UUID,
        source: str,
        clock: Clock,
        symbol: str,
        timeframe: Timeframe,
    ) -> None:
        self._run_id = run_id
        self._source = source
        self._clock = clock
        self._symbol = symbol
        self._timeframe = timeframe
        self._findings: list[DataQualityFinding] = []

    def record(
        self,
        code: DataQualityIssue,
        severity: FindingSeverity,
        message: str,
        *,
        source_row: int | None = None,
        open_time: datetime | None = None,
        context: Mapping[str, str] | None = None,
    ) -> DataQualityFinding:
        """Record one finding and return it.

        Args:
            code: Stable machine-readable issue code.
            severity: Governs whether the record is rejected or the run fails.
            message: Human-readable explanation; whitespace-collapsed and bounded.
            source_row: One-based data row in the source file, when applicable.
            open_time: Bar open time the finding concerns, when applicable.
            context: Additional structured, string-valued detail.

        Returns:
            The recorded finding.
        """
        finding = DataQualityFinding(
            finding_id=deterministic_uuid(
                "data_quality_finding",
                str(self._run_id),
                code.value,
                str(len(self._findings)),
            ),
            ingestion_run_id=self._run_id,
            code=code,
            severity=severity,
            message=_fit_message(message),
            source=self._source,
            source_row=source_row,
            symbol=self._symbol,
            timeframe=self._timeframe,
            open_time=open_time,
            context=dict(context or {}),
            detected_at=self._clock.now(),
        )
        self._findings.append(finding)
        return finding

    @property
    def findings(self) -> tuple[DataQualityFinding, ...]:
        """Return every finding recorded so far, in recording order."""
        return tuple(self._findings)

    def count(self, severity: FindingSeverity) -> int:
        """Return how many findings were recorded at a given severity."""
        return sum(1 for finding in self._findings if finding.severity is severity)

    @property
    def has_fatal(self) -> bool:
        """Return whether any finding fails the entire ingestion run."""
        return any(finding.blocks_ingestion for finding in self._findings)
