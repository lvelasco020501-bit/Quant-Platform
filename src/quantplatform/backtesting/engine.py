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
from quantplatform.backtesting.intents import build_forced_exit_intent, build_intent
from quantplatform.backtesting.metrics import (
    EquityPoint,
    PerformanceSummary,
    TradeStatistics,
    compute_performance,
    longest_streaks,
)
from quantplatform.backtesting.results import BacktestResult, BarOutcome, ComponentCallCounts
from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.enums import (
    CircuitBreakerReason,
    OrderSide,
    OrderType,
    PositionState,
    ReconciliationStatus,
    SystemState,
)
from quantplatform.core.errors import (
    ConfigurationError,
    DataIntegrityError,
    PositionRiskAmbiguityError,
    StrategyContextError,
)
from quantplatform.core.events import DomainEvent
from quantplatform.core.interfaces import FeaturePipeline
from quantplatform.core.models.health import HealthStatus
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order, OrderIntent
from quantplatform.core.models.portfolio import PortfolioSnapshot, Position
from quantplatform.core.models.risk import (
    CircuitBreakerState,
    PositionRiskState,
    RiskAction,
    RiskContext,
    RiskDecision,
)
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.stops import StopSpecification
from quantplatform.core.models.trades import ClosedTrade
from quantplatform.core.symbol_rules import as_symbol_rules_store
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.risk.engine import StandardRiskEngine
from quantplatform.risk.sizing import open_risk_amount
from quantplatform.strategies.base import BaseStrategy

__all__ = ["BacktestEngine", "RunState"]

_SECONDS_PER_HOUR = 3_600
_SECONDS_PER_DAY = 86_400

_MIN_BARS_FOR_VOLATILITY = 3
"""Three closes yield two returns, the fewest that have any dispersion between them."""

_MIN_RETURNS_FOR_VOLATILITY = 2
"""Bessel's correction needs two observations."""


@dataclass(slots=True)
class RunState:
    """Mutable bookkeeping for one run.

    Owned by the caller rather than by the engine, so two runs on the same engine cannot
    contaminate one another — and so a caller that receives its bars one at a time, as paper
    trading does, can hold the run open across arbitrarily long gaps between them.
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
    position_risk: dict[str, PositionRiskState]
    """What each open position is protected by, keyed by symbol.

    Reconstructed after every fill from the position that actually resulted, never from what
    was requested. Empty unless the risk engine derived a stop — a position with no recorded
    protection has none, which is a true statement where an entry claiming otherwise would
    not be.
    """
    retired_risk: dict[str, PositionRiskState]
    """Risk records whose positions have just gone flat, awaiting their trade record.

    A position's risk is deleted the moment it closes, and the trade that closed it is built
    afterwards — so without somewhere to put the denominator it was already gone by the time
    anything wanted it. Held for exactly one step of one bar and consumed by
    :meth:`_record_closed_trades`, which empties it.
    """

    breakers: list[CircuitBreakerState]
    """Every circuit breaker currently latched, one entry per reason.

    Owned here rather than by the risk engine, which holds no state and reads no clock: what
    an account has lost today, how far it sits below its high and how many attempts in a row
    failed are all facts about *this run*. Risk decides only what a latch means for an order.
    """

    day_start_realized_pnl: Decimal
    """Booked PnL as the reporting day opened, so the daily limit can measure money actually
    lost rather than a marked decline the account has not taken."""

    approval_times: list[datetime]
    initial_equity: Decimal
    """The equity the account genuinely held when the run opened.

    Read from the portfolio, never from configuration. The two used to be separate numbers:
    ``BacktestConfig.initial_capital`` anchored the drawdown while the portfolio held
    whatever it had actually been seeded with, and a paper deployment that seeded nothing
    produced a report claiming a 100% drawdown and a ten-thousand loss that never happened.
    One number, taken from the account itself, makes that unrepresentable.
    """

    peak_equity: Decimal
    day_start_equity: Decimal
    day_started_at: date | None
    commission: Decimal
    slippage: Decimal
    traded_notional: Decimal
    """Quote-asset notional executed across every fill, which turnover divides by equity.

    Accumulated here because nothing else sees every fill. The metric has existed since the
    performance summary did and has reported ``None`` in every run ever made, because no
    caller had this number to give it."""

    exposed_bars: int
    """Bars that closed holding something, for time in market."""
    trade_results: list[ClosedTrade]
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
            A shared
            :class:`~quantplatform.core.symbol_rules.SymbolRulesStore` is held by
            reference, so a refresh reaches this component; any other mapping is copied
            into one, so a caller mutating its own dictionary cannot alter what is traded
            against.
        """
        self._config = config
        self._strategy = strategy
        self._features = features
        self._risk = risk_engine
        self._broker = broker
        self._portfolio = portfolio
        self._symbols = as_symbol_rules_store(symbols)

    @property
    def strategy_id(self) -> str:
        """Return the identifier of the strategy this engine runs."""
        return self._strategy.metadata.strategy_id

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
        self._validate(bars)
        state = self.begin()
        for bar in bars:
            self.advance(bar, state)
        return self.summarise(state)

    # --- Incremental API --------------------------------------------------------------------
    #
    # A backtest receives its bars all at once; paper trading receives them one at a time, over
    # hours or days. Both run the identical chain, so the chain is exposed incrementally and
    # :meth:`run` is simply the loop over it. Duplicating the pipeline for the streaming case
    # would create a second implementation of the trading logic, and the two would drift.

    def begin(self) -> RunState:
        """Open a run and return the state that carries it.

        Args:
            None.

        Returns:
            Fresh bookkeeping for a run that has processed no bars.

        Raises:
            ConfigurationError: If the risk engine demands a metric this run cannot
                supply, or the account holds no equity to trade with.
            StrategyContextError: If the pipeline cannot produce a required feature.
        """
        self._validate_contract()
        self._validate_funded()
        return self._initial_state()

    def advance(self, bar: MarketBar, state: RunState) -> BarOutcome:
        """Run the full chain for one bar and fold the outcome into ``state``.

        Args:
            bar: The next closed bar, at or after the last one this state processed.
            state: Run state returned by :meth:`begin`.

        Returns:
            Everything this bar produced.

        Raises:
            DataIntegrityError: If the bar is open, unknown, or out of order.
        """
        previous = state.outcomes[-1].bar if state.outcomes else None
        self._validate_bar(bar, previous)
        self._process_bar(len(state.outcomes), bar, state)
        return state.outcomes[-1]

    def summarise(self, state: RunState) -> BacktestResult:
        """Freeze a run's state into an immutable result, leaving the state usable.

        Args:
            state: Run state to summarise.

        Returns:
            The result as of every bar processed so far. A run that consumed nothing yields
            an empty result rather than an error, because "no bars yet" is a legitimate state
            for a paper session that has only just started.
        """
        if not state.outcomes:
            return BacktestResult(config=self._config, performance=self._empty_performance())
        return self._build_result(state)

    # --- One iteration ----------------------------------------------------------------------

    def _process_bar(self, index: int, bar: MarketBar, state: RunState) -> None:
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

        # Every fill is now booked, so the position is final for this bar and its risk can
        # be restated from what genuinely exists rather than from what was asked for.
        self._update_position_risk(execution.fills, state)

        history = state.history.setdefault(bar.symbol, [])
        history.append(bar)
        state.last_close[bar.symbol] = bar.close

        features = self._features.compute(history)
        state.calls["feature_computations"] += 1

        snapshot = self._snapshot(bar, state)

        # Closed trades and the equity anchors are folded in *before* anything is authorised.
        # They used to be updated at the end of the bar, which left a breaker fed from them
        # one bar behind: the entry placed on the very bar that broke a limit still went
        # through. One bar is one more position opened by an account already told to stop.
        self._record_closed_trades(state)
        self._update_equity_anchors(snapshot, state)
        self._update_breakers(snapshot, state)

        # Protection is decided before opinion is asked for, and authorised before it. An exit
        # forced by risk must not lose its place in a queue to the very strategy whose position
        # it is closing — and must not have its budget spent by that strategy's next entry.
        forced = self._forced_exit_intents(bar, snapshot, state)
        forced_decisions, forced_submitted, forced_events = self._authorise(
            forced, bar, snapshot, state, forced_exit=True
        )
        events.extend(forced_events)

        # Strictly after the triggers above have been read, and strictly before anything
        # else can read the state again. That ordering *is* the guarantee that a level
        # raised by this bar cannot judge this bar; there is no flag to check, because the
        # new level is simply not present until the next iteration reaches the line above.
        self._advance_position_risk(bar, state, triggered={i.symbol for i in forced})

        signals = self._generate(bar, history, features, snapshot, state)
        intents = self._build_intents(signals, snapshot)
        decisions, submitted, decision_events = self._authorise(intents, bar, snapshot, state)
        events.extend(decision_events)

        intents = forced + intents
        decisions = forced_decisions + decisions
        submitted = forced_submitted + submitted

        state.signals.extend(signals)
        state.intents.extend(intents)
        state.decisions.extend(decisions)
        state.approved.extend(submitted)
        state.events.extend(events)

        final_snapshot = self._snapshot(bar, state)
        if any(position.is_open for position in final_snapshot.positions):
            state.exposed_bars += 1
        self._append_equity_point(final_snapshot, state)
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
        state: RunState,
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

    def _forced_exit_intents(
        self, bar: MarketBar, snapshot: PortfolioSnapshot, state: RunState
    ) -> tuple[OrderIntent, ...]:
        """Ask risk what this bar did to what is already open, and propose the exits it names.

        The engine does not decide anything here: it reads actions and translates them. The
        judgement of whether a stop was breached belongs to the risk engine, which is the only
        component that knows what each position is protected by.

        Whether protection is *obligatory* is the configuration's own answer, not a separate
        switch this method could be left holding the wrong way: a run that sizes by risk is a
        run where every open position must be accounted for. Under V1 nothing derives a stop,
        so an unprotected position is ordinary and nothing is required of it.

        Returns:
            One exit intent per position risk decided must be closed, empty on the ordinary
            bar where nothing breached anything.
        """
        actions = self._risk.evaluate_open_positions(
            positions=self._portfolio.positions(),
            position_risk=state.position_risk,
            bar=bar,
            require_protection=self._risk.config.risk_v2_active,
        )
        intents: list[OrderIntent] = []
        for action in actions:
            intent = self._forced_exit_intent(action, snapshot, bar)
            if intent is not None:
                intents.append(intent)
        return tuple(intents)

    def _advance_position_risk(
        self, bar: MarketBar, state: RunState, *, triggered: set[str]
    ) -> None:
        """Move each open position's anchor and protective level on to the next bar.

        Runs every bar, on every open position, whether or not anything filled. The other
        update path — :meth:`_update_position_risk` — restates a position after its own
        fills, so it visits only symbols that traded; a position held quietly through a
        rally would record none of it, and a trailing stop reads exactly that record.
        """
        if not state.position_risk:
            return
        state.position_risk.update(
            self._risk.advance_position_risk(
                positions=self._portfolio.positions(),
                position_risk=state.position_risk,
                bar=bar,
                triggered=triggered,
            )
        )

    def _forced_exit_intent(
        self, action: RiskAction, snapshot: PortfolioSnapshot, bar: MarketBar
    ) -> OrderIntent | None:
        """Build the market exit that carries out one risk action."""
        symbol = action.symbol if action.symbol is not None else bar.symbol
        return build_forced_exit_intent(
            action,
            snapshot=snapshot,
            bar_close_time=bar.close_time,
            execution_mode=self._config.execution_mode,
            market_type=self._symbols[symbol].market_type,
        )

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
        state: RunState,
        *,
        forced_exit: bool = False,
    ) -> tuple[tuple[RiskDecision, ...], tuple[ApprovedOrder, ...], tuple[DomainEvent, ...]]:
        """Evaluate every intent and submit whatever the risk engine authorised.

        A rejection is an ordinary outcome and never stops the run: refusing one intent says
        nothing about the next. A broker refusal is equally ordinary — the venue declining an
        order is information, not a crash.

        Args:
            intents: Proposals to evaluate, in the order they were produced.
            bar: The closed bar the decision is made from.
            snapshot: Account state the decision is made against.
            state: Run state, updated with what was submitted.
            forced_exit: Whether these intents carry out a risk action. Withdraws the veto of
                the administrative frequency limits only; every rule that decides whether the
                venue could accept the order still blocks.

        Returns:
            The decisions taken, the orders the broker accepted, and the events both produced.
        """
        decisions: list[RiskDecision] = []
        submitted: list[ApprovedOrder] = []
        events: list[DomainEvent] = []
        for intent in intents:
            context = self._risk_context(bar, snapshot, state)
            assessment = self._risk.assess(intent, context, forced_exit=forced_exit)
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
        self, bar: MarketBar, snapshot: PortfolioSnapshot, state: RunState
    ) -> RiskContext:
        """Assemble everything the risk engine observes, entirely from run state."""
        working = self._broker.open_orders()
        return RiskContext(
            as_of=bar.close_time,
            health=self._health(bar),
            snapshot=snapshot,
            symbol_rules=self._symbols[bar.symbol],
            reference_price=bar.close,
            latest_bar_close_time=bar.close_time,
            latest_bar_is_closed=bar.is_closed,
            open_order_count=len(working),
            open_order_symbols=frozenset(order.symbol for order in working),
            pending_buy_notional=self._pending_buy_notional(state),
            approved_orders_last_hour=self._approvals_within(state, _SECONDS_PER_HOUR, bar),
            approved_orders_today=self._approvals_within(state, _SECONDS_PER_DAY, bar),
            day_start_equity=state.day_start_equity,
            peak_equity=state.peak_equity,
            breakers=tuple(state.breakers),
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

    def _pending_buy_notional(self, state: RunState) -> dict[str, Decimal]:
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

    def _approvals_within(self, state: RunState, seconds: int, bar: MarketBar) -> int:
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

    def _snapshot(self, bar: MarketBar, state: RunState) -> PortfolioSnapshot:
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

    def _account_for_fills(self, fills: Sequence[Fill], bar: MarketBar, state: RunState) -> None:
        """Accumulate the modelled cost of executing this bar's fills.

        Slippage is measured against the bar's own open, which is what the broker would have
        matched a market order at with slippage switched off. It is the execution cost *this
        model* imposed, not a claim about real market impact. Limit orders are excluded: they
        fill at their limit by definition, and the difference from the open is the edge the
        order was placed to capture, not a cost it paid.
        """
        for fill in fills:
            state.commission += fill.fee
            state.traded_notional += fill.notional
            order = self._broker.get_order(fill.client_order_id)
            if order.order_type is not OrderType.MARKET:
                continue
            if fill.side is OrderSide.BUY:
                state.slippage += (fill.price - bar.open) * fill.quantity
            else:
                state.slippage += (bar.open - fill.price) * fill.quantity

    def _update_position_risk(self, fills: Sequence[Fill], state: RunState) -> None:
        """Restate what each touched position is protected by, from the position itself.

        Called once per bar, after the portfolio has booked every fill it produced, so the
        position read here is final: partial fills, several fills of one order, and a
        reduction all arrive as one settled outcome rather than a sequence to be replayed.

        The risk recorded is computed from ``avg_entry_price`` and the size that actually
        remains — never from the sizing that preceded execution. Those differ whenever a cap
        reduced the order, lot rounding moved it, or the fill landed away from the reference
        price, and persisting the earlier figure would describe a position that was never
        opened.

        A position reduced to flat loses its record: nothing is protecting it, and a stale
        entry would go on claiming otherwise.

        Raises:
            PositionRiskAmbiguityError: If a fill adds to a position whose recorded stop
                disagrees with the one its approving order carried. See the error for why
                this fails rather than choosing between them.
        """
        held = {position.symbol: position for position in self._portfolio.positions()}
        for symbol in {fill.symbol for fill in fills}:
            position = held.get(symbol)
            if position is None or not position.is_open:
                retired = state.position_risk.pop(symbol, None)
                if retired is not None:
                    state.retired_risk[symbol] = retired
                continue
            stop = self._approved_stop(symbol, fills, state)
            existing = state.position_risk.get(symbol)
            if stop is None:
                # A reduction, or an entry that carried no stop. An existing record is
                # rescaled to what remains; absent one, there is nothing to record.
                if existing is None:
                    continue
                stop = existing.stop
            elif existing is not None and existing.stop != stop:
                msg = "this fill's protective stop and the position's recorded stop differ"
                raise PositionRiskAmbiguityError(
                    msg,
                    symbol=symbol,
                    recorded_stop=str(existing.stop.trigger_price),
                    incoming_stop=str(stop.trigger_price),
                )
            entry = position.avg_entry_price
            opened_at = position.opened_at
            if entry is None or opened_at is None:
                msg = "an open position carries neither an entry price nor an opening time"
                raise DataIntegrityError(
                    msg,
                    symbol=symbol,
                    has_entry_price=entry is not None,
                    has_opened_at=opened_at is not None,
                )
            current = open_risk_amount(
                quantity=position.quantity,
                avg_entry_price=entry,
                stop=stop,
                policy=self._risk.config.execution_policy,
            )
            if current is None or current <= ZERO:
                # Nothing computable, so nothing is claimed. Recording a position as
                # protected without being able to say by how much is the failure this
                # whole layer exists to prevent.
                state.position_risk.pop(symbol, None)
                continue
            state.position_risk[symbol] = PositionRiskState(
                symbol=symbol,
                stop=stop,
                quantity=position.quantity,
                # Carried, never restated: what the position opened risking is the only
                # denominator under which two trades can be compared.
                initial_risk_amount=(
                    existing.initial_risk_amount if existing is not None else current
                ),
                current_risk_amount=current,
                entry_price=entry,
                # Carried, not recomputed. The favourable extreme is a fact about the whole
                # life of the position, and a rebuild that dropped it would restart a
                # trailing stop from entry on every fill — trailing only the bars since the
                # last one, while reporting that it had trailed throughout.
                highest_price_seen=existing.highest_price_seen if existing is not None else None,
                opened_at=existing.opened_at if existing is not None else opened_at,
            )

    def _approved_stop(
        self, symbol: str, fills: Sequence[Fill], state: RunState
    ) -> StopSpecification | None:
        """Return the protective stop the orders behind this bar's buys were approved under.

        Only buys are consulted: a sell reduces a position and cannot change what the
        remaining size is protected at. Several fills of one order share a client order id
        and therefore one stop, which is why this looks the stop up rather than accumulating
        one per fill.
        """
        for fill in fills:
            if fill.symbol != symbol or fill.side is not OrderSide.BUY:
                continue
            approved = state.approved_by_id.get(fill.client_order_id)
            if approved is not None and approved.protective_stop is not None:
                return approved.protective_stop
        return None

    def _update_equity_anchors(self, snapshot: PortfolioSnapshot, state: RunState) -> None:
        """Move the peak and the day's opening marks, and clear the day's own latch.

        Split from :meth:`_append_equity_point` so the anchors a breaker measures against are
        current before anything is authorised, while the curve still records the account as
        the bar finally left it. Taking the peak is idempotent, so running it in both places
        cannot move a number.

        The daily reset lives here because this is where a new reporting day is recognised.
        It is the single exception to a latch not clearing itself, and it is the metric's own
        definition rather than the process deciding conditions have improved: a daily limit
        that never resets is a total limit wearing the wrong name. A structural latch —
        a drawdown, a losing streak — survives untouched, which is why each reason latches
        separately.
        """
        equity = snapshot.equity
        state.peak_equity = max(state.peak_equity, equity)
        day = snapshot.taken_at.date()
        if state.day_started_at == day:
            return
        state.day_started_at = day
        state.day_start_equity = equity
        state.day_start_realized_pnl = snapshot.realized_pnl
        state.breakers = [
            breaker
            for breaker in state.breakers
            if breaker.reason is not CircuitBreakerReason.DAILY_LOSS_LIMIT
        ]

    def _append_equity_point(self, snapshot: PortfolioSnapshot, state: RunState) -> None:
        """Record where the account finished this bar."""
        equity = snapshot.equity
        state.peak_equity = max(state.peak_equity, equity)
        drawdown = ZERO
        if state.peak_equity > ZERO:
            drawdown = (state.peak_equity - equity) / state.peak_equity
        state.curve.append(EquityPoint(at=snapshot.taken_at, equity=equity, drawdown=drawdown))

    def _update_breakers(self, snapshot: PortfolioSnapshot, state: RunState) -> None:
        """Latch whatever this bar's arithmetic says has broken, and latch it once.

        A latched breaker is never un-latched here. Re-deriving the condition each bar and
        clearing it when it stops holding would turn a halt into a pause, and the condition
        that halted the account is exactly the condition under which a process should not be
        deciding on its own that things have improved.

        Nothing is closed. The breakers gate new exposure; the stop closes what is open.
        """
        config = self._risk.config
        latched = {breaker.reason for breaker in state.breakers}
        at = snapshot.taken_at

        if config.max_daily_loss_pct is not None and state.day_start_equity > ZERO:
            lost = state.day_start_realized_pnl - snapshot.realized_pnl
            if lost > ZERO:
                with localcontext() as ctx:
                    ctx.prec = DECIMAL_WORKING_PRECISION
                    fraction = lost / state.day_start_equity
                if fraction >= config.max_daily_loss_pct:
                    self._latch(
                        state,
                        latched,
                        CircuitBreakerReason.DAILY_LOSS_LIMIT,
                        at=at,
                        daily_loss=lost,
                    )

        if config.latch_total_drawdown and state.peak_equity > ZERO:
            with localcontext() as ctx:
                ctx.prec = DECIMAL_WORKING_PRECISION
                drawdown = (state.peak_equity - snapshot.equity) / state.peak_equity
            if drawdown >= config.max_total_drawdown_pct:
                self._latch(state, latched, CircuitBreakerReason.EXCESSIVE_DRAWDOWN, at=at)

        if config.max_consecutive_losses is not None:
            streak = 0
            for trade in reversed(state.trade_results):
                if trade.realized_pnl >= ZERO:
                    break
                streak += 1
            if streak >= config.max_consecutive_losses:
                self._latch(
                    state,
                    latched,
                    CircuitBreakerReason.CONSECUTIVE_LOSSES,
                    at=at,
                    consecutive_losses=streak,
                )

    def _latch(
        self,
        state: RunState,
        latched: set[CircuitBreakerReason | None],
        reason: CircuitBreakerReason,
        *,
        at: datetime,
        daily_loss: Decimal = ZERO,
        consecutive_losses: int = 0,
    ) -> None:
        """Record one halt, keeping at most one entry per reason."""
        if reason in latched:
            return
        latched.add(reason)
        state.breakers.append(
            CircuitBreakerState(
                tripped_at=at,
                reason=reason,
                daily_loss=daily_loss,
                consecutive_losses=consecutive_losses,
            )
        )

    def _record_closed_trades(self, state: RunState) -> None:
        """Record a round trip whenever a position lifecycle has just ended.

        A trade is a lifecycle that returned to flat, not a fill: an entry still open has no
        outcome yet, and counting fills would move the win rate every time a position was
        merely scaled into. The result is the lifecycle's own cumulative realised PnL, which
        Phase 3A resets when a new lifecycle begins.
        """
        for position in self._portfolio.positions():
            previous = state.positions.get(position.symbol)
            if previous is not None and previous.is_open and not position.is_open:
                retired = state.retired_risk.pop(position.symbol, None)
                state.trade_results.append(
                    ClosedTrade(
                        symbol=position.symbol,
                        realized_pnl=position.realized_pnl,
                        initial_risk_amount=(
                            retired.initial_risk_amount if retired is not None else None
                        ),
                        opened_at=retired.opened_at
                        if retired is not None
                        else (previous.opened_at or position.updated_at),
                        closed_at=position.updated_at,
                    )
                )
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
            self._validate_bar(bar, previous)
            previous = bar

    def _validate_bar(self, bar: MarketBar, previous: MarketBar | None) -> None:
        """Check one bar against the run's integrity rules.

        Raises:
            DataIntegrityError: If the bar is open, unknown to this engine, or precedes the
                bar before it.
        """
        if not bar.is_closed:
            raise DataIntegrityError(
                "only closed bars may be processed",
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

    def _validate_funded(self) -> None:
        """Refuse to open a run against an account holding nothing.

        A run with no equity does not fail — it goes quiet, which is far worse. Sizing an
        entry asks for a share of equity, a share of nothing is nothing, and ``build_intent``
        discards the signal before it ever becomes an order intent. There is no rejection, no
        reason recorded, and nothing for a report to describe: the risk engine is never even
        consulted. A week of that produces immaculate green reports for a session that never
        placed a single order, which is exactly what happened.

        Declaring capital in the run configuration is not evidence that the account was
        seeded with it. This is where the two meet, so this is where they are checked.

        Raises:
            ConfigurationError: If the account holds no equity in the quote asset.
        """
        equity = self._opening_equity()
        if equity > ZERO:
            return
        raise ConfigurationError(
            "the account holds no equity, so every signal would be discarded before it "
            "became an order intent and the run would report perfect health while trading "
            "nothing; seed the portfolio with the capital the run configuration declares",
            run_id=self._config.run_id,
            quote_asset=self._config.quote_asset,
            declared_capital=str(self._config.initial_capital),
            account_equity=str(equity),
        )

    def _opening_equity(self) -> Decimal:
        """Return the equity the account holds as the run opens.

        Taken from the portfolio, which is the only thing that knows what the account is
        actually worth. ``BacktestConfig.initial_capital`` is the value a composition root
        *seeds* with; it is not evidence that the seeding happened, and treating it as such
        is what let a paper session report losing money it never had.

        Positions are always empty at this point — the portfolio engine is flat-start by
        construction and refuses a seeded base-asset balance — so the quote-asset total is
        the whole of the account's worth and no mark prices are needed to value it.
        """
        quote = self._config.quote_asset
        return sum(
            (balance.total for balance in self._portfolio.balances() if balance.asset == quote),
            ZERO,
        )

    def _initial_state(self) -> RunState:
        """Build the bookkeeping for a fresh run."""
        equity = self._opening_equity()
        return RunState(
            initial_equity=equity,
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
            position_risk={},
            retired_risk={},
            breakers=[],
            day_start_realized_pnl=ZERO,
            approval_times=[],
            peak_equity=equity,
            day_start_equity=equity,
            day_started_at=None,
            commission=ZERO,
            slippage=ZERO,
            traded_notional=ZERO,
            exposed_bars=0,
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
            initial_equity=self._opening_equity(),
            realized_pnl=ZERO,
            unrealized_pnl=ZERO,
            commission_paid=ZERO,
            slippage_paid=ZERO,
            trades=_trade_statistics(()),
            periods_per_year=self._config.periods_per_year,
            risk_free_rate=self._config.risk_free_rate,
            minimum_periods_for_ratios=self._config.minimum_periods_for_ratios,
            traded_notional=ZERO,
        )

    def _build_result(self, state: RunState) -> BacktestResult:
        """Freeze the run's bookkeeping into an immutable result."""
        final = state.snapshots[-1]
        performance = compute_performance(
            curve=tuple(state.curve),
            initial_equity=state.initial_equity,
            realized_pnl=final.realized_pnl,
            unrealized_pnl=final.unrealized_pnl,
            commission_paid=state.commission,
            slippage_paid=state.slippage,
            trades=_trade_statistics(tuple(state.trade_results)),
            periods_per_year=self._config.periods_per_year,
            risk_free_rate=self._config.risk_free_rate,
            minimum_periods_for_ratios=self._config.minimum_periods_for_ratios,
            traded_notional=state.traded_notional,
            exposed_bars=state.exposed_bars,
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
            trades=tuple(state.trade_results),
            events=tuple(state.events),
            calls=ComponentCallCounts(**state.calls),
            performance=performance,
            started_at=state.outcomes[0].bar.close_time,
            ended_at=state.outcomes[-1].bar.close_time,
        )


def _trade_statistics(trades: Sequence[ClosedTrade]) -> TradeStatistics:
    """Summarise closed round trips, leaving ratios undefined when nothing supports them."""
    results = [trade.realized_pnl for trade in trades]
    multiples = [
        multiple for multiple in (trade.r_multiple for trade in trades) if multiple is not None
    ]
    streak_wins, streak_losses = longest_streaks(trades)
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
        max_consecutive_wins=streak_wins,
        max_consecutive_losses=streak_losses,
        # R is reported only over the trades that recorded a denominator. A run where none
        # did reports nothing rather than zero: zero would read as "every trade broke even
        # against its risk", which is a claim about trading rather than about missing data.
        average_r=sum(multiples, start=ZERO) / Decimal(len(multiples)) if multiples else None,
        expectancy_r=sum(multiples, start=ZERO) / Decimal(len(multiples)) if multiples else None,
    )
