"""Every experiment that ran, including the ones nobody wants to remember.

This is the whole anti-overfitting mechanism, and it is a counter rather than a prohibition.
Nobody can be stopped from looking at the same question a twentieth time; what can be arranged
is that the twentieth look is on the record, so a result reached on the last attempt cannot be
presented as though it were the first.

Two absences are deliberate. Nothing here deletes or replaces — re-running an experiment adds
a line, and the same identifier appearing repeatedly *is* the finding. And there is no
``best``, no ``rank`` and no ``top``: the distance between "the harness can order these by
profit" and "the harness says this one is good" is one habit, and the honest place to close
that distance is before the method exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from quantplatform.core.models.base import DomainModel, Text, UtcDatetime
from quantplatform.research.result import ExperimentResult, ExperimentStatus, result_hash

__all__ = ["ExperimentLedger", "LedgerEntry"]


class LedgerEntry(DomainModel):
    """One line of the record: an experiment ran, and this is what came of it."""

    experiment_id: Text
    name: Text
    result_hash: Text
    code_revision: Text | None = None
    status: ExperimentStatus
    recorded_at: UtcDatetime


class ExperimentLedger:
    """An append-only record of every experiment attempted."""

    def __init__(self, path: Path) -> None:
        """Point a ledger at its file, which need not exist yet.

        Args:
            path: JSON-lines file the record is appended to. Never rewritten.
        """
        self._path = path

    def record(self, result: ExperimentResult) -> LedgerEntry:
        """Append one experiment to the record and return the line written.

        Failures are recorded exactly like successes. An experiment that blew up is evidence
        about the configuration that blew it up, and a record holding only what worked would
        describe a platform that never had a bad idea.
        """
        entry = LedgerEntry(
            experiment_id=result.experiment_id,
            name=result.definition.name,
            result_hash=result_hash(result),
            code_revision=result.code_revision,
            status=result.status,
            recorded_at=result.finished_at,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
        return entry

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Return every line ever appended, oldest first."""
        if not self._path.exists():
            return ()
        return tuple(
            LedgerEntry.model_validate(json.loads(line))
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def attempts(self, experiment_id: str) -> int:
        """Return how many times this exact experiment has been run.

        The number a reader needs before believing any single result: an outcome found on the
        first attempt and one found on the twelfth are different kinds of evidence, and only
        this count distinguishes them.
        """
        return sum(1 for entry in self.entries() if entry.experiment_id == experiment_id)
