"""Keeping the evidence, not just the note that says it existed.

A ledger line recorded that an experiment ran and carried a hash of its result. The result
itself was written nowhere, so nothing could be re-read, compared or audited: the record
pointed at evidence that had already been discarded.

An attempt is its own thing. One experiment can be run many times — ``experiment_id`` names
the question, ``result_hash`` names the answer, and neither identifies *this run*, because two
runs that agree share both. The attempt identifier is what lets the record hold them apart,
and what a ledger line points at.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from quantplatform.research.canonical import canonical_json
from quantplatform.research.result import ExperimentResult, result_hash

__all__ = ["ResultStore", "attempt_id"]

_ATTEMPT_ID_LENGTH = 32


def attempt_id(result: ExperimentResult) -> str:
    """Return the identifier of this particular execution.

    Distinct from the experiment and from its answer, and derived from both plus when the run
    happened — which is the only thing that separates two runs that agreed completely. The
    clock is deliberately excluded from :func:`~quantplatform.research.result.result_hash`
    for the opposite reason: that hash exists to be equal across runs, and this one exists to
    differ.
    """
    payload = "|".join(
        (
            result.experiment_id,
            result_hash(result),
            result.started_at.isoformat(),
            result.finished_at.isoformat(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_ATTEMPT_ID_LENGTH]


class ResultStore:
    """Immutable storage for what each attempt produced."""

    def __init__(self, root: Path) -> None:
        """Point a store at its directory, which need not exist yet."""
        self._root = root

    def save(self, result: ExperimentResult) -> str:
        """Write one attempt and return its identifier.

        Failures are stored exactly like successes: an experiment that blew up is evidence
        about the configuration that blew it up, and a store holding only what worked would
        describe a platform that never had a bad idea.

        Raises:
            FileExistsError: If this attempt was already written. Evidence is never rewritten
                — silently replacing an earlier file would destroy the record of what was
                believed at the time, which is the thing an audit trail is for.
        """
        identifier = attempt_id(result)
        path = self._path_for(identifier)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            msg = f"attempt {identifier} is already stored"
            raise FileExistsError(msg)
        path.write_text(canonical_json(result, indent=2), encoding="utf-8")
        return identifier

    def load(self, identifier: str) -> ExperimentResult:
        """Read back one attempt.

        Raises:
            FileNotFoundError: If nothing was stored under this identifier. Reported rather
                than answered with an empty result, which would put a run that never happened
                into a comparison.
        """
        path = self._path_for(identifier)
        if not path.exists():
            msg = f"no stored attempt {identifier}"
            raise FileNotFoundError(msg)
        return ExperimentResult.model_validate_json(path.read_text(encoding="utf-8"))

    def _path_for(self, identifier: str) -> Path:
        """Return where one attempt lives."""
        return self._root / f"{identifier}.json"
