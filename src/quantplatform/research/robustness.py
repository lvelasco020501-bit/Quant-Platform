"""Summarising many comparable results without choosing between them.

Sensitivity, regime and stress all produce the same shape of question: here are several runs
that are meant to be looked at *together* — how spread out are they, how many were positive,
how bad was the worst? None of that is a ranking. A median is not a winner, and reporting the
worst alongside the best is the opposite of hiding it.

This module exists so that question is answered once. Three separate implementations would be
three separate places a "just pick the best one" shortcut could quietly appear later, reviewed
with a third of the scrutiny any one of them would get alone.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from decimal import Decimal

from quantplatform.core.constants import ZERO
from quantplatform.core.models.base import DomainModel
from quantplatform.core.numeric import Money
from quantplatform.research.ledger import LedgerEntry
from quantplatform.research.result import ExperimentResult, ExperimentStatus

__all__ = ["DistributionSummary", "VariationRun", "summarise_variations"]

_MINIMUM_FOR_DISPERSION = 2


class VariationRun(DomainModel):
    """One result and the ledger line that recorded it.

    The same shape as walk-forward's ``FoldRun``, under a different name: a sensitivity
    variant, a regime episode and a stress scenario are not folds — none of them are indexed,
    none of them are contiguous — but each is, just the same, a result whose membership in one
    sweep needs demonstrating rather than assuming.
    """

    entry: LedgerEntry
    result: ExperimentResult


class DistributionSummary(DomainModel):
    """How a set of comparable results looked, collectively and at their extremes.

    Every field here is a fact about the *set*: nowhere does this model say which member of
    the set produced ``min_return`` or ``max_return`` — reporting that would hand back exactly
    the one-line shortcut ("this is the config that did best") this module exists to refuse.
    Read a specific run's own attempt to find out what it did; read this to find out how
    stable the family of runs was.
    """

    count_total: int
    count_completed: int
    count_failed: int

    median_return: Money | None = None
    min_return: Money | None = None
    max_return: Money | None = None

    median_max_drawdown: Money | None = None
    min_max_drawdown: Money | None = None
    max_max_drawdown: Money | None = None

    positive_share_of_completed: Money | None = None
    """Share of *completed* runs whose return was above zero. The denominator is completed
    runs, not the full count — see :attr:`count_failed` to compute the other fraction."""

    expectancy_dispersion: Money | None = None
    """Spread of per-run expectancy; ``None`` below two completed runs, where spread is
    undefined rather than zero."""

    r_stability: Money | None = None
    """Spread of per-run average R, under the same rule."""


def _median(values: Sequence[Decimal]) -> Decimal | None:
    return statistics.median(values) if values else None


def _dispersion(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < _MINIMUM_FOR_DISPERSION:
        return None
    return Decimal(str(statistics.pstdev([float(value) for value in values])))


def summarise_variations(
    runs: Sequence[VariationRun], *, expected_plan_id: str
) -> DistributionSummary:
    """Summarise a set of runs that all belong to one sensitivity, regime or stress plan.

    Membership is checked, not assumed — the same discipline
    :func:`~quantplatform.research.aggregate.aggregate_walk_forward` applies to folds. A
    result belonging to a different plan would still produce a plausible-looking summary,
    which is exactly why it is refused rather than silently included.

    Args:
        runs: Each run's ledger line and result.
        expected_plan_id: The one plan every run must belong to.

    Returns:
        The summary. Failed runs are counted and excluded from the arithmetic, never dropped
        from the record and never treated as a return of zero.

    Raises:
        ValueError: If the runs are empty, or any of them names a different
            ``variation_plan_id`` (or none at all).
    """
    if not runs:
        msg = "a distribution summary needs at least one run"
        raise ValueError(msg)
    mismatched = [run for run in runs if run.entry.variation_plan_id != expected_plan_id]
    if mismatched:
        msg = "every run must belong to the plan being summarised"
        raise ValueError(msg, {"expected_plan_id": expected_plan_id})

    results = [run.result for run in runs]
    completed = [
        result
        for result in results
        if result.status is ExperimentStatus.SUCCEEDED and result.performance is not None
    ]
    failed = len(results) - len(completed)

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
        positive = sum(1 for value in returns if value > ZERO)
        share = Decimal(positive) / Decimal(len(completed))

    return DistributionSummary(
        count_total=len(results),
        count_completed=len(completed),
        count_failed=failed,
        median_return=_median(returns),
        min_return=min(returns) if returns else None,
        max_return=max(returns) if returns else None,
        median_max_drawdown=_median(drawdowns),
        min_max_drawdown=min(drawdowns) if drawdowns else None,
        max_max_drawdown=max(drawdowns) if drawdowns else None,
        positive_share_of_completed=share,
        expectancy_dispersion=_dispersion(expectancies),
        r_stability=_dispersion(multiples),
    )
