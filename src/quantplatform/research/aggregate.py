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
from quantplatform.research.result import ExperimentResult, ExperimentStatus

__all__ = ["WalkForwardSummary", "aggregate_walk_forward"]

_MINIMUM_FOR_DISPERSION = 2


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


def aggregate_walk_forward(results: Sequence[ExperimentResult]) -> WalkForwardSummary:
    """Summarise the test folds of a walk-forward plan.

    Only the test folds. A training window is an observation window in this phase, and
    including it would report the same calendar twice under two names.

    Args:
        results: Every fold's result, in any order. Training folds are ignored; failures are
            counted and excluded from the arithmetic rather than dropped from the record.

    Returns:
        The summary. Deliberately not a score: every field is a separate observation, and a
        single number combining them is the thing that would be optimised within a week.
    """
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
