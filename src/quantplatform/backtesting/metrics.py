"""Deterministic performance metrics over a completed backtest.

Every metric is either a real number or explicitly **not computable**. There is no third
option here, and in particular no zero standing in for "we could not work it out" — a Sharpe
ratio of ``0.0`` reported because the run had one bar is worse than no number at all, because
it looks like an answer.

Nothing divides without first proving the denominator is non-zero, and nothing annualises a
sample too short to annualise.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

from pydantic import ConfigDict

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ONE, ZERO
from quantplatform.core.models.base import DomainModel, UtcDatetime
from quantplatform.core.numeric import Money

__all__ = ["EquityPoint", "PerformanceSummary", "TradeStatistics", "compute_performance"]

_MIN_POINTS_FOR_DURATION = 2
"""A span needs a start and an end; one point describes an instant, not a duration."""

_MIN_DOWNSIDE_OBSERVATIONS = 2
"""Bessel's correction needs two samples; one losing bar has no dispersion to measure."""


class EquityPoint(DomainModel):
    """One point on the equity curve."""

    at: UtcDatetime
    equity: Money
    drawdown: Money
    """Fractional decline from the running peak, as a non-negative number where ``0.1`` is
    ten percent below the high-water mark."""


class TradeStatistics(DomainModel):
    """Round-trip statistics, counted from realised closes rather than from fills.

    A "trade" here is a position lifecycle that ended: opened, then reduced back to flat. A
    fill is not a trade — an entry that is still open has no outcome to be right or wrong
    about, and counting it would make the win rate move every time a position is merely
    scaled into.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int
    wins: int
    losses: int
    gross_profit: Money
    gross_loss: Money
    """Total of losing trades as a non-negative magnitude."""

    win_rate: Money | None = None
    average_win: Money | None = None
    average_loss: Money | None = None
    profit_factor: Money | None = None
    """``gross_profit / gross_loss``; ``None`` when there were no losses to divide by."""

    expectancy: Money | None = None
    """Average realised result per closed trade; ``None`` when nothing closed."""


class PerformanceSummary(DomainModel):
    """Everything the run can say about how it went.

    A field set to ``None`` means the metric could not be computed from this run, not that it
    computed to zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_equity: Money
    final_equity: Money
    total_return: Money | None = None
    realized_pnl: Money
    unrealized_pnl: Money
    max_drawdown: Money
    """Largest fractional peak-to-trough decline over the run."""

    cagr: Money | None = None
    sharpe_ratio: Money | None = None
    sortino_ratio: Money | None = None
    commission_paid: Money
    slippage_paid: Money
    """Quote-asset cost of executing away from the bar's reference price.

    Measured against each fill's own bar open, which is the price the broker would have
    matched at with slippage switched off. It is therefore the *modelled* execution cost of
    this run, not an estimate of real-world market impact.
    """

    trades: TradeStatistics
    bars_processed: int
    duration_seconds: int


def compute_performance(  # noqa: PLR0913 - a summary is defined by exactly these inputs
    *,
    curve: Sequence[EquityPoint],
    initial_equity: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    commission_paid: Decimal,
    slippage_paid: Decimal,
    trades: TradeStatistics,
    periods_per_year: Decimal,
    risk_free_rate: Decimal,
    minimum_periods_for_ratios: int,
) -> PerformanceSummary:
    """Summarise a completed run.

    Args:
        curve: Equity curve in chronological order; may be empty.
        initial_equity: Equity before the first bar.
        realized_pnl: Cumulative realised PnL from the portfolio.
        unrealized_pnl: Open PnL at the final mark.
        commission_paid: Total fees charged across every fill.
        slippage_paid: Total execution cost against each fill's bar open.
        trades: Round-trip statistics.
        periods_per_year: Bars per year, for annualisation.
        risk_free_rate: Annualised risk-free rate.
        minimum_periods_for_ratios: Return observations below which ratios are not computed.

    Returns:
        The summary, with metrics this run cannot support left as ``None``.
    """
    final_equity = curve[-1].equity if curve else initial_equity
    max_drawdown = max((point.drawdown for point in curve), default=ZERO)
    duration = _duration_seconds(curve)

    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        total_return = (
            (final_equity - initial_equity) / initial_equity if initial_equity > ZERO else None
        )
        returns = _period_returns(curve, initial_equity)
        sharpe = _sharpe(returns, periods_per_year, risk_free_rate, minimum_periods_for_ratios)
        sortino = _sortino(returns, periods_per_year, risk_free_rate, minimum_periods_for_ratios)
        cagr = _cagr(initial_equity, final_equity, duration)

    return PerformanceSummary(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        max_drawdown=max_drawdown,
        cagr=cagr,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        commission_paid=commission_paid,
        slippage_paid=slippage_paid,
        trades=trades,
        bars_processed=len(curve),
        duration_seconds=duration,
    )


def _duration_seconds(curve: Sequence[EquityPoint]) -> int:
    """Return the wall-time span the curve covers, zero for fewer than two points."""
    if len(curve) < _MIN_POINTS_FOR_DURATION:
        return 0
    return int((curve[-1].at - curve[0].at).total_seconds())


def _period_returns(curve: Sequence[EquityPoint], initial_equity: Decimal) -> tuple[Decimal, ...]:
    """Return the per-bar fractional returns of the equity curve.

    A bar whose starting equity was zero contributes no observation: the account had nothing
    to return *on*, and treating that as a return of zero would dilute every ratio computed
    from the sample.
    """
    returns: list[Decimal] = []
    previous = initial_equity
    for point in curve:
        if previous > ZERO:
            returns.append((point.equity - previous) / previous)
        previous = point.equity
    return tuple(returns)


def _mean(values: Sequence[Decimal]) -> Decimal:
    """Return the arithmetic mean of a non-empty sample."""
    return sum(values, start=ZERO) / Decimal(len(values))


def _standard_deviation(values: Sequence[Decimal], mean: Decimal) -> Decimal:
    """Return the sample standard deviation, using Bessel's correction."""
    variance = sum(((value - mean) ** 2 for value in values), start=ZERO) / Decimal(len(values) - 1)
    return variance.sqrt()


def _sharpe(
    returns: Sequence[Decimal],
    periods_per_year: Decimal,
    risk_free_rate: Decimal,
    minimum_periods: int,
) -> Decimal | None:
    """Return the annualised Sharpe ratio, or ``None`` when the sample cannot support one.

    Not computable with fewer than ``minimum_periods`` observations, or when returns never
    varied — a zero standard deviation makes the ratio a division by zero, not an infinitely
    good result.
    """
    if len(returns) < minimum_periods:
        return None
    mean = _mean(returns)
    deviation = _standard_deviation(returns, mean)
    if deviation <= ZERO:
        return None
    excess = mean - (risk_free_rate / periods_per_year)
    return (excess / deviation) * periods_per_year.sqrt()


def _sortino(
    returns: Sequence[Decimal],
    periods_per_year: Decimal,
    risk_free_rate: Decimal,
    minimum_periods: int,
) -> Decimal | None:
    """Return the annualised Sortino ratio, or ``None`` when it cannot be computed.

    Uses downside deviation, so a run that never lost money has no denominator. That is
    reported as not computable rather than as an unbounded ratio.
    """
    if len(returns) < minimum_periods:
        return None
    target = risk_free_rate / periods_per_year
    downside = [value for value in returns if value < target]
    if len(downside) < _MIN_DOWNSIDE_OBSERVATIONS:
        return None
    squared = sum(((value - target) ** 2 for value in downside), start=ZERO)
    deviation = (squared / Decimal(len(downside) - 1)).sqrt()
    if deviation <= ZERO:
        return None
    return ((_mean(returns) - target) / deviation) * periods_per_year.sqrt()


def _cagr(initial_equity: Decimal, final_equity: Decimal, duration_seconds: int) -> Decimal | None:
    """Return the compound annual growth rate, or ``None`` when it is meaningless.

    Requires a positive starting and ending equity and a run spanning real time. A wiped-out
    account has no growth *rate*: the loss is fully described by the total return, and forcing
    a rate out of it would produce a complex number or a fabricated floor.
    """
    if initial_equity <= ZERO or final_equity <= ZERO or duration_seconds <= 0:
        return None
    years = Decimal(duration_seconds) / Decimal(_SECONDS_PER_YEAR)
    if years <= ZERO:
        return None
    return (final_equity / initial_equity) ** (ONE / years) - ONE


_SECONDS_PER_YEAR = 365 * 86_400
