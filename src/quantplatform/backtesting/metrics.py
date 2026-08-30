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

from pydantic import ConfigDict, Field

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ONE, ZERO
from quantplatform.core.models.base import DomainModel, UtcDatetime
from quantplatform.core.models.trades import ClosedTrade
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

    # --- Research metrics (M2) ------------------------------------------------------------
    #
    # Added so runs can be *compared*, not merely described. A win rate says how often a
    # strategy is right; none of these say that, and all of them say more about whether it
    # survives being wrong.

    reward_risk_ratio: Money | None = None
    """``average_win / average_loss``; ``None`` when nothing lost, so nothing to divide by.

    Deliberately not clamped or defaulted: a strategy with no losing trades in its sample
    has an undefined reward-to-risk, not an infinite one, and reporting a number here would
    invent a comparison the data cannot support.
    """

    max_consecutive_losses: int = Field(default=0, ge=0)
    """Longest unbroken run of losing trades.

    A distinct failure from the loss *rate*: five losses spread across a month is a strategy
    performing within expectation, and five in a row is a regime it did not anticipate. The
    second is what exhausts an account and what a circuit breaker is sized against.
    """

    max_consecutive_wins: int = Field(default=0, ge=0)
    """Longest unbroken run of winning trades, reported for symmetry."""

    average_r: Money | None = None
    """Mean R-multiple across closed trades, where ``R = net_pnl / risk_amount``.

    ``None`` until positions record what they risked. That is not a temporary gap in the
    computation but a real one in the data: risk_amount does not exist until a position
    carries a stop, and no position did before this work began. Reporting ``0`` instead
    would read as "every trade broke even", which is a different and false claim.
    """

    expectancy_r: Money | None = None
    """Expectancy denominated in R rather than quote currency.

    The comparable form: quote-currency expectancy scales with account size and position
    sizing, so two strategies cannot be ranked by it. R-expectancy can be.
    """


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

    time_in_market: Money | None = None
    """Share of processed bars at whose close the account held something.

    Counted in bars rather than in elapsed time: a gap in the feed is not time spent holding
    a position, and on any timeframe the two figures agree only when nothing is missing —
    which is exactly the case where the distinction would not have mattered.

    Any exposure counts as exposure. A version weighted by position size will be a *different*
    metric under a different name when several positions become possible, not a redefinition
    of this one, or every series recorded before that day would stop being comparable.
    """

    turnover: Money | None = None
    """Total traded notional over initial equity.

    How hard the account was worked to produce the result. Two strategies with the same
    return and very different turnover are not the same strategy: one of them is paying far
    more in fees and slippage for it, and is far more exposed to those costs being modelled
    optimistically. ``None`` when nothing was traded or the account had no equity to measure
    against.
    """


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
    traded_notional: Decimal | None = None,
    exposed_bars: int = 0,
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
        exposed_bars: Bars that closed holding something, for time in market.
        traded_notional: Total quote-asset notional executed across every fill, for turnover.
            Optional so that callers predating this metric keep working and receive ``None``
            rather than a fabricated zero.

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
        turnover = (
            traded_notional / initial_equity
            if traded_notional is not None and initial_equity > ZERO
            else None
        )

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
        turnover=turnover,
        time_in_market=time_in_market(exposed_bars=exposed_bars, bars_processed=len(curve)),
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


def longest_streaks(trades: Sequence[ClosedTrade]) -> tuple[int, int]:
    """Return the longest unbroken runs of winning and of losing trades.

    A trade that broke exactly even breaks both runs. It is neither a win nor a loss, and
    letting it continue either would report a streak that did not happen — while which of the
    two it extended would come down to an arbitrary choice nobody could defend.

    Args:
        trades: Closed round trips, in the order they closed.

    Returns:
        ``(longest_wins, longest_losses)``, both zero when nothing closed.
    """
    best_wins = best_losses = wins = losses = 0
    for trade in trades:
        if trade.realized_pnl > ZERO:
            wins, losses = wins + 1, 0
        elif trade.realized_pnl < ZERO:
            wins, losses = 0, losses + 1
        else:
            wins = losses = 0
        best_wins = max(best_wins, wins)
        best_losses = max(best_losses, losses)
    return best_wins, best_losses


def time_in_market(*, exposed_bars: int, bars_processed: int) -> Decimal | None:
    """Return the share of processed bars at whose close something was held.

    Args:
        exposed_bars: Bars that closed with an open position.
        bars_processed: Bars the run consumed, which is the denominator.

    Returns:
        The share, or ``None`` when nothing was processed — a run with no bars has no
        exposure to report, and zero would claim it stood aside rather than never started.
    """
    if bars_processed <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        return Decimal(exposed_bars) / Decimal(bars_processed)
