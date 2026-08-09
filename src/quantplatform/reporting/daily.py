"""Turning a running session into one day's report.

The whole file reads a :class:`~quantplatform.paper.results.SessionResult` and writes value
objects. Nothing here calls a strategy, a risk engine, a broker or a portfolio; nothing here
holds a reference to one. The reporting layer observes and cannot act, which is what makes
it safe to run alongside a live session.

**Daily metrics go through the same function the run-level ones do.** Sharpe, Sortino,
drawdown and total return are computed by
:func:`~quantplatform.backtesting.metrics.compute_performance` over the day's own equity
curve. A second implementation for the daily case would be two definitions of "Sharpe" that
must agree forever and eventually will not.

**A round trip is attributed to the day it closed.** A position opened on Monday and sold on
Tuesday is Tuesday's trade: Monday had no outcome to be right or wrong about, and counting
the entry would put a win on a day before the market decided it was one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal, localcontext

from quantplatform.backtesting.metrics import (
    EquityPoint,
    PerformanceSummary,
    TradeStatistics,
    compute_performance,
)
from quantplatform.backtesting.results import BacktestResult, BarOutcome
from quantplatform.core.clock import Clock
from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.enums import AlertSeverity, OrderSide, OrderStatus, RiskOutcome
from quantplatform.core.models.orders import Fill
from quantplatform.core.numeric import to_decimal
from quantplatform.core.timeutils import ensure_utc
from quantplatform.paper.results import SessionResult
from quantplatform.reporting.config import AlertThresholds, ReportingConfiguration
from quantplatform.reporting.health import evaluate_health
from quantplatform.reporting.models import (
    Alert,
    AlertCode,
    DailyAlerts,
    DailyComparison,
    DailyReport,
    DailySeries,
    DailyStatistics,
    FeedDiagnostics,
    HealthLevel,
    RoundTrip,
    SeriesPoint,
)
from quantplatform.reporting.summary import build_summary

__all__ = [
    "DailyReportBuilder",
    "DailyReportRecorder",
    "evaluate_alerts",
    "reconstruct_round_trips",
]

_MIN_BARS_FOR_SPAN = 2
"""A span needs a first and a last bar; one bar describes an instant."""

_NO_DIAGNOSTICS = FeedDiagnostics()
"""Everything zero and nothing measured, for a caller with no feed observations."""


# --- Round trips ----------------------------------------------------------------------------


class _OpenCycle:
    """A position being accumulated, until it returns to flat and becomes a round trip."""

    def __init__(self, *, symbol: str, side: OrderSide, opened_at: datetime) -> None:
        self.symbol = symbol
        self.side = side
        self.opened_at = opened_at
        self.quantity = ZERO
        self.cost = ZERO
        """Remaining book cost of the open quantity, at average price."""

        self.entry_quantity = ZERO
        self.entry_cost = ZERO
        self.exit_quantity = ZERO
        self.exit_value = ZERO
        self.fees = ZERO
        self.realized = ZERO

    def add_entry(self, fill: Fill) -> None:
        """Fold an opening fill into the cycle."""
        self.quantity += fill.quantity
        self.cost += fill.quantity * fill.price
        self.entry_quantity += fill.quantity
        self.entry_cost += fill.quantity * fill.price
        self.fees += fill.fee

    def reduce(self, fill: Fill) -> RoundTrip | None:
        """Fold a closing fill in, returning the finished trade once the position is flat.

        Average cost, matching the portfolio engine's own accounting. A different basis here
        would make the report disagree with the ledger it is describing.
        """
        if self.quantity <= ZERO:
            return None
        matched = min(fill.quantity, self.quantity)
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            average = self.cost / self.quantity
            self.realized += (fill.price - average) * matched
            self.cost -= average * matched
        self.quantity -= matched
        self.exit_quantity += matched
        self.exit_value += matched * fill.price
        self.fees += fill.fee
        if self.quantity > ZERO:
            return None
        return self._close(fill.executed_at)

    def _close(self, closed_at: datetime) -> RoundTrip:
        """Freeze the finished cycle into a round trip."""
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            entry_price = (
                self.entry_cost / self.entry_quantity if self.entry_quantity > ZERO else ZERO
            )
            exit_price = self.exit_value / self.exit_quantity if self.exit_quantity > ZERO else ZERO
        return RoundTrip(
            symbol=self.symbol,
            side=self.side,
            opened_at=self.opened_at,
            closed_at=closed_at,
            quantity=self.exit_quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            gross_pnl=self.realized,
            fees=self.fees,
            net_pnl=self.realized - self.fees,
        )


def reconstruct_round_trips(fills: Sequence[Fill]) -> tuple[RoundTrip, ...]:
    """Rebuild completed position lifecycles from a chronological run of fills.

    Args:
        fills: Every fill the session produced, in any order.

    Returns:
        Each round trip that reached flat, ordered by when it closed. A position still open
        at the end produces nothing: it has no outcome yet.
    """
    cycles: dict[str, _OpenCycle] = {}
    trips: list[RoundTrip] = []
    for fill in sorted(fills, key=lambda item: (item.executed_at, str(item.fill_id))):
        cycle = cycles.get(fill.symbol)
        if fill.side is OrderSide.BUY:
            if cycle is None:
                cycle = _OpenCycle(
                    symbol=fill.symbol, side=OrderSide.BUY, opened_at=fill.executed_at
                )
                cycles[fill.symbol] = cycle
            cycle.add_entry(fill)
            continue
        if cycle is None:
            # A closing fill with no recorded opening. The platform is spot long-only, so
            # this means the fill history handed in starts mid-position; there is no cost
            # basis to measure a result against, and inventing one would fabricate a PnL.
            continue
        finished = cycle.reduce(fill)
        if finished is not None:
            trips.append(finished)
            del cycles[fill.symbol]
    return tuple(trips)


def _trade_statistics(trips: Sequence[RoundTrip]) -> TradeStatistics:
    """Summarise a day's closed trades in the shape the metrics function expects."""
    wins = [trip for trip in trips if trip.is_win]
    losses = [trip for trip in trips if not trip.is_win]
    gross_profit = sum((trip.net_pnl for trip in wins), start=ZERO)
    gross_loss = -sum((trip.net_pnl for trip in losses), start=ZERO)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        count = len(trips)
        win_rate = Decimal(len(wins)) / Decimal(count) if count else None
        average_win = gross_profit / Decimal(len(wins)) if wins else None
        average_loss = gross_loss / Decimal(len(losses)) if losses else None
        profit_factor = gross_profit / gross_loss if gross_loss > ZERO else None
        expectancy = (
            sum((trip.net_pnl for trip in trips), start=ZERO) / Decimal(count) if count else None
        )
    return TradeStatistics(
        count=count,
        wins=len(wins),
        losses=len(losses),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        win_rate=win_rate,
        average_win=average_win,
        average_loss=average_loss,
        profit_factor=profit_factor,
        expectancy=expectancy,
    )


# --- Alerts ---------------------------------------------------------------------------------


def evaluate_alerts(*, statistics: DailyStatistics, thresholds: AlertThresholds) -> DailyAlerts:
    """Raise everything about the day that crossed a configured line.

    Args:
        statistics: The day's computed figures.
        thresholds: The limits in force.

    Returns:
        Every alert that fired, in a stable order.
    """
    raised: list[Alert] = []
    _add_drawdown(raised, statistics, thresholds)
    _add_loss(raised, statistics, thresholds)
    _add_counter(
        raised,
        code=AlertCode.GAP_DETECTED,
        severity=AlertSeverity.ERROR,
        observed=statistics.gap_count,
        limit=thresholds.max_gap_count,
        message="candle gap(s) detected; the day's history is discontinuous",
    )
    _add_counter(
        raised,
        code=AlertCode.MULTIPLE_RECONNECTS,
        severity=AlertSeverity.WARNING,
        observed=statistics.reconnect_count,
        limit=thresholds.max_reconnects,
        message="feed reconnection(s)",
    )
    _add_counter(
        raised,
        code=AlertCode.MISSING_DATA,
        severity=AlertSeverity.ERROR,
        observed=statistics.missing_bars,
        limit=thresholds.max_missing_bars,
        message="bar(s) missing from the series",
    )
    _add_counter(
        raised,
        code=AlertCode.RUNTIME_EXCEPTION,
        severity=AlertSeverity.ERROR,
        observed=statistics.runtime_exceptions,
        limit=thresholds.max_runtime_exceptions,
        message="runtime exception(s) during the session",
    )
    _add_rejection_spikes(raised, statistics, thresholds)
    _add_acceptance(raised, statistics, thresholds)
    _add_cost_ratios(raised, statistics, thresholds)
    return DailyAlerts(alerts=tuple(raised))


def _add_drawdown(
    raised: list[Alert], statistics: DailyStatistics, thresholds: AlertThresholds
) -> None:
    """Flag an intraday decline past its limit."""
    limit = thresholds.max_daily_drawdown
    if statistics.max_drawdown <= limit:
        return
    severity = (
        AlertSeverity.CRITICAL
        if statistics.max_drawdown >= limit * thresholds.red_escalation_factor
        else AlertSeverity.ERROR
    )
    raised.append(
        Alert(
            code=AlertCode.DRAWDOWN_EXCEEDED,
            severity=severity,
            message=(
                f"intraday drawdown reached {_percent(statistics.max_drawdown)} "
                f"against a limit of {_percent(limit)}"
            ),
            observed=statistics.max_drawdown,
            threshold=limit,
        )
    )


def _add_loss(
    raised: list[Alert], statistics: DailyStatistics, thresholds: AlertThresholds
) -> None:
    """Flag a loss larger than the configured magnitude."""
    loss = -statistics.daily_pnl
    if loss <= thresholds.max_daily_loss:
        return
    raised.append(
        Alert(
            code=AlertCode.LARGE_LOSS,
            severity=AlertSeverity.ERROR,
            message=f"the day lost {loss}, beyond the {thresholds.max_daily_loss} limit",
            observed=loss,
            threshold=thresholds.max_daily_loss,
        )
    )


def _add_counter(
    raised: list[Alert],
    *,
    code: AlertCode,
    severity: AlertSeverity,
    observed: int,
    limit: int,
    message: str,
) -> None:
    """Flag a counter that rose past its limit."""
    if observed <= limit:
        return
    raised.append(
        Alert(
            code=code,
            severity=severity,
            message=f"{observed} {message} (limit {limit})",
            observed=Decimal(observed),
            threshold=Decimal(limit),
        )
    )


def _add_rejection_spikes(
    raised: list[Alert], statistics: DailyStatistics, thresholds: AlertThresholds
) -> None:
    """Flag order flow being refused unusually often, by risk or by the venue adapter."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        risk_total = statistics.approved_orders + statistics.risk_rejections
        if risk_total > 0:
            ratio = Decimal(statistics.risk_rejections) / Decimal(risk_total)
            if ratio > thresholds.max_risk_rejection_ratio:
                raised.append(
                    Alert(
                        code=AlertCode.RISK_REJECTION_SPIKE,
                        severity=AlertSeverity.WARNING,
                        message=f"risk refused {_percent(ratio)} of order intents",
                        observed=ratio,
                        threshold=thresholds.max_risk_rejection_ratio,
                    )
                )
        broker_total = statistics.approved_orders
        if broker_total > 0:
            ratio = Decimal(statistics.broker_rejections) / Decimal(broker_total)
            if ratio > thresholds.max_broker_rejection_ratio:
                raised.append(
                    Alert(
                        code=AlertCode.BROKER_REJECTION_SPIKE,
                        severity=AlertSeverity.ERROR,
                        message=f"the broker refused {_percent(ratio)} of submitted orders",
                        observed=ratio,
                        threshold=thresholds.max_broker_rejection_ratio,
                    )
                )


def _add_acceptance(
    raised: list[Alert], statistics: DailyStatistics, thresholds: AlertThresholds
) -> None:
    """Flag a feed that delivered less than it should have."""
    observed = statistics.acceptance_rate
    if observed is None or observed >= thresholds.min_acceptance_rate:
        return
    raised.append(
        Alert(
            code=AlertCode.LOW_ACCEPTANCE_RATE,
            severity=AlertSeverity.WARNING,
            message=(
                f"only {_percent(observed)} of received bars reached the pipeline, "
                f"against a floor of {_percent(thresholds.min_acceptance_rate)}"
            ),
            observed=observed,
            threshold=thresholds.min_acceptance_rate,
        )
    )


def _add_cost_ratios(
    raised: list[Alert], statistics: DailyStatistics, thresholds: AlertThresholds
) -> None:
    """Flag execution costs that look wrong against the notional they were charged on."""
    if statistics.traded_notional <= ZERO:
        return
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        slippage_ratio = abs(statistics.slippage_paid) / statistics.traded_notional
        commission_ratio = statistics.commission_paid / statistics.traded_notional
    if slippage_ratio > thresholds.max_slippage_ratio:
        raised.append(
            Alert(
                code=AlertCode.ABNORMAL_SLIPPAGE,
                severity=AlertSeverity.WARNING,
                message=f"slippage was {_percent(slippage_ratio)} of traded notional",
                observed=slippage_ratio,
                threshold=thresholds.max_slippage_ratio,
            )
        )
    if commission_ratio > thresholds.max_commission_ratio:
        raised.append(
            Alert(
                code=AlertCode.ABNORMAL_COMMISSION,
                severity=AlertSeverity.WARNING,
                message=f"commission was {_percent(commission_ratio)} of traded notional",
                observed=commission_ratio,
                threshold=thresholds.max_commission_ratio,
            )
        )


def _percent(value: Decimal) -> str:
    """Render a unit-interval ratio as a percentage with one decimal place."""
    return f"{value * Decimal(100):.1f}%"


# --- Comparison -----------------------------------------------------------------------------


def _compare(
    *, statistics: DailyStatistics, level: HealthLevel, previous: DailyReport
) -> DailyComparison:
    """Describe how today moved against the previous reported day."""
    before = previous.statistics
    deteriorations: list[str] = []

    pnl_delta = statistics.daily_pnl - before.daily_pnl
    if pnl_delta < ZERO:
        deteriorations.append(f"PnL fell by {abs(pnl_delta)} against {previous.day.isoformat()}")

    win_rate_delta = _delta(statistics.win_rate, before.win_rate)
    if win_rate_delta is not None and win_rate_delta < ZERO:
        deteriorations.append(f"win rate fell by {_percent(abs(win_rate_delta))}")

    drawdown_delta = statistics.max_drawdown - before.max_drawdown
    if drawdown_delta > ZERO:
        deteriorations.append(f"drawdown deepened by {_percent(drawdown_delta)}")

    sharpe_delta = _delta(statistics.sharpe_ratio, before.sharpe_ratio)
    if sharpe_delta is not None and sharpe_delta < ZERO:
        deteriorations.append(f"Sharpe fell by {abs(sharpe_delta):.2f}")

    acceptance_delta = _delta(statistics.acceptance_rate, before.acceptance_rate)
    if acceptance_delta is not None and acceptance_delta < ZERO:
        deteriorations.append(f"feed acceptance fell by {_percent(abs(acceptance_delta))}")

    health_delta = level.rank - previous.health.level.rank
    if health_delta > 0:
        deteriorations.append(f"health moved from {previous.health.level.value} to {level.value}")

    return DailyComparison(
        previous_day=previous.day,
        pnl_delta=pnl_delta,
        win_rate_delta=win_rate_delta,
        drawdown_delta=drawdown_delta,
        sharpe_delta=sharpe_delta,
        trade_count_delta=statistics.trade_count - before.trade_count,
        acceptance_rate_delta=acceptance_delta,
        health_delta=health_delta,
        deteriorations=tuple(deteriorations),
    )


def _delta(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    """Return the difference between two optional figures, ``None`` if either is missing."""
    if current is None or previous is None:
        return None
    return current - previous


# --- Builder --------------------------------------------------------------------------------


class DailyReportBuilder:
    """Builds one day's report from a session's own record of itself."""

    def __init__(self, *, config: ReportingConfiguration, clock: Clock) -> None:
        """Create a builder.

        Args:
            config: Where a day begins and what counts as an alert.
            clock: Injected time source, stamped onto each report as ``generated_at``.
        """
        self._config = config
        self._clock = clock

    @property
    def config(self) -> ReportingConfiguration:
        """Return the configuration in force."""
        return self._config

    def build(
        self,
        *,
        day: date,
        result: SessionResult,
        diagnostics: FeedDiagnostics | None = None,
        previous: DailyReport | None = None,
    ) -> DailyReport:
        """Summarise one day of a session.

        Args:
            day: The reporting day to describe.
            result: Everything the session has produced so far.
            diagnostics: Feed and process observations from outside the session, which
                cannot see its own data source.
            previous: The last reported day, for comparison.

        Returns:
            The finished, immutable report.
        """
        observations = diagnostics if diagnostics is not None else _NO_DIAGNOSTICS
        detail = result.detail
        outcomes = self._outcomes_for(day, detail)
        trips = tuple(
            trip
            for trip in reconstruct_round_trips(result.fills)
            if self._config.day_of(trip.closed_at) == day
        )
        opening, curve = self._day_curve(day, detail)
        performance = self._performance(detail, curve, opening, trips)
        statistics = self._statistics(
            result=result,
            outcomes=outcomes,
            trips=trips,
            opening=opening,
            performance=performance,
            observations=observations,
        )
        health = evaluate_health(statistics=statistics, thresholds=self._config.thresholds)
        alerts = evaluate_alerts(statistics=statistics, thresholds=self._config.thresholds)
        comparison = (
            _compare(statistics=statistics, level=health.level, previous=previous)
            if previous is not None
            else None
        )
        return DailyReport(
            session_id=result.session_id,
            strategy_id=result.strategy_id,
            day=day,
            generated_at=ensure_utc(self._clock.now()),
            quote_asset=detail.config.quote_asset if detail is not None else "USDT",
            timezone=self._config.timezone,
            statistics=statistics,
            health=health,
            alerts=alerts,
            summary=build_summary(
                day=day,
                statistics=statistics,
                health=health,
                alerts=alerts,
                comparison=comparison,
            ),
            series=self._series(outcomes, curve, trips),
            trades=trips,
            comparison=comparison,
        )

    def _outcomes_for(self, day: date, detail: BacktestResult | None) -> tuple[BarOutcome, ...]:
        """Return the bars that closed inside the reporting day."""
        if detail is None:
            return ()
        return tuple(
            outcome for outcome in detail.bars if self._config.day_of(outcome.bar.close_time) == day
        )

    def _day_curve(
        self, day: date, detail: BacktestResult | None
    ) -> tuple[Decimal, tuple[EquityPoint, ...]]:
        """Return the day's opening equity and its curve, with day-local drawdown.

        Drawdown is recomputed against the *day's* running peak rather than the run's. A
        daily report that inherited the run's high-water mark would show a deep drawdown on
        a day that never fell, purely because a better day happened last week.
        """
        if detail is None:
            return ZERO, ()
        opening = detail.config.initial_capital
        points: list[EquityPoint] = []
        peak = opening
        for point in detail.equity_curve:
            point_day = self._config.day_of(point.at)
            if point_day < day:
                opening = point.equity
                continue
            if point_day > day:
                break
            peak = max(peak, point.equity) if points else max(opening, point.equity)
            drawdown = (peak - point.equity) / peak if peak > ZERO else ZERO
            points.append(EquityPoint(at=point.at, equity=point.equity, drawdown=drawdown))
        return opening, tuple(points)

    def _performance(
        self,
        detail: BacktestResult | None,
        curve: tuple[EquityPoint, ...],
        opening: Decimal,
        trips: Sequence[RoundTrip],
    ) -> PerformanceSummary:
        """Run the day's curve through the platform's own metrics function."""
        config = detail.config if detail is not None else None
        realized = sum((trip.net_pnl for trip in trips), start=ZERO)
        return compute_performance(
            curve=curve,
            initial_equity=opening if opening > ZERO else Decimal(1),
            realized_pnl=realized,
            unrealized_pnl=ZERO,
            commission_paid=sum((trip.fees for trip in trips), start=ZERO),
            slippage_paid=ZERO,
            trades=_trade_statistics(trips),
            periods_per_year=config.periods_per_year if config else Decimal(8_760),
            risk_free_rate=config.risk_free_rate if config else ZERO,
            minimum_periods_for_ratios=config.minimum_periods_for_ratios if config else 2,
        )

    def _statistics(
        self,
        *,
        result: SessionResult,
        outcomes: tuple[BarOutcome, ...],
        trips: tuple[RoundTrip, ...],
        opening: Decimal,
        performance: PerformanceSummary,
        observations: FeedDiagnostics,
    ) -> DailyStatistics:
        """Fold every source into the day's counted figures."""
        flow = _OrderFlow.of(outcomes)
        costs = _Costs.of(outcomes)
        closing = performance.final_equity
        runtime = result.runtime
        wins = [trip.net_pnl for trip in trips if trip.is_win]
        losses = [trip.net_pnl for trip in trips if not trip.is_win]
        last_snapshot = next(
            (outcome.snapshot for outcome in reversed(outcomes) if outcome.snapshot is not None),
            None,
        )
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            exposure = (
                last_snapshot.gross_exposure / last_snapshot.equity
                if last_snapshot is not None and last_snapshot.equity > ZERO
                else None
            )
            holding = (
                Decimal(sum(trip.holding_seconds for trip in trips)) / Decimal(len(trips))
                if trips
                else None
            )
            average_size = (
                costs.entry_notional / Decimal(costs.entry_fills) if costs.entry_fills else None
            )
        return DailyStatistics(
            opening_equity=opening,
            daily_equity=closing,
            daily_pnl=closing - opening,
            daily_return=performance.total_return,
            trade_count=len(trips),
            long_trades=sum(1 for trip in trips if trip.side is OrderSide.BUY),
            short_trades=sum(1 for trip in trips if trip.side is OrderSide.SELL),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=performance.trades.win_rate,
            average_win=performance.trades.average_win,
            average_loss=performance.trades.average_loss,
            profit_factor=performance.trades.profit_factor,
            expectancy=performance.trades.expectancy,
            largest_winner=max(wins) if wins else None,
            largest_loser=min(losses) if losses else None,
            average_position_size=average_size,
            average_holding_seconds=holding,
            exposure_utilization=exposure,
            max_drawdown=performance.max_drawdown,
            sharpe_ratio=performance.sharpe_ratio,
            sortino_ratio=performance.sortino_ratio,
            commission_paid=costs.commission,
            slippage_paid=costs.slippage,
            traded_notional=costs.notional,
            approved_orders=flow.approved,
            resized_orders=flow.resized,
            rejected_orders=flow.risk_rejections
            + flow.broker_rejections
            + observations.broker_rejections,
            risk_rejections=flow.risk_rejections,
            broker_rejections=flow.broker_rejections + observations.broker_rejections,
            runtime_seconds=_span_seconds(outcomes),
            acceptance_rate=runtime.acceptance_rate,
            bars_processed=len(outcomes),
            bars_rejected=runtime.bars_rejected,
            reconnect_count=observations.reconnects,
            gap_count=observations.gaps_detected,
            heartbeat_failures=observations.heartbeat_failures,
            duplicate_candles=observations.duplicate_candles,
            out_of_order_candles=observations.out_of_order_candles,
            unknown_symbols=observations.unknown_symbols,
            missing_bars=observations.missing_bars,
            runtime_exceptions=observations.runtime_exceptions,
            session_interruptions=observations.session_interruptions or runtime.restarts,
            report_failures=runtime.report_failures,
            clock_drift_seconds=observations.clock_drift_seconds,
        )

    def _series(
        self,
        outcomes: tuple[BarOutcome, ...],
        curve: tuple[EquityPoint, ...],
        trips: tuple[RoundTrip, ...],
    ) -> DailySeries:
        """Collect the plottable shape of the day."""
        returns: list[SeriesPoint] = []
        previous: Decimal | None = None
        for point in curve:
            if previous is not None and previous > ZERO:
                with localcontext() as ctx:
                    ctx.prec = DECIMAL_WORKING_PRECISION
                    returns.append(
                        SeriesPoint(at=point.at, value=(point.equity - previous) / previous)
                    )
            previous = point.equity
        exposure: list[SeriesPoint] = []
        for outcome in outcomes:
            snapshot = outcome.snapshot
            if snapshot is None or snapshot.equity <= ZERO:
                continue
            with localcontext() as ctx:
                ctx.prec = DECIMAL_WORKING_PRECISION
                exposure.append(
                    SeriesPoint(
                        at=outcome.bar.close_time,
                        value=snapshot.gross_exposure / snapshot.equity,
                    )
                )
        return DailySeries(
            equity=tuple(SeriesPoint(at=point.at, value=point.equity) for point in curve),
            drawdown=tuple(SeriesPoint(at=point.at, value=point.drawdown) for point in curve),
            returns=tuple(returns),
            exposure=tuple(exposure),
            trade_pnl=tuple(trip.net_pnl for trip in trips),
        )


class _OrderFlow:
    """How many order decisions of each kind a day produced."""

    __slots__ = ("approved", "broker_rejections", "resized", "risk_rejections")

    def __init__(self) -> None:
        self.approved = 0
        self.resized = 0
        self.risk_rejections = 0
        self.broker_rejections = 0

    @classmethod
    def of(cls, outcomes: Sequence[BarOutcome]) -> _OrderFlow:
        """Count the order flow across a day's bars."""
        flow = cls()
        for outcome in outcomes:
            for decision in outcome.decisions:
                if decision.is_executable:
                    flow.approved += 1
                    if decision.outcome is RiskOutcome.RESIZED:
                        flow.resized += 1
                else:
                    flow.risk_rejections += 1
            flow.broker_rejections += sum(
                1 for order in outcome.orders if order.status is OrderStatus.REJECTED
            )
        return flow


class _Costs:
    """What a day's execution cost, and what it traded to incur it."""

    __slots__ = ("commission", "entry_fills", "entry_notional", "notional", "slippage")

    def __init__(self) -> None:
        self.commission = ZERO
        self.slippage = ZERO
        self.notional = ZERO
        self.entry_notional = ZERO
        self.entry_fills = 0

    @classmethod
    def of(cls, outcomes: Sequence[BarOutcome]) -> _Costs:
        """Total a day's fees, slippage and traded notional.

        Slippage is measured against each fill's own bar open, matching
        :class:`~quantplatform.backtesting.metrics.PerformanceSummary` exactly: it is the
        modelled execution cost of this run, not an estimate of market impact.
        """
        costs = cls()
        for outcome in outcomes:
            reference = outcome.bar.open
            for fill in outcome.fills:
                value = fill.price * fill.quantity
                costs.commission += fill.fee
                costs.notional += value
                signed = (
                    fill.price - reference if fill.side is OrderSide.BUY else reference - fill.price
                )
                costs.slippage += signed * fill.quantity
                if fill.side is OrderSide.BUY:
                    costs.entry_notional += value
                    costs.entry_fills += 1
        return costs


def _span_seconds(outcomes: Sequence[BarOutcome]) -> Decimal:
    """Return the wall-clock span a day's processed bars cover."""
    if len(outcomes) < _MIN_BARS_FOR_SPAN:
        return ZERO
    span = outcomes[-1].bar.close_time - outcomes[0].bar.close_time
    return to_decimal(int(span.total_seconds()))


# --- Session integration ----------------------------------------------------------------------


class DailyReportRecorder:
    """A :class:`~quantplatform.paper.session.DayRolloverObserver` that writes daily reports.

    The only object in this package a running session ever touches, and it can do exactly
    two things to that session: nothing, and nothing. It receives a finished day, builds a
    report, hands it to a writer and returns.

    **It never raises.** The observer contract forbids it, because a session that has been
    trading for a week must not die because a disk filled up at midnight. A failure is
    counted here and in the session's own
    :attr:`~quantplatform.paper.results.RuntimeMetrics.report_failures`, so containment does
    not become silence.
    """

    def __init__(
        self,
        *,
        builder: DailyReportBuilder,
        sink: Callable[[DailyReport], object] | None = None,
        diagnostics: Callable[[], FeedDiagnostics] | None = None,
        previous: DailyReport | None = None,
    ) -> None:
        """Wire a recorder.

        Args:
            builder: Builds the report for a finished day.
            sink: Where a finished report goes — normally
                :meth:`~quantplatform.reporting.writer.DailyReportWriter.write`. Whatever it
                returns is discarded, which is why the return type is unconstrained: a sink
                that reports where it put the files is as valid as one that returns nothing,
                and neither can tell the recorder anything. Omitted means reports are only
                kept in memory, which is what a test wants.
            diagnostics: Called at rollover for feed and process observations the session
                cannot see itself.
            previous: The last report from an earlier process, so the first day of a
                restarted session still compares against something.
        """
        self._builder = builder
        self._sink = sink
        self._diagnostics = diagnostics
        self._previous = previous
        self._reports: list[DailyReport] = []
        self._failures = 0

    @property
    def reports(self) -> tuple[DailyReport, ...]:
        """Return every report produced, oldest first."""
        return tuple(self._reports)

    @property
    def failures(self) -> int:
        """Return how many rollovers failed to produce a report."""
        return self._failures

    @property
    def latest(self) -> DailyReport | None:
        """Return the most recent report, if any."""
        return self._reports[-1] if self._reports else None

    def day_of(self, moment: datetime) -> date:
        """Return the reporting day an instant falls in, per the reporting time zone."""
        return self._builder.config.day_of(moment)

    def on_day_rollover(self, *, completed_day: date, result: SessionResult) -> None:
        """Build and emit the report for a finished day, swallowing any failure."""
        try:
            report = self._builder.build(
                day=completed_day,
                result=result,
                diagnostics=self._diagnostics() if self._diagnostics is not None else None,
                previous=self._previous,
            )
            if self._sink is not None:
                self._sink(report)
            self._reports.append(report)
            self._previous = report
        except Exception:
            # Contained deliberately: see the class docstring. Counted, never hidden.
            self._failures += 1
