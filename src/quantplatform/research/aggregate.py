"""Consolidating a plan's folds without losing the ones that went badly.

Two temptations shape this module and both are refused. A single headline number would be the
thing everyone optimises within a week, so there isn't one. And a fold that blew up is not a
fold that broke even — turning failures into returns of zero would drag every median toward a
figure nothing measured, so failures are counted, kept, and excluded from the arithmetic.

Excluding them creates its own trap: eight failures and two winners would read as "100%
positive". The share therefore carries its denominator in its own name, and the counts sit
beside it so the other fraction is one subtraction away.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from decimal import Decimal

from pydantic import Field

from quantplatform.core.constants import ZERO
from quantplatform.core.models.base import DomainModel
from quantplatform.core.numeric import Money
from quantplatform.research.definition import ExperimentRole
from quantplatform.research.ledger import LedgerEntry
from quantplatform.research.result import ExperimentResult, ExperimentStatus

__all__ = ["FoldRun", "WalkForwardSummary", "aggregate_walk_forward"]

_MINIMUM_FOR_DISPERSION = 2


class FoldRun(DomainModel):
    """One fold's evidence: the ledger line proving where it came from, and its result.

    A bare list of results was trusted to belong together. Two plans handed in at once would
    have produced a summary of neither, and the numbers would have looked exactly as
    convincing as a correct one. Membership is now demonstrated rather than assumed.
    """

    entry: LedgerEntry
    result: ExperimentResult


class WalkForwardSummary(DomainModel):
    """What a plan's test folds looked like, collectively and at their worst."""

    folds_total: int = Field(default=0, ge=0)
    folds_completed: int = Field(default=0, ge=0)
    folds_failed: int = Field(default=0, ge=0)

    median_return: Money | None = None
    worst_return: Money | None = None
    """Reported alongside the median rather than smoothed into it. A plan whose typical fold
    is fine and whose worst is ruinous is not a plan that is fine."""

    median_max_drawdown: Money | None = None
    worst_max_drawdown: Money | None = None

    positive_share_of_completed: Money | None = None
    """Share of *completed* folds that returned above zero.

    The denominator is in the name deliberately. Eight failures and two winners would be
    "100% positive" under any shorter name, and :attr:`folds_failed` sits beside this so the
    reader can compute the honest fraction without being told which one to prefer.
    """

    expectancy_dispersion: Money | None = None
    """Spread of per-fold expectancy; ``None`` below two folds, where spread is undefined
    rather than zero."""

    r_stability: Money | None = None
    """Spread of per-fold average R, under the same rule."""


def _median(values: Sequence[Decimal]) -> Decimal | None:
    return statistics.median(values) if values else None


def _dispersion(values: Sequence[Decimal]) -> Decimal | None:
    """Return the population spread, or ``None`` when there is not enough to spread."""
    if len(values) < _MINIMUM_FOR_DISPERSION:
        return None
    return Decimal(str(statistics.pstdev([float(value) for value in values])))


def aggregate_walk_forward(
    runs: Sequence[FoldRun], *, require_complete: bool = True
) -> WalkForwardSummary:
    """Summarise the test folds of one walk-forward plan.

    Membership is checked, not assumed. Every rejection below describes a set of results that
    would still have produced a plausible-looking summary, which is exactly why each is an
    error rather than a warning.

    Args:
        runs: Each fold's ledger line and result. Training folds are ignored — a training
            window is an observation window in this phase, and including it would report the
            same calendar twice under two names. Failures are counted and excluded from the
            arithmetic rather than dropped from the record.
        require_complete: Whether every fold index from zero must be present. Seven folds of a
            ten-fold plan is a different claim from the plan's result, and saying so out loud
            is the price of making it.

    Returns:
        The summary, with no single score: a headline number is the thing everyone optimises
        within a week.

    Raises:
        ValueError: If the runs span more than one plan, repeat a fold index, carry no
            lineage, include a training fold, leave a hole in the plan while
            ``require_complete``, or are empty.
    """
    if not runs:
        msg = "a walk-forward summary needs at least one fold"
        raise ValueError(msg)
    _verify_membership(runs, require_complete=require_complete)
    results = [run.result for run in runs]
    folds = [
        result for result in results if result.definition.role is ExperimentRole.WALK_FORWARD_TEST
    ]
    completed = [
        result
        for result in folds
        if result.status is ExperimentStatus.SUCCEEDED and result.performance is not None
    ]
    returns = [
        result.performance.total_return
        for result in completed
        if result.performance is not None and result.performance.total_return is not None
    ]
    drawdowns = [
        result.performance.max_drawdown for result in completed if result.performance is not None
    ]
    expectancies = [
        result.performance.trades.expectancy
        for result in completed
        if result.performance is not None and result.performance.trades.expectancy is not None
    ]
    multiples = [
        result.performance.trades.average_r
        for result in completed
        if result.performance is not None and result.performance.trades.average_r is not None
    ]

    share: Decimal | None = None
    if completed:
        with_positive = sum(1 for value in returns if value > ZERO)
        share = Decimal(with_positive) / Decimal(len(completed))

    return WalkForwardSummary(
        folds_total=len(folds),
        folds_completed=len(completed),
        folds_failed=len(folds) - len(completed),
        median_return=_median(returns),
        worst_return=min(returns) if returns else None,
        median_max_drawdown=_median(drawdowns),
        worst_max_drawdown=max(drawdowns) if drawdowns else None,
        positive_share_of_completed=share,
        expectancy_dispersion=_dispersion(expectancies),
        r_stability=_dispersion(multiples),
    )


def _verify_membership(runs: Sequence[FoldRun], *, require_complete: bool) -> None:
    """Check the runs are the test folds of exactly one plan.

    Raises:
        ValueError: Naming which of the conditions failed, because "these do not belong
            together" is not actionable and "fold 3 appears twice" is.
    """
    if any(run.entry.plan_id is None or run.entry.fold_index is None for run in runs):
        msg = "every fold must carry the lineage that proves which plan it belongs to"
        raise ValueError(msg)
    plans = {run.entry.plan_id for run in runs}
    if len(plans) > 1:
        msg = f"a summary describes one plan, not {len(plans)}"
        raise ValueError(msg)
    indices = [run.entry.fold_index for run in runs]
    if len(set(indices)) != len(indices):
        msg = "two folds claim the same index, so one of them is misfiled"
        raise ValueError(msg)
    roles = {run.result.definition.role for run in runs}
    if ExperimentRole.WALK_FORWARD_TEST not in roles:
        msg = "a summary needs the plan's test folds"
        raise ValueError(msg)
    if roles - {ExperimentRole.WALK_FORWARD_TEST}:
        msg = "only test folds belong in a summary; a training window is not a second result"
        raise ValueError(msg)
    if require_complete:
        expected = set(range(len(indices)))
        missing = expected - {index for index in indices if index is not None}
        if missing:
            msg = f"the plan is missing folds {sorted(missing)}"
            raise ValueError(msg)
