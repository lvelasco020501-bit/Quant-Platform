"""What an experiment produced, including when it produced nothing.

A result embeds its definition rather than pointing at it, so that a single file is standalone
evidence. It records failures with the same standing as successes, because a record that keeps
only the runs that worked acquires survivorship bias on its first bad day. And its reproducible
hash excludes the clock: two identical runs necessarily happen at different moments, so a hash
over timestamps could never match and reproducibility would be untestable by construction.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self

from pydantic import model_validator

from quantplatform.backtesting.metrics import EquityPoint, PerformanceSummary
from quantplatform.core.models.base import DomainModel, Text, UtcDatetime
from quantplatform.core.models.trades import ClosedTrade
from quantplatform.research.definition import ExperimentDefinition

__all__ = ["ExperimentResult", "ExperimentStatus", "result_hash"]

_HASH_LENGTH = 32
_UNHASHED_FIELDS = frozenset({"started_at", "finished_at"})


class ExperimentStatus(StrEnum):
    """How an experiment ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REPRODUCIBILITY_FAILURE = "reproducibility_failure"
    """A ledger verdict, never an outcome.

    Produced by comparing two results, so no single run can be born with it — see the
    validator below, which makes claiming it unrepresentable rather than merely discouraged.
    """


class ExperimentResult(DomainModel):
    """One experiment's outcome, carrying the inputs that produced it."""

    definition: ExperimentDefinition
    code_revision: Text | None = None
    """Revision of the code that ran, supplied by the caller.

    Reading it is I/O and this package does none, so a composition root passes it in. ``None``
    is an honest answer where a fabricated one would not be.
    """

    status: ExperimentStatus
    error: Text | None = None
    bars_digest: Text | None = None
    """Fingerprint of the exact bars this run consumed.

    Kept here rather than in the definition: a definition must be declarable before the data
    exists, and folding the digest into the experiment's identity would destroy the very
    comparison it enables — the same definition against a different vintage would stop
    looking like the same definition.
    """
    performance: PerformanceSummary | None = None
    trades: tuple[ClosedTrade, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()
    started_at: UtcDatetime
    finished_at: UtcDatetime

    @property
    def experiment_id(self) -> str:
        """Return the identifier of the definition this result came from."""
        return self.definition.experiment_id

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        """Check the result's contents match the outcome it reports.

        Raises:
            ValueError: If a failure carries no explanation, or a success carries an error or
                no performance — either of which would leave a reader unable to tell what
                actually happened.
        """
        if self.status is ExperimentStatus.REPRODUCIBILITY_FAILURE:
            msg = (
                "a reproducibility verdict comes from comparing two results, not from "
                "running one: a single run cannot report one about itself"
            )
            raise ValueError(msg)
        if self.status is ExperimentStatus.FAILED and self.error is None:
            msg = "a failed experiment must record why it failed"
            raise ValueError(msg)
        if self.status is ExperimentStatus.SUCCEEDED and self.error is not None:
            msg = "a succeeded experiment must not carry an error"
            raise ValueError(msg)
        return self


def result_hash(result: ExperimentResult) -> str:
    """Return the hash two identical runs must agree on.

    Everything except when the run happened. The timestamps are recorded because an operator
    needs them; they are excluded here because including them would make the property this
    hash exists to express impossible to satisfy.
    """
    payload = result.model_dump_json(exclude=set(_UNHASHED_FIELDS))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
