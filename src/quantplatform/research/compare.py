"""Putting results beside each other without deciding between them.

A comparison that sorts by profit has already answered the question it was asked to
illustrate. So the table preserves the order it was handed, offers no way to reorder itself,
and keeps the runs that failed as rows: a comparison that quietly omits what did not work is
a comparison that has started choosing. The neutrality only reaches the edge of this
module — whoever reads the table will form a view, and should — but nothing here does it for
them, and nothing here makes it one keystroke away.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.definition import ExperimentRole
from quantplatform.research.result import ExperimentResult, ExperimentStatus

__all__ = ["COMPARISON_METRICS", "ComparisonRow", "ComparisonTable", "compare"]

COMPARISON_METRICS: Final[tuple[str, ...]] = (
    "total_return",
    "max_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "expectancy",
    "average_r",
    "expectancy_r",
    "profit_factor",
    "win_rate",
    "average_win",
    "average_loss",
    "max_consecutive_losses",
    "turnover",
    "commission_paid",
    "slippage_paid",
    "closed_trades",
    "time_in_market",
)
"""Every column, in a fixed order, the same for every row.

Named once so a table cannot quietly grow a column for one experiment and not another, which
is how two runs stop being comparable while still appearing side by side.
"""

_TRADE_METRICS: Final[frozenset[str]] = frozenset(
    {
        "expectancy",
        "average_r",
        "expectancy_r",
        "profit_factor",
        "win_rate",
        "average_win",
        "average_loss",
        "max_consecutive_losses",
    }
)


class ComparisonRow(DomainModel):
    """One experiment's line in the table."""

    experiment_id: Text
    name: Text
    role: ExperimentRole
    status: ExperimentStatus
    metrics: Mapping[str, Decimal | None]
    """Every metric in :data:`COMPARISON_METRICS`.

    A value of ``None`` means the run could not produce that figure. It is never filled with
    zero: zero is a measurement, and a run that could not compute a Sharpe ratio did not
    measure a Sharpe ratio of nothing.
    """


class ComparisonTable(DomainModel):
    """Results side by side, in the order they were handed over.

    Offers no ordering, no ranking and no selection, and the absence is asserted by a test so
    that adding one is a failure rather than a convenience.
    """

    rows: tuple[ComparisonRow, ...] = ()


def _metric_values(result: ExperimentResult) -> dict[str, Decimal | None]:
    """Read every compared metric from one result, or nothing when it produced none."""
    performance = result.performance
    if performance is None:
        return dict.fromkeys(COMPARISON_METRICS)
    values: dict[str, Decimal | None] = {}
    for metric in COMPARISON_METRICS:
        if metric == "closed_trades":
            values[metric] = Decimal(performance.trades.count)
        elif metric in _TRADE_METRICS:
            raw = getattr(performance.trades, metric)
            values[metric] = None if raw is None else Decimal(raw)
        else:
            values[metric] = getattr(performance, metric)
    return values


def compare(results: Sequence[ExperimentResult]) -> ComparisonTable:
    """Return the results as a table, in the order given.

    Args:
        results: What to compare. A failed run is included: omitting it would hide the
            configurations that did not survive, which is the half of a comparison most
            likely to matter.

    Returns:
        The table. Comparing nothing yields an empty table rather than an error — a run set
        that turned out to be empty is a fact about the search, not a mistake in reading it.
    """
    return ComparisonTable(
        rows=tuple(
            ComparisonRow(
                experiment_id=result.experiment_id,
                name=result.definition.name,
                role=result.definition.role,
                status=result.status,
                metrics=_metric_values(result),
            )
            for result in results
        )
    )
