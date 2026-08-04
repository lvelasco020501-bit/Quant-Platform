"""The backtest engine: the only component that orchestrates a run.

It owns the chain and nothing else. Bars become features, features become a strategy context,
the strategy returns opinions, orchestration turns those into proposals, the risk engine
authorises or refuses them, the broker executes what was authorised, and the portfolio books
the result. No step is skipped and no component reaches around another — the strategy in
particular never sees the account, the broker or the risk engine, which is what lets the same
strategy run unchanged here and against a live venue.

**Execution is next-bar.** A decision made from bar *N*'s close is submitted to the broker and
matched against bar *N+1*, at *N+1*'s open. It cannot be otherwise: the broker prices a market
order at the bar's open, so matching a bar-*N* decision against bar *N* would fill it at a
price that printed before the strategy had seen the data it decided on. That is look-ahead,
and it is the single most effective way to make a losing strategy look profitable. The engine
therefore settles the previous bar's orders at the top of each iteration, before the current
bar is decided on.

**Determinism.** No clock is read, no random number is drawn, and no global state is touched.
Timestamps come from bars, identifiers are derived, and iteration order is fixed. Two runs
over the same data with the same configuration produce byte-identical output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from itertools import pairwise

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.backtesting.intents import build_intent
from quantplatform.backtesting.metrics import (
    EquityPoint,
    PerformanceSummary,
    TradeStatistics,
    compute_performance,
)
from quantplatform.backtesting.results import BacktestResult, BarOutcome, ComponentCallCounts
from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.enums import (
    OrderSide,
    OrderType,
    PositionState,
    ReconciliationStatus,
    SystemState,
)
from quantplatform.core.errors import (
    ConfigurationError,
    DataIntegrityError,
    StrategyContextError,
)
from quantplatform.core.events import DomainEvent
from quantplatform.core.interfaces import FeaturePipeline
from quantplatform.core.models.health import HealthStatus
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order, OrderIntent
from quantplatform.core.models.portfolio import PortfolioSnapshot, Position
from quantplatform.core.models.risk import RiskContext, RiskDecision
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.risk.engine import StandardRiskEngine
from quantplatform.strategies.base import BaseStrategy

__all__ = ["BacktestEngine"]

_SECONDS_PER_HOUR = 3_600
_SECONDS_PER_DAY = 86_400

_MIN_BARS_FOR_VOLATILITY = 3
"""Three closes yield two returns, the fewest that have any dispersion between them."""

_MIN_RETURNS_FOR_VOLATILITY = 2
"""Bessel's correction needs two observations."""


@dataclass(slots=True)
class _RunState:
    """Mutable bookkeeping for one run, discarded when the run ends.

    Deliberately local to a single :meth:`BacktestEngine.run` call rather than instance state,
    so two runs on the same engine cannot contaminate one another.
    """

    curve: list[EquityPoint]
    snapshots: list[PortfolioSnapshot]
    outcomes: list[BarOutcome]
    events: list[DomainEvent]
    signals: list[Signal]
    intents: list[OrderIntent]
    decisions: list[RiskDecision]
    approved: list[ApprovedOrder]
    orders: list[Order]
    fills: list[Fill]
    history: dict[str, list[MarketBar]]
    last_close: dict[str, Decimal]
    positions: dict[str, Position]
    approved_by_id: dict[str, ApprovedOrder]
    approval_times: list[datetime]
    peak_equity: Decimal
    day_start_equity: Decimal
    day_started_at: date | None
    commission: Decimal
    slippage: Decimal
    trade_results: list[Decimal]
    calls: dict[str, int]


class BacktestEngine:
    """Runs one strategy over one ordered series of closed bars.

    Every collaborator is injected; nothing is constructed internally and nothing is reached
    through a global. That is what makes a run reproducible from its inputs alone, and what
    allows a test to substitute a counting double for any single stage.
    """

    def __init__(
        self,
        *,
        config: BacktestConfig,
        strategy: BaseStrategy,
        features: FeaturePipeline,
        risk_engine: StandardRiskEngine,
        broker: SimulatedBroker,
        portfolio: SpotPortfolioEngine,
        symbols: Mapping[str, SymbolRules],
    ) -> None:
        """Wire a run.

        Args:
            config: Immutable run settings.
            strategy: The pure decision function under test.
            features: Pipeline producing the strategy's feature inputs.
            risk_engine: The only component permitted to authorise an order.
            broker: Deterministic simulated venue.
            portfolio: Sole accounting authority; the broker settles into it.
            symbols: Venue rules per traded symbol.
        """
        self._config = config
        self._strategy = strategy
        self._features = features
        self._risk = risk_engine
        self._broker = broker
        self._portfolio = portfolio
        self._symbols = dict(symbols)

    def run(self, bars: Sequence[MarketBar]) -> BacktestResult:
        """Execute the full chain over ``bars`` and return an immutable record of the run.

        Args:
            bars: Closed bars in non-decreasing close-time order, possibly spanning symbols.

        Returns:
            Everything the run produced, including the event sequence exactly as emitted.

        Raises:
            DataIntegrityError: If a bar is open, unknown to this engine, or out of order.
            StrategyContextError: If the feature pipeline cannot produce a feature the
                strategy requires, or a context otherwise fails its contract.
            StrategyError: Anything the strategy itself raises; a broken strategy stops the
                run rather than being silently skipped for the rest of it.
            PortfolioError: If portfolio accounting refuses a fill; a violated ledger
                invariant makes every later number meaningless, so the run stops.
        """
        self._validate_contract()
        self._validate(bars)
        state = self._initial_state()
        if not bars:
            return BacktestResult(config=self._config, performance=self._empty_performance())

        for index, bar in enumerate(bars):
            self._process_bar(index, bar, state)

        return self._build_result(state)

    # --- One iteration ----------------------------------------------------------------------

    def _process_bar(self, index: int, bar: MarketBar, state: _RunState) -> None:
        """Run the mandated chain for a single bar."""
        events: list[DomainEvent] = []

        # Settle what the previous bar authorised, at this bar's prices. This is step 7 of the
        # chain, run first, which is what makes execution next-bar rather than look-ahead.
        execution = self._broker.process_bar(bar)
        state.calls["broker_bars_processed"] += 1
        events.extend(execution.events)
        state.orders.extend(execution.orders)
        state.fills.extend(execution.fills)
        self._account_for_fills(execution.fills, bar, state)

        # The portfolio has already booked each fill: the broker settles atomically inside
        # process_bar so a refused fill can be rolled back. Verifying beats re-applying, which
        # would be an idempotent no-op that only looked like the chain was being followed.
        for fill in execution.fills:
            if not self._portfolio.has_applied(fill.fill_id):  # pragma: no cover - defensive
                msg = "the broker reported a fill the portfolio never applied"
                raise DataIntegrityError(msg, fill_id=str(fill.fill_id))
            state.calls["portfolio_fills_applied"] += 1

        history = state.history.setdefault(bar.symbol, [])
        history.append(bar)
        state.last_close[bar.symbol] = bar.close

        features = self._features.compute(history)
        state.calls["feature_computations"] += 1

        snapshot = self._snapshot(bar, state)
        signals = self._generate(bar, history, features, snapshot, state)
        intents = self._build_intents(signals, snapshot)
        decisions, submitted, decision_events = self._authorise(intents, bar, snapshot, state)
        events.extend(decision_events)

        state.signals.extend(signals)
        state.intents.extend(intents)
        state.decisions.extend(decisions)
        state.approved.extend(submitted)
        state.events.extend(events)

        final_snapshot = self._snapshot(bar, state)
        self._record_equity(final_snapshot, state)
        self._record_closed_trades(state)
        state.snapshots.append(final_snapshot)
        state.outcomes.append(
            BarOutcome(
                index=index,
                bar=bar,
                signals=tuple(signals),
                intents=tuple(intents),
                decisions=tuple(decisions),
                submitted=tuple(submitted),
                fills=tuple(execution.fills),
                orders=tuple(execution.orders),
                events=tuple(events),
                snapshot=final_snapshot,
            )
        )

    def _generate(
        self,
        bar: MarketBar,
        history: Sequence[MarketBar],
        features: Mapping[str, Decimal],
        snapshot: PortfolioSnapshot,
        state: _RunState,
    ) -> tuple[Signal, ...]:
        """Build the strategy context and ask the strategy for its opinion."""
        rules = self._symbols[bar.symbol]
        context = StrategyContext(
            symbol=bar.symbol,
            market_type=rules.market_type,
            timeframe=bar.timeframe,
            as_of=bar.close_time,
            bars=tuple(history),
            features=dict(features),
            position_state=self._position_state(snapshot, bar.symbol),
            symbol_rules=rules,
        )
        if context.history_length < self._strategy.metadata.required_history:
            # Warm-up: the strategy declared it needs more history than exists yet. Calling it
            # anyway would violate its own contract, so the bar is observed and skipped.
            return ()
        self._strategy.validate_context(context)
        state.calls["strategy_invocations"] += 1
        return tuple(self._strategy.generate(context))

    def _build_intents(
        self, signals: Sequence[Signal], snapshot: PortfolioSnapshot
    ) -> tuple[OrderIntent, ...]:
        """Translate actionable signals into proposals for the risk engine."""
        intents = []
        for signal in signals:
            intent = build_intent(
                signal,
                snapshot=snapshot,
                entry_fraction=self._config.entry_fraction,
                execution_mode=self._config.execution_mode,
                market_type=self._symbols[signal.symbol].market_type,
            )
            if intent is not None:
                intents.append(intent)
        return tuple(intents)

    def _authorise(
        self,
        intents: Sequence[OrderIntent],
        bar: MarketBar,
        snapshot: PortfolioSnapshot,
        state: _RunState,
    ) -> tuple[tuple[RiskDecision, ...], tuple[ApprovedOrder, ...], tuple[DomainEvent, ...]]:
        """Evaluate every intent and submit whatever the risk engine authorised.

        A rejection is an ordinary outcome and never stops the run: refusing one intent says
        nothing about the next. A broker refusal is equally ordinary — the venue declining an
        order is information, not a crash.
        """
        decisions: list[RiskDecision] = []
        submitted: list[ApprovedOrder] = []
        events: list[DomainEvent] = []
        for intent in intents:
            context = self._risk_context(bar, snapshot, state)
            assessment = self._risk.assess(intent, context)
            state.calls["risk_evaluations"] += 1
            decisions.append(assessment.decision)
            events.extend(assessment.events)
            order = assessment.decision.approved_order
            if order is None:
                continue
            result = self._broker.submit(order)
            state.calls["broker_submissions"] += 1
            events.extend(result.events)
            if result.accepted:
                submitted.append(order)
                state.approved_by_id[order.client_order_id] = order
                state.approval_times.append(bar.close_time)
        return tuple(decisions), tuple(submitted), tuple(events)

    # --- Context construction -----------------------------------------------------------------

    def _risk_context(
        self, bar: MarketBar, snapshot: PortfolioSnapshot, state: _RunState
    ) -> RiskContext:
        """Assemble everything the risk engine observes, entirely from run state."""
        return RiskContext(
            as_of=bar.close_time,
            health=self._health(bar),
            snapshot=snapshot,
            symbol_rules=self._symbols[bar.symbol],
            reference_price=bar.close,
            latest_bar_close_time=bar.close_time,
            latest_bar_is_closed=bar.is_closed,
            open_order_count=len(self._broker.open_orders()),
            pending_buy_notional=self._pending_buy_notional(state),
            approved_orders_last_hour=self._approvals_within(state, _SECONDS_PER_HOUR, bar),
            approved_orders_today=self._approvals_within(state, _SECONDS_PER_DAY, bar),
            day_start_equity=state.day_start_equity,
            peak_equity=state.peak_equity,
            spread_basis_points=self._config.assumed_spread_basis_points,
            realized_volatility=self._realized_volatility(state.history.get(bar.symbol, ())),
            consecutive_api_failures=0,
            known_idempotency_keys=frozenset(),
        )

    def _realized_volatility(self, history: Sequence[MarketBar]) -> Decimal | None:
        """Return the standard deviation of trailing close-to-close returns.

        Genuinely measurable from bars, unlike spread, so it is computed rather than assumed.
        ``None`` until two returns exist: one observation has no dispersion, and reporting
        zero would tell the risk engine the market was perfectly calm when it was in fact
        unmeasured.
        """
        window = history[-(self._config.volatility_window + 1) :]
        if len(window) < _MIN_BARS_FOR_VOLATILITY:
            return None
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            returns = [
                (later.close - earlier.close) / earlier.close
                for earlier, later in pairwise(window)
                if earlier.close > ZERO
            ]
            if len(returns) < _MIN_RETURNS_FOR_VOLATILITY:
                return None
            mean = sum(returns, start=ZERO) / Decimal(len(returns))
            variance = sum(((value - mean) ** 2 for value in returns), start=ZERO) / Decimal(
                len(returns) - 1
            )
            return variance.sqrt()

    def _pending_buy_notional(self, state: _RunState) -> dict[str, Decimal]:
        """Return the worst-case quote value committed by each symbol's working buy orders.

        The same upper bound the broker reserved: a limit buy at its limit, a market buy at
        its approved cap. Supplying it is what stops two entries approved between one
        another's fills from each seeing untouched headroom.
        """
        pending: dict[str, Decimal] = {}
        for order in self._broker.open_orders():
            if order.side is not OrderSide.BUY:
                continue
            cap = order.limit_price if order.order_type is OrderType.LIMIT else None
            if cap is None:
                approved = state.approved_by_id.get(order.client_order_id)
                cap = approved.max_execution_price if approved is not None else None
            if cap is None:  # pragma: no cover - the order contract guarantees a buy cap
                continue
            pending[order.symbol] = pending.get(order.symbol, ZERO) + cap * order.remaining_quantity
        return pending

    def _approvals_within(self, state: _RunState, seconds: int, bar: MarketBar) -> int:
        """Count authorisations inside a trailing window ending at this bar's close.

        Measured against bar timestamps, never a clock, so the count is identical on replay.
        """
        cutoff = bar.close_time.timestamp() - seconds
        return sum(1 for at in state.approval_times if at.timestamp() > cutoff)

    def _health(self, bar: MarketBar) -> HealthStatus:
        """Return a healthy status derived from the bar being processed.

        A backtest has no venue to be unhealthy and no clock to drift, so the operational
        guards are satisfied by construction. They still run: the point of the chain is that
        every check executes in every mode, not that some are skipped when convenient.
        """
        return HealthStatus(
            state=SystemState.HEALTHY,
            checked_at=bar.close_time,
            components=(),
            circuit_breakers=(),
            reconciliation_status=ReconciliationStatus.IN_SYNC,
            last_bar_close_time=bar.close_time,
            data_age_seconds=0,
            clock_skew_seconds=0.0,
            consecutive_api_failures=0,
            halt_reason=None,
        )

    def _snapshot(self, bar: MarketBar, state: _RunState) -> PortfolioSnapshot:
        """Mark the account at the latest close of every symbol seen so far."""
        marks = dict(state.last_close)
        marks.setdefault(bar.symbol, bar.close)
        return self._portfolio.snapshot(as_of=bar.close_time, mark_prices=marks)

    def _position_state(self, snapshot: PortfolioSnapshot, symbol: str) -> PositionState:
        """Return the coarse position state a strategy is allowed to see."""
        for position in snapshot.positions:
            if position.symbol == symbol and position.is_open:
                return PositionState.LONG
        return PositionState.FLAT

    # --- Accounting -----------------------------------------------------------------------------

    def _account_for_fills(self, fills: Sequence[Fill], bar: MarketBar, state: _RunState) -> None:
        """Accumulate the modelled cost of executing this bar's fills.

        Slippage is measured against the bar's own open, which is what the broker would have
        matched a market order at with slippage switched off. It is the execution cost *this
        model* imposed, not a claim about real market impact. Limit orders are excluded: they
        fill at their limit by definition, and the difference from the open is the edge the
        order was placed to capture, not a cost it paid.
        """
        for fill in fills:
            state.commission += fill.fee
            order = self._broker.get_order(fill.client_order_id)
            if order.order_type is not OrderType.MARKET:
                continue
            if fill.side is OrderSide.BUY:
                state.slippage += (fill.price - bar.open) * fill.quantity
            else:
                state.slippage += (bar.open - fill.price) * fill.quantity

    def _record_equity(self, snapshot: PortfolioSnapshot, state: _RunState) -> None:
        """Append an equity point and update the peak and daily anchors."""
        equity = snapshot.equity
        state.peak_equity = max(state.peak_equity, equity)
        drawdown = ZERO
        if state.peak_equity > ZERO:
            drawdown = (state.peak_equity - equity) / state.peak_equity
        day = snapshot.taken_at.date()
        if state.day_started_at != day:
            state.day_started_at = day
            state.day_start_equity = equity
        state.curve.append(EquityPoint(at=snapshot.taken_at, equity=equity, drawdown=drawdown))

    def _record_closed_trades(self, state: _RunState) -> None:
        """Record a round trip whenever a position lifecycle has just ended.

        A trade is a lifecycle that returned to flat, not a fill: an entry still open has no
        outcome yet, and counting fills would move the win rate every time a position was
        merely scaled into. The result is the lifecycle's own cumulative realised PnL, which
        Phase 3A resets when a new lifecycle begins.
        """
        for position in self._portfolio.positions():
            previous = state.positions.get(position.symbol)
            if previous is not None and previous.is_open and not position.is_open:
                state.trade_results.append(position.realized_pnl)
            state.positions[position.symbol] = position

    # --- Setup and teardown ----------------------------------------------------------------------

    def _validate_contract(self) -> None:
        """Check the strategy and the feature pipeline agree, before any bar is processed.

        A pipeline that cannot produce a feature the strategy declared as required will fail
        on the very first bar that reaches the strategy. Catching it here turns a failure
        buried partway through a long run into one raised before the run starts, which is the
        difference between a misconfiguration and a mystery.

        Raises:
            ConfigurationError: If the risk engine demands a metric this run cannot supply.
            StrategyContextError: If a required feature is not one the pipeline produces.
        """
        if (
            self._risk.config.strict_missing_metrics
            and self._config.assumed_spread_basis_points is None
        ):
            raise ConfigurationError(
                "the risk engine requires a spread metric but this backtest assumes none; "
                "set assumed_spread_basis_points, or disable strict_missing_metrics",
            )
        available = set(self._features.feature_names)
        missing = sorted(set(self._strategy.metadata.required_features) - available)
        if missing:
            raise StrategyContextError(
                "the feature pipeline does not produce every feature the strategy requires",
                strategy_id=self._strategy.metadata.strategy_id,
                missing=missing,
            )

    def _validate(self, bars: Sequence[MarketBar]) -> None:
        """Reject a dataset the run could not be trusted to reproduce.

        Raises:
            DataIntegrityError: If a bar is open, belongs to an unregistered symbol, or
                arrives out of chronological order.
        """
        previous: MarketBar | None = None
        for bar in bars:
            if not bar.is_closed:
                raise DataIntegrityError(
                    "a backtest consumes only closed bars",
                    symbol=bar.symbol,
                    open_time=bar.open_time.isoformat(),
                )
            if bar.symbol not in self._symbols:
                raise DataIntegrityError(
                    "no venue rules are registered for this symbol", symbol=bar.symbol
                )
            if previous is not None and bar.close_time < previous.close_time:
                raise DataIntegrityError(
                    "bars must arrive in non-decreasing close-time order",
                    symbol=bar.symbol,
                    open_time=bar.open_time.isoformat(),
                )
            previous = bar

    def _initial_state(self) -> _RunState:
        """Build the bookkeeping for a fresh run."""
        equity = self._config.initial_capital
        return _RunState(
            curve=[],
            snapshots=[],
            outcomes=[],
            events=[],
            signals=[],
            intents=[],
            decisions=[],
            approved=[],
            orders=[],
            fills=[],
            history={},
            last_close={},
            positions={},
            approved_by_id={},
            approval_times=[],
            peak_equity=equity,
            day_start_equity=equity,
            day_started_at=None,
            commission=ZERO,
            slippage=ZERO,
            trade_results=[],
            calls={
                "feature_computations": 0,
                "strategy_invocations": 0,
                "risk_evaluations": 0,
                "broker_submissions": 0,
                "broker_bars_processed": 0,
                "portfolio_fills_applied": 0,
            },
        )

    def _empty_performance(self) -> PerformanceSummary:
        """Summarise a run over no data: an untouched account and nothing computable."""
        return compute_performance(
            curve=(),
            initial_equity=self._config.initial_capital,
            realized_pnl=ZERO,
            unrealized_pnl=ZERO,
            commission_paid=ZERO,
            slippage_paid=ZERO,
            trades=_trade_statistics(()),
            periods_per_year=self._config.periods_per_year,
            risk_free_rate=self._config.risk_free_rate,
            minimum_periods_for_ratios=self._config.minimum_periods_for_ratios,
        )

    def _build_result(self, state: _RunState) -> BacktestResult:
        """Freeze the run's bookkeeping into an immutable result."""
        final = state.snapshots[-1]
        performance = compute_performance(
            curve=tuple(state.curve),
            initial_equity=self._config.initial_capital,
            realized_pnl=final.realized_pnl,
            unrealized_pnl=final.unrealized_pnl,
            commission_paid=state.commission,
            slippage_paid=state.slippage,
            trades=_trade_statistics(tuple(state.trade_results)),
            periods_per_year=self._config.periods_per_year,
            risk_free_rate=self._config.risk_free_rate,
            minimum_periods_for_ratios=self._config.minimum_periods_for_ratios,
        )
        return BacktestResult(
            config=self._config,
            bars=tuple(state.outcomes),
            equity_curve=tuple(state.curve),
            snapshots=tuple(state.snapshots),
            signals=tuple(state.signals),
            intents=tuple(state.intents),
            decisions=tuple(state.decisions),
            approved_orders=tuple(state.approved),
            orders=tuple(state.orders),
            fills=tuple(state.fills),
            events=tuple(state.events),
            calls=ComponentCallCounts(**state.calls),
            performance=performance,
            started_at=state.outcomes[0].bar.close_time,
            ended_at=state.outcomes[-1].bar.close_time,
        )


def _trade_statistics(results: Sequence[Decimal]) -> TradeStatistics:
    """Summarise closed round trips, leaving ratios undefined when nothing supports them."""
    wins = [value for value in results if value > ZERO]
    losses = [value for value in results if value < ZERO]
    gross_profit = sum(wins, start=ZERO)
    gross_loss = -sum(losses, start=ZERO)
    count = len(results)
    return TradeStatistics(
        count=count,
        wins=len(wins),
        losses=len(losses),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        win_rate=Decimal(len(wins)) / Decimal(count) if count else None,
        average_win=gross_profit / Decimal(len(wins)) if wins else None,
        average_loss=gross_loss / Decimal(len(losses)) if losses else None,
        profit_factor=gross_profit / gross_loss if gross_loss > ZERO else None,
        expectancy=sum(results, start=ZERO) / Decimal(count) if count else None,
    )
