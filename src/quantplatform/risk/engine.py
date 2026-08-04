"""The risk engine: final authority over every order intent.

:class:`StandardRiskEngine` is the only component permitted to produce an
:class:`~quantplatform.core.models.orders.ApprovedOrder`, and it is the last place a trade
can be stopped before it reaches a venue. Everything it does is a pure function of the intent
and the context it is handed: it reads no clock, opens no connection, touches no balance and
writes nothing. Feeding the same intent and context in twice produces byte-identical
decisions, which is what makes a rejection reproducible months later from stored inputs.

**Every check is evaluated, not just the first failure.** A decision records the complete
list — passed, failed and skipped — because "why was this rejected" is almost never answered
well by a single reason, and an operator tuning limits needs to see which other constraints
the intent was also close to. Evaluation stops early only where continuing is impossible
rather than merely redundant: once sizing has determined that no valid quantity exists, the
checks that would divide by that quantity are recorded as skipped instead of computed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.enums import (
    MarketType,
    OrderSide,
    OrderType,
    RiskCheckCode,
    RiskCheckSeverity,
    RiskCheckStatus,
    RiskOutcome,
    SystemState,
)
from quantplatform.core.errors import RiskInvariantError
from quantplatform.core.events import RiskDecisionMade
from quantplatform.core.ids import client_order_id_from_key, deterministic_uuid
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, OrderIntent
from quantplatform.core.models.portfolio import PortfolioSnapshot, Position
from quantplatform.core.models.risk import RiskCheckResult, RiskContext, RiskDecision
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.sizing import (
    market_buy_price_cap,
    normalize_limit_price,
    normalize_quantity,
    quantity_for_notional,
)

__all__ = ["RiskEvaluationResult", "StandardRiskEngine"]

_MAX_REASON_LENGTH = 500


@dataclass(frozen=True, slots=True)
class _Sizing:
    """Everything the sizing stage determined about how large the order may be."""

    requested_quantity: Decimal | None
    """The raw quantity the intent asked for, unrounded; ``None`` when it was sized by
    notional instead and no quantity was ever promised."""

    unconstrained_quantity: Decimal
    """What the intent alone implied, after lot rounding but before any risk limit."""

    quantity: Decimal
    limit_price: Decimal | None
    max_execution_price: Decimal | None
    reference_price: Decimal
    """Price used to value the order: the normalised limit for a limit order, the cap for a
    market buy, the reference price for a market sell."""

    @property
    def notional(self) -> Decimal:
        """Return the quote-asset value this sizing implies."""
        return self.quantity * self.reference_price

    @property
    def was_reduced(self) -> bool:
        """Return whether the account is getting less than the intent asked for.

        Rounding a quantity down onto the venue lot grid counts: a strategy that asked for
        0.1234 and is authorised for 0.12 did not get what it requested, and reporting that
        as an unmodified approval would hide a real change from whoever reads the decision.
        """
        if self.requested_quantity is not None and self.quantity < self.requested_quantity:
            return True
        return self.quantity < self.unconstrained_quantity


@dataclass(frozen=True, slots=True)
class RiskEvaluationResult:
    """A decision together with the events it produced.

    Returned in addition to the :class:`~quantplatform.core.interfaces.RiskEngine` protocol's
    :meth:`StandardRiskEngine.evaluate`, which returns the decision alone. Callers that need
    the events, or need to know whether the decision was freshly computed, use
    :meth:`StandardRiskEngine.assess`.
    """

    decision: RiskDecision
    events: tuple[RiskDecisionMade, ...]
    replayed: bool
    """``True`` when this intent had already been decided and the stored decision was
    returned unchanged; no new events are produced in that case."""


class _Recorder:
    """Accumulates check results in evaluation order.

    Owns the sequence numbering so that a check's position in the fixed order is recorded
    rather than inferred from list position, which survives serialisation and re-sorting.
    """

    def __init__(self, *, at: datetime) -> None:
        self._at = at
        self._results: list[RiskCheckResult] = []

    def record(
        self,
        code: RiskCheckCode,
        *,
        passed: bool,
        message: str,
        observed: Decimal | None = None,
        limit: Decimal | None = None,
        severity: RiskCheckSeverity = RiskCheckSeverity.BLOCKING,
        metadata: dict[str, str] | None = None,
    ) -> bool:
        """Record a check outcome and return whether it passed."""
        status = RiskCheckStatus.PASSED if passed else RiskCheckStatus.FAILED
        self._append(code, status, message, observed, limit, severity, metadata)
        return passed

    def skip(
        self,
        code: RiskCheckCode,
        *,
        message: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Record that a check was not applicable or could not be computed."""
        self._append(
            code, RiskCheckStatus.SKIPPED, message, None, None, RiskCheckSeverity.ADVISORY, metadata
        )

    def _append(
        self,
        code: RiskCheckCode,
        status: RiskCheckStatus,
        message: str,
        observed: Decimal | None,
        limit: Decimal | None,
        severity: RiskCheckSeverity,
        metadata: dict[str, str] | None,
    ) -> None:
        self._results.append(
            RiskCheckResult(
                code=code,
                status=status,
                severity=severity,
                sequence=len(self._results),
                message=message[:_MAX_REASON_LENGTH],
                observed=observed,
                limit=limit,
                metadata=dict(metadata) if metadata else {},
                evaluated_at=self._at,
            )
        )

    @property
    def results(self) -> tuple[RiskCheckResult, ...]:
        """Return every recorded check, in evaluation order."""
        return tuple(self._results)

    @property
    def blocked(self) -> bool:
        """Return whether any blocking check has failed so far."""
        return any(result.blocks for result in self._results)

    def reasons(self) -> tuple[str, ...]:
        """Return the message of every blocking failure, in evaluation order."""
        return tuple(result.message for result in self._results if result.blocks)


class StandardRiskEngine:
    """Evaluates order intents against a fixed, ordered set of risk checks.

    **Outcomes.** ``APPROVED`` when the requested size survives untouched, ``RESIZED`` when a
    smaller valid order remains, ``REJECTED`` when none does. Sizing only ever reduces:
    there is no path through this engine that increases what a strategy asked for.

    **Idempotency.** Decisions are keyed by the intent's idempotency key. Re-evaluating a key
    the engine has already decided returns that exact decision, with no events and no
    recomputation, so a retry after a crash cannot produce a second approved order for one
    logical intent. An intent whose key is listed in
    :attr:`~quantplatform.core.models.risk.RiskContext.known_idempotency_keys` but is unknown
    to this engine — decided by a previous process, say — is rejected as a duplicate rather
    than re-approved, because the engine cannot see what that earlier decision authorised.

    **Side effects.** None, other than the in-memory decision history described above, which
    is written only after a complete decision has been built. The engine never submits an
    order, reserves funds, touches a balance or a position, computes PnL, or reads a
    credential.
    """

    def __init__(self, *, config: RiskConfiguration) -> None:
        """Construct an engine bound to one set of limits.

        Args:
            config: The limits every intent is evaluated against.
        """
        self._config = config
        self._decisions: dict[str, RiskDecision] = {}

    @property
    def config(self) -> RiskConfiguration:
        """Return the configuration this engine evaluates against.

        Read-only, and exposed so a composition root can check its own assumptions against
        the engine's before a run — for example, that a backtest supplies every metric the
        engine is configured to require.
        """
        return self._config

    # --- RiskEngine protocol ----------------------------------------------------------------

    def evaluate(self, intent: OrderIntent, context: RiskContext) -> RiskDecision:
        """Approve, resize or reject an order intent.

        Satisfies :class:`~quantplatform.core.interfaces.RiskEngine`. Use :meth:`assess` when
        the emitted events or the replay flag are needed.
        """
        return self.assess(intent, context).decision

    # --- Extended API -----------------------------------------------------------------------

    def assess(self, intent: OrderIntent, context: RiskContext) -> RiskEvaluationResult:
        """Evaluate an intent and return the decision together with its events.

        Args:
            intent: Proposed order to evaluate.
            context: Observable system, market and portfolio state.

        Returns:
            The decision, the ``RiskDecisionMade`` event it produced, and whether the
            decision was replayed from history rather than freshly computed.

        Raises:
            RiskInvariantError: If the engine would emit a decision that contradicts its own
                invariants. Never raised for an ordinary rejection.
        """
        replayed = self._decisions.get(intent.idempotency_key)
        if replayed is not None:
            return RiskEvaluationResult(decision=replayed, events=(), replayed=True)

        decision = self._decide(intent, context)
        self._decisions[intent.idempotency_key] = decision
        event = RiskDecisionMade(
            event_id=deterministic_uuid("event", "risk_decision_made", str(decision.decision_id)),
            occurred_at=context.as_of,
            source="risk_engine",
            correlation_id=intent.intent_id,
            decision=decision,
        )
        return RiskEvaluationResult(decision=decision, events=(event,), replayed=False)

    # --- Decision construction ----------------------------------------------------------------

    def _decide(self, intent: OrderIntent, context: RiskContext) -> RiskDecision:
        """Run every check in order and assemble the resulting decision."""
        recorder = _Recorder(at=context.as_of)
        self._check_operational(recorder, context)
        self._check_duplication(recorder, intent, context)
        self._check_frequency(recorder, context)
        self._check_drawdown(recorder, context)
        self._check_market_conditions(recorder, context)
        self._check_instrument(recorder, intent, context)

        sizing = self._size(recorder, intent, context)
        if sizing is not None:
            self._check_venue_bounds(recorder, sizing, context.symbol_rules)
            self._check_balance(recorder, intent, sizing, context)
            self._check_exposure(recorder, intent, sizing, context)

        decision_id = self._decision_id(intent, sizing)
        if recorder.blocked or sizing is None:
            return self._reject(intent, context, recorder, decision_id)
        return self._approve(intent, context, recorder, decision_id, sizing)

    def _reject(
        self,
        intent: OrderIntent,
        context: RiskContext,
        recorder: _Recorder,
        decision_id: object,
    ) -> RiskDecision:
        """Build a rejected decision carrying every reason it was refused."""
        reasons = recorder.reasons()
        if not reasons:  # pragma: no cover - guarded by the caller's `blocked` test
            raise RiskInvariantError("a rejection must record at least one blocking failure")
        return RiskDecision(
            decision_id=decision_id,  # type: ignore[arg-type]
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            outcome=RiskOutcome.REJECTED,
            checks=recorder.results,
            requested_quantity=intent.requested_quantity,
            approved_order=None,
            rejection_reasons=reasons,
            decided_at=context.as_of,
        )

    def _approve(
        self,
        intent: OrderIntent,
        context: RiskContext,
        recorder: _Recorder,
        decision_id: object,
        sizing: _Sizing,
    ) -> RiskDecision:
        """Build an approved or resized decision and the order it authorises."""
        outcome = RiskOutcome.RESIZED if sizing.was_reduced else RiskOutcome.APPROVED
        order = ApprovedOrder(
            client_order_id=client_order_id_from_key(intent.idempotency_key),
            intent_id=intent.intent_id,
            decision_id=decision_id,  # type: ignore[arg-type]
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            symbol=intent.symbol,
            market_type=intent.market_type,
            side=intent.side,
            order_type=intent.order_type,
            quantity=sizing.quantity,
            limit_price=sizing.limit_price,
            stop_price=None,
            max_execution_price=sizing.max_execution_price,
            time_in_force=intent.time_in_force,
            execution_mode=intent.execution_mode,
            idempotency_key=intent.idempotency_key,
            approved_at=context.as_of,
        )
        return RiskDecision(
            decision_id=decision_id,  # type: ignore[arg-type]
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            outcome=outcome,
            checks=recorder.results,
            requested_quantity=sizing.requested_quantity,
            approved_order=order,
            rejection_reasons=(),
            decided_at=context.as_of,
        )

    def _decision_id(self, intent: OrderIntent, sizing: _Sizing | None) -> object:
        """Derive the decision id from the intent and the size actually authorised.

        The final quantity is part of the derivation so that a resized decision has an id
        distinct from the approval of the same intent at full size: two different
        authorisations must never share an identifier, even when they came from one intent.
        """
        quantity = str(sizing.quantity) if sizing is not None else "rejected"
        return deterministic_uuid("risk_decision", intent.idempotency_key, quantity)

    # --- Operational checks -------------------------------------------------------------------

    def _check_operational(self, recorder: _Recorder, context: RiskContext) -> None:
        """Evaluate system state, configuration, data freshness and health."""
        state = context.health.state
        permitted = state is SystemState.HEALTHY or (
            state is SystemState.DEGRADED and self._config.allow_degraded_state
        )
        recorder.record(
            RiskCheckCode.SYSTEM_STATE,
            passed=permitted,
            message=f"system state is {state.value}",
            metadata={
                "state": state.value,
                "allow_degraded": str(self._config.allow_degraded_state),
            },
        )
        recorder.record(
            RiskCheckCode.CONFIGURATION_VALID,
            passed=True,
            message="risk configuration validated at construction",
        )
        recorder.record(
            RiskCheckCode.EXECUTION_MODE,
            passed=True,
            message=f"execution mode {context.snapshot.execution_mode.value} accepted",
            metadata={"mode": context.snapshot.execution_mode.value},
        )

        age = Decimal(str(context.data_age_seconds))
        recorder.record(
            RiskCheckCode.DATA_FRESHNESS,
            passed=age <= self._config.stale_market_data_seconds,
            message="market data is stale"
            if age > self._config.stale_market_data_seconds
            else "market data is fresh",
            observed=age,
            limit=Decimal(self._config.stale_market_data_seconds),
        )
        recorder.record(
            RiskCheckCode.CLOSED_CANDLE,
            passed=context.latest_bar_is_closed,
            message="the latest bar is still open"
            if not context.latest_bar_is_closed
            else "the latest bar is closed",
        )
        rules_age = Decimal(str(context.symbol_rules_age_seconds))
        recorder.record(
            RiskCheckCode.SYMBOL_RULES_FRESHNESS,
            passed=rules_age <= self._config.stale_symbol_rules_seconds,
            message="venue symbol rules are stale"
            if rules_age > self._config.stale_symbol_rules_seconds
            else "venue symbol rules are fresh",
            observed=rules_age,
            limit=Decimal(self._config.stale_symbol_rules_seconds),
        )

        unhealthy = [
            component.name for component in context.health.components if not component.healthy
        ]
        recorder.record(
            RiskCheckCode.EXCHANGE_HEALTH,
            passed=not unhealthy,
            message=f"unhealthy components: {', '.join(sorted(unhealthy))}"
            if unhealthy
            else "all reported components are healthy",
            metadata={"unhealthy": ",".join(sorted(unhealthy))} if unhealthy else None,
        )
        reconciled = context.health.reconciliation_status.allows_trading
        recorder.record(
            RiskCheckCode.RECONCILIATION_STATUS,
            passed=reconciled,
            message=f"reconciliation status is {context.health.reconciliation_status.value}",
            metadata={"status": context.health.reconciliation_status.value},
        )
        failures = Decimal(context.consecutive_api_failures)
        recorder.record(
            RiskCheckCode.CONSECUTIVE_API_FAILURES,
            passed=failures < self._config.max_consecutive_api_failures,
            message="consecutive api failures reached the configured threshold"
            if failures >= self._config.max_consecutive_api_failures
            else "api failure count is within budget",
            observed=failures,
            limit=Decimal(self._config.max_consecutive_api_failures),
        )

    def _check_duplication(
        self, recorder: _Recorder, intent: OrderIntent, context: RiskContext
    ) -> None:
        """Evaluate duplicate intents, pending-order capacity and symbol conflicts."""
        already_known = intent.idempotency_key in context.known_idempotency_keys
        recorder.record(
            RiskCheckCode.DUPLICATE_SIGNAL,
            passed=not already_known,
            message="this idempotency key was already decided elsewhere"
            if already_known
            else "intent is not a duplicate",
        )
        recorder.record(
            RiskCheckCode.PENDING_ORDERS,
            passed=context.open_order_count < self._config.max_open_orders,
            message="the maximum number of working orders is already open"
            if context.open_order_count >= self._config.max_open_orders
            else "working-order capacity is available",
            observed=Decimal(context.open_order_count),
            limit=Decimal(self._config.max_open_orders),
        )
        conflicting = intent.symbol in context.open_order_symbols
        recorder.record(
            RiskCheckCode.CONFLICTING_ORDER,
            passed=not conflicting,
            message=f"an order is already working on {intent.symbol}"
            if conflicting
            else "no conflicting order on this symbol",
            metadata={"symbol": intent.symbol},
        )

    def _check_frequency(self, recorder: _Recorder, context: RiskContext) -> None:
        """Evaluate the hourly and daily order-rate limits.

        Both counts come from the context, which the orchestration layer derives from
        decisions that actually authorised an order. A rejected intent therefore never
        consumes budget, and a replayed intent is never counted twice, because a replay does
        not reach this code at all.
        """
        recorder.record(
            RiskCheckCode.MAX_HOURLY_ORDERS,
            passed=context.approved_orders_last_hour < self._config.max_orders_per_hour,
            message="the hourly order limit is exhausted"
            if context.approved_orders_last_hour >= self._config.max_orders_per_hour
            else "hourly order budget is available",
            observed=Decimal(context.approved_orders_last_hour),
            limit=Decimal(self._config.max_orders_per_hour),
        )
        recorder.record(
            RiskCheckCode.MAX_DAILY_ORDERS,
            passed=context.approved_orders_today < self._config.max_orders_per_day,
            message="the daily order limit is exhausted"
            if context.approved_orders_today >= self._config.max_orders_per_day
            else "daily order budget is available",
            observed=Decimal(context.approved_orders_today),
            limit=Decimal(self._config.max_orders_per_day),
        )

    def _check_drawdown(self, recorder: _Recorder, context: RiskContext) -> None:
        """Evaluate the daily and total drawdown limits.

        Drawdown is a positive fraction lost from the relevant peak. A peak of zero is not a
        drawdown of zero — it is an account with no reference to measure against — so it is
        treated as missing rather than silently passing.
        """
        equity = context.snapshot.equity
        self._record_drawdown(
            recorder,
            RiskCheckCode.DAILY_DRAWDOWN,
            equity=equity,
            peak=context.day_start_equity,
            limit=self._config.max_daily_drawdown_pct,
            label="daily",
        )
        self._record_drawdown(
            recorder,
            RiskCheckCode.TOTAL_DRAWDOWN,
            equity=equity,
            peak=context.peak_equity,
            limit=self._config.max_total_drawdown_pct,
            label="total",
        )

    def _record_drawdown(
        self,
        recorder: _Recorder,
        code: RiskCheckCode,
        *,
        equity: Decimal,
        peak: Decimal,
        limit: Decimal,
        label: str,
    ) -> None:
        """Record one drawdown check, honouring the strict-missing-metrics policy."""
        if peak <= ZERO:
            if self._config.strict_missing_metrics:
                recorder.record(
                    code,
                    passed=False,
                    message=f"{label} drawdown cannot be evaluated without a positive peak",
                )
            else:
                recorder.skip(code, message=f"{label} drawdown peak is unavailable")
            return
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            drawdown = (peak - equity) / peak
        drawdown = max(drawdown, ZERO)
        recorder.record(
            code,
            passed=drawdown <= limit,
            message=f"{label} drawdown exceeds its limit"
            if drawdown > limit
            else f"{label} drawdown is within its limit",
            observed=drawdown,
            limit=limit,
        )

    def _check_market_conditions(self, recorder: _Recorder, context: RiskContext) -> None:
        """Evaluate the spread and volatility guards."""
        self._record_optional_metric(
            recorder,
            RiskCheckCode.EXCESSIVE_SPREAD,
            value=context.spread_basis_points,
            limit=self._config.max_spread_bps,
            label="spread",
        )
        self._record_optional_metric(
            recorder,
            RiskCheckCode.EXCESSIVE_VOLATILITY,
            value=context.realized_volatility,
            limit=self._config.max_volatility,
            label="realized volatility",
        )

    def _record_optional_metric(
        self,
        recorder: _Recorder,
        code: RiskCheckCode,
        *,
        value: Decimal | None,
        limit: Decimal,
        label: str,
    ) -> None:
        """Record a guard over a metric the context may not carry."""
        if value is None:
            if self._config.strict_missing_metrics:
                recorder.record(
                    code,
                    passed=False,
                    message=f"{label} is unavailable and strict mode requires it",
                    limit=limit,
                )
            else:
                recorder.skip(code, message=f"{label} is unavailable")
            return
        recorder.record(
            code,
            passed=value <= limit,
            message=f"{label} exceeds its limit"
            if value > limit
            else f"{label} is within its limit",
            observed=value,
            limit=limit,
        )

    def _check_instrument(
        self, recorder: _Recorder, intent: OrderIntent, context: RiskContext
    ) -> None:
        """Evaluate symbol, market type, side, order type and time in force."""
        rules = context.symbol_rules
        recorder.record(
            RiskCheckCode.ALLOWED_SYMBOL,
            passed=intent.symbol == rules.symbol,
            message="the intent symbol does not match the supplied venue rules"
            if intent.symbol != rules.symbol
            else "symbol is permitted",
            metadata={"intent_symbol": intent.symbol, "rules_symbol": rules.symbol},
        )
        spot = intent.market_type is MarketType.SPOT and rules.market_type is MarketType.SPOT
        recorder.record(
            RiskCheckCode.ALLOWED_MARKET_TYPE,
            passed=spot,
            message="only spot markets are permitted" if not spot else "market type is permitted",
            metadata={"market_type": intent.market_type.value},
        )
        recorder.record(
            RiskCheckCode.LEVERAGE_PROHIBITED,
            passed=not intent.market_type.allows_leverage,
            message="the requested market type permits leverage"
            if intent.market_type.allows_leverage
            else "no leverage is available on this market type",
        )
        recorder.record(
            RiskCheckCode.SHORT_SELLING_PROHIBITED,
            passed=not intent.market_type.allows_short,
            message="the requested market type permits short selling"
            if intent.market_type.allows_short
            else "short selling is unavailable on this market type",
        )
        allowed_type = self._config.permits_order_type(intent.order_type)
        recorder.record(
            RiskCheckCode.ALLOWED_ORDER_TYPE,
            passed=allowed_type,
            message=f"order type {intent.order_type.value} is not permitted"
            if not allowed_type
            else "order type is permitted",
            metadata={"order_type": intent.order_type.value},
        )
        allowed_tif = intent.time_in_force in self._config.allowed_time_in_force
        recorder.record(
            RiskCheckCode.ALLOWED_TIME_IN_FORCE,
            passed=allowed_tif,
            message=f"time in force {intent.time_in_force.value} is not permitted"
            if not allowed_tif
            else "time in force is permitted",
            metadata={"time_in_force": intent.time_in_force.value},
        )
        recorder.record(
            RiskCheckCode.REFERENCE_PRICE,
            passed=context.reference_price > ZERO,
            message="the reference price is not usable"
            if context.reference_price <= ZERO
            else "reference price is usable",
            observed=context.reference_price,
        )

    # --- Sizing --------------------------------------------------------------------------------

    def _size(
        self, recorder: _Recorder, intent: OrderIntent, context: RiskContext
    ) -> _Sizing | None:
        """Determine the largest valid quantity, or ``None`` when none exists."""
        rules = context.symbol_rules
        limit_price = (
            normalize_limit_price(intent.limit_price, intent.side, rules)
            if intent.limit_price is not None
            else None
        )
        if intent.limit_price is not None and limit_price is not None:
            recorder.record(
                RiskCheckCode.PRICE_PRECISION,
                passed=limit_price > ZERO,
                message="the limit price rounds to zero on the venue tick grid"
                if limit_price <= ZERO
                else "limit price normalised to the venue tick grid",
                observed=limit_price,
                metadata={
                    "requested": str(intent.limit_price),
                    "normalized": str(limit_price),
                    "direction": "down" if intent.side is OrderSide.BUY else "up",
                },
            )
        else:
            recorder.skip(
                RiskCheckCode.PRICE_PRECISION, message="market orders carry no limit price"
            )

        cap = self._market_buy_cap(recorder, intent, context)
        valuation = self._valuation_price(intent, limit_price, cap, context.reference_price)

        requested = self._requested_quantity(intent, valuation, rules)
        quantity = self._constrain(requested, intent, valuation, context)

        recorder.record(
            RiskCheckCode.QUANTITY_PRECISION,
            passed=quantity > ZERO,
            message="no venue-valid quantity remains after rounding and limits"
            if quantity <= ZERO
            else "quantity normalised to the venue lot grid",
            observed=quantity,
            metadata={
                "requested": str(requested),
                "normalized": str(quantity),
                "delta": str(requested - quantity),
            },
        )
        if quantity <= ZERO:
            return None
        if intent.order_type is OrderType.MARKET and intent.side is OrderSide.BUY and cap is None:
            return None
        return _Sizing(
            requested_quantity=intent.requested_quantity,
            unconstrained_quantity=requested,
            quantity=quantity,
            limit_price=limit_price,
            max_execution_price=cap,
            reference_price=valuation,
        )

    def _requested_quantity(
        self, intent: OrderIntent, valuation: Decimal, rules: SymbolRules
    ) -> Decimal:
        """Return the venue-valid quantity the intent asked for, however it expressed it."""
        if intent.requested_quantity is not None:
            return normalize_quantity(intent.requested_quantity, rules)
        notional = intent.requested_notional
        if notional is None:  # pragma: no cover - the intent model enforces exactly one
            return ZERO
        return quantity_for_notional(notional, valuation, rules)

    def _valuation_price(
        self,
        intent: OrderIntent,
        limit_price: Decimal | None,
        cap: Decimal | None,
        reference: Decimal,
    ) -> Decimal:
        """Return the price an order of this shape must be sized and funded against.

        A buy is always valued at the worst price it may pay — its limit, or its cap — so
        that every downstream balance and exposure check is computed against the largest
        debit the order can incur rather than an optimistic one.
        """
        if limit_price is not None:
            return limit_price
        if intent.side is OrderSide.BUY and cap is not None:
            return cap
        return reference

    def _constrain(
        self,
        requested: Decimal,
        intent: OrderIntent,
        valuation: Decimal,
        context: RiskContext,
    ) -> Decimal:
        """Reduce a requested quantity to the largest size every limit permits."""
        rules = context.symbol_rules
        allowed = requested
        for candidate in (
            self._venue_maximum(rules),
            self._order_notional_maximum(valuation),
            self._balance_maximum(intent, valuation, context),
            self._exposure_maximum(intent, valuation, context),
        ):
            if candidate is not None:
                allowed = min(allowed, candidate)
        return normalize_quantity(allowed, rules) if allowed < requested else requested

    def _venue_maximum(self, rules: SymbolRules) -> Decimal | None:
        """Return the venue's own quantity ceiling, if it declares one."""
        return rules.max_quantity

    def _order_notional_maximum(self, valuation: Decimal) -> Decimal | None:
        """Return the quantity implied by the per-order notional ceiling."""
        if self._config.max_order_notional <= ZERO or valuation <= ZERO:
            return None
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            return self._config.max_order_notional / valuation

    def _balance_maximum(
        self, intent: OrderIntent, valuation: Decimal, context: RiskContext
    ) -> Decimal | None:
        """Return the quantity the available balance can actually fund.

        Only ``free`` balance counts. Funds already reserved against a working order are, by
        construction, spoken for; treating them as available is how an account ends up with
        two orders competing for the same money.
        """
        snapshot = context.snapshot
        rules = context.symbol_rules
        if intent.side is OrderSide.BUY:
            free = _free_balance(snapshot, rules.quote_asset)
            if valuation <= ZERO:
                return None
            return self._affordable_quantity(free, valuation)
        return _free_balance(snapshot, rules.base_asset)

    def _affordable_quantity(self, free: Decimal, valuation: Decimal) -> Decimal | None:
        """Return the largest quantity ``free`` can fund at ``valuation``, fees included.

        Solved rather than approximated, because a flat fee is not expressible as a rate: at
        a notional of 20 a flat 3 is 1500 basis points, and treating it as one would either
        starve large orders or under-fund small ones. Under the basis-point model the fee
        scales with size, so the affordable quantity divides by a per-unit cost; under the
        flat model the fee is subtracted once before dividing.
        """
        if valuation <= ZERO:
            return None
        fee = self._config.execution_policy.fee
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            rate = fee.maximum_fee(valuation)
            flat = fee.maximum_fee(ZERO)
            if flat > ZERO:
                spendable = free - flat
                return spendable / valuation if spendable > ZERO else ZERO
            unit_cost = valuation + rate
            return free / unit_cost if unit_cost > ZERO else None

    def _exposure_maximum(
        self, intent: OrderIntent, valuation: Decimal, context: RiskContext
    ) -> Decimal | None:
        """Return the quantity the exposure ceilings permit a buy to add.

        A sell reduces exposure and is never constrained by these limits.
        """
        if intent.side is OrderSide.SELL or valuation <= ZERO:
            return None
        snapshot = context.snapshot
        symbol_headroom = (
            self._config.max_symbol_exposure
            - _symbol_exposure(snapshot, intent.symbol)
            - context.pending_buy_notional.get(intent.symbol, ZERO)
        )
        portfolio_headroom = (
            (snapshot.equity * self._config.max_portfolio_exposure_pct)
            - snapshot.gross_exposure
            - _total_pending_buy_notional(context)
        )
        headroom = min(symbol_headroom, portfolio_headroom)
        if headroom <= ZERO:
            return ZERO
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            return headroom / valuation

    def _market_buy_cap(
        self, recorder: _Recorder, intent: OrderIntent, context: RiskContext
    ) -> Decimal | None:
        """Compute the maximum execution price a market buy may carry.

        ``reference_price * (1 + total_buffer_bps / 10_000)``, rounded up to the venue tick.
        The buffer covers the broker's configured slippage plus a spread and safety allowance,
        and the configuration refuses to construct if it is smaller than that slippage — so a
        cap produced here can never be one the broker would immediately breach.
        """
        if intent.order_type is not OrderType.MARKET or intent.side is not OrderSide.BUY:
            recorder.skip(
                RiskCheckCode.MARKET_BUY_CAP,
                message="a price cap applies only to market buys",
            )
            return None
        if context.reference_price <= ZERO:
            recorder.record(
                RiskCheckCode.MARKET_BUY_CAP,
                passed=False,
                message="a market buy cannot be capped without a usable reference price",
            )
            return None
        buffer_bps = self._config.total_market_buy_buffer_bps
        cap = market_buy_price_cap(context.reference_price, buffer_bps, context.symbol_rules)
        broker_worst = self._config.execution_policy.slippage.worst_buy_price(
            context.reference_price
        )
        sufficient = cap >= broker_worst
        recorder.record(
            RiskCheckCode.MARKET_BUY_CAP,
            passed=sufficient,
            message="the configured buffer does not cover the broker's worst-case price"
            if not sufficient
            else "market-buy cap covers the broker's worst-case execution price",
            observed=cap,
            limit=broker_worst,
            metadata={
                "reference_price": str(context.reference_price),
                "buffer_bps": str(buffer_bps),
                "required_buffer_bps": str(self._config.minimum_required_buffer_bps),
            },
        )
        return cap if sufficient else None

    # --- Post-sizing checks -----------------------------------------------------------------------

    def _check_venue_bounds(self, recorder: _Recorder, sizing: _Sizing, rules: SymbolRules) -> None:
        """Evaluate the venue's quantity and notional bounds against the sized order."""
        quantity = sizing.quantity
        recorder.record(
            RiskCheckCode.MINIMUM_QUANTITY,
            passed=quantity >= rules.min_quantity,
            message="the sized quantity is below the venue minimum"
            if quantity < rules.min_quantity
            else "quantity meets the venue minimum",
            observed=quantity,
            limit=rules.min_quantity,
        )
        if rules.max_quantity is None:
            recorder.skip(
                RiskCheckCode.MAXIMUM_QUANTITY, message="the venue declares no quantity ceiling"
            )
        else:
            recorder.record(
                RiskCheckCode.MAXIMUM_QUANTITY,
                passed=quantity <= rules.max_quantity,
                message="the sized quantity exceeds the venue maximum"
                if quantity > rules.max_quantity
                else "quantity is within the venue maximum",
                observed=quantity,
                limit=rules.max_quantity,
            )

        notional = sizing.notional
        recorder.record(
            RiskCheckCode.MINIMUM_NOTIONAL,
            passed=notional >= rules.min_notional,
            message="the sized notional is below the venue minimum"
            if notional < rules.min_notional
            else "notional meets the venue minimum",
            observed=notional,
            limit=rules.min_notional,
        )
        if rules.max_notional is None:
            recorder.skip(
                RiskCheckCode.MAXIMUM_NOTIONAL, message="the venue declares no notional ceiling"
            )
        else:
            recorder.record(
                RiskCheckCode.MAXIMUM_NOTIONAL,
                passed=notional <= rules.max_notional,
                message="the sized notional exceeds the venue maximum"
                if notional > rules.max_notional
                else "notional is within the venue maximum",
                observed=notional,
                limit=rules.max_notional,
            )
        recorder.record(
            RiskCheckCode.MAX_ORDER_NOTIONAL,
            passed=notional <= self._config.max_order_notional,
            message="the sized notional exceeds the configured per-order limit"
            if notional > self._config.max_order_notional
            else "notional is within the configured per-order limit",
            observed=notional,
            limit=self._config.max_order_notional,
        )

    def _check_balance(
        self,
        recorder: _Recorder,
        intent: OrderIntent,
        sizing: _Sizing,
        context: RiskContext,
    ) -> None:
        """Evaluate whether the account can actually fund the sized order."""
        snapshot = context.snapshot
        rules = context.symbol_rules
        if intent.side is OrderSide.BUY:
            free = _free_balance(snapshot, rules.quote_asset)
            required = sizing.notional + self._config.execution_policy.fee.maximum_fee(
                sizing.notional
            )
            recorder.record(
                RiskCheckCode.AVAILABLE_BALANCE,
                passed=free >= required,
                message="available quote balance does not cover the order and its fees"
                if free < required
                else "available quote balance covers the order",
                observed=free,
                limit=required,
                metadata={"asset": rules.quote_asset},
            )
            recorder.skip(
                RiskCheckCode.ACCOUNTING_INVARIANT,
                message="a buy does not draw on an existing position",
            )
            return

        free = _free_balance(snapshot, rules.base_asset)
        position = _position_for(snapshot, intent.symbol)
        held = position.quantity if position is not None else ZERO
        recorder.record(
            RiskCheckCode.AVAILABLE_BALANCE,
            passed=free >= sizing.quantity and held >= sizing.quantity,
            message="available base balance or open position does not cover the sale"
            if free < sizing.quantity or held < sizing.quantity
            else "available base balance and position cover the sale",
            observed=min(free, held),
            limit=sizing.quantity,
            metadata={"asset": rules.base_asset, "free": str(free), "position": str(held)},
        )
        total = _total_balance(snapshot, rules.base_asset)
        reconciled = position is None or position.quantity == total
        recorder.record(
            RiskCheckCode.ACCOUNTING_INVARIANT,
            passed=reconciled,
            message="the open position disagrees with the base-asset balance"
            if not reconciled
            else "position and base balance reconcile",
            observed=held,
            limit=total,
        )

    def _check_exposure(
        self,
        recorder: _Recorder,
        intent: OrderIntent,
        sizing: _Sizing,
        context: RiskContext,
    ) -> None:
        """Evaluate position-count and exposure ceilings against the projected position."""
        snapshot = context.snapshot
        # A position limit must count positions that are already spoken for, not only those
        # that exist. Two buys on different new symbols, each evaluated before the other
        # fills, would otherwise both see an empty book and both be approved.
        projected_symbols = _projected_position_symbols(intent, context)
        holds_symbol = _position_for(snapshot, intent.symbol) is not None
        opens_new = intent.side is OrderSide.BUY and not holds_symbol
        within_limit = len(projected_symbols) <= self._config.max_open_positions
        recorder.record(
            RiskCheckCode.MAX_POSITION_COUNT,
            passed=within_limit,
            message="opening another position would exceed the configured maximum"
            if not within_limit
            else "projected position count is within the configured maximum",
            observed=Decimal(len(projected_symbols)),
            limit=Decimal(self._config.max_open_positions),
            metadata={
                "open": str(snapshot.open_position_count),
                "pending": str(len(context.pending_buy_notional)),
                "opens_new": str(opens_new),
            },
        )

        if intent.side is OrderSide.SELL:
            recorder.skip(
                RiskCheckCode.MAX_SYMBOL_EXPOSURE, message="a sale reduces symbol exposure"
            )
            recorder.skip(RiskCheckCode.MAX_EXPOSURE, message="a sale reduces portfolio exposure")
            return

        pending_symbol = context.pending_buy_notional.get(intent.symbol, ZERO)
        projected_symbol = (
            _symbol_exposure(snapshot, intent.symbol) + pending_symbol + sizing.notional
        )
        recorder.record(
            RiskCheckCode.MAX_SYMBOL_EXPOSURE,
            passed=projected_symbol <= self._config.max_symbol_exposure,
            message="projected symbol exposure exceeds its limit"
            if projected_symbol > self._config.max_symbol_exposure
            else "projected symbol exposure is within its limit",
            observed=projected_symbol,
            limit=self._config.max_symbol_exposure,
            metadata={"pending": str(pending_symbol)},
        )

        equity = snapshot.equity
        if equity <= ZERO:
            recorder.record(
                RiskCheckCode.MAX_EXPOSURE,
                passed=False,
                message="portfolio exposure cannot be evaluated against zero equity",
                observed=snapshot.gross_exposure + sizing.notional,
            )
            return
        projected_portfolio = (
            snapshot.gross_exposure + _total_pending_buy_notional(context) + sizing.notional
        )
        ceiling = equity * self._config.max_portfolio_exposure_pct
        recorder.record(
            RiskCheckCode.MAX_EXPOSURE,
            passed=projected_portfolio <= ceiling,
            message="projected portfolio exposure exceeds its limit"
            if projected_portfolio > ceiling
            else "projected portfolio exposure is within its limit",
            observed=projected_portfolio,
            limit=ceiling,
        )


def _total_pending_buy_notional(context: RiskContext) -> Decimal:
    """Return the quote-asset value already committed by every working buy order."""
    return sum(context.pending_buy_notional.values(), start=ZERO)


def _projected_position_symbols(intent: OrderIntent, context: RiskContext) -> frozenset[str]:
    """Return every symbol that will hold a position once working orders resolve.

    The union of what is open now, what a pending buy is about to open, and the symbol this
    intent would open. A buy on a symbol already in that union adds to a position rather than
    creating one, so the union does not grow and the count is unchanged. A sell never adds a
    symbol: it can only reduce or close what is already there.
    """
    open_symbols = {position.symbol for position in context.snapshot.positions if position.is_open}
    projected = open_symbols | set(context.pending_buy_notional)
    if intent.side is OrderSide.BUY:
        projected.add(intent.symbol)
    return frozenset(projected)


def _free_balance(snapshot: PortfolioSnapshot, asset: str) -> Decimal:
    """Return the spendable amount of an asset, zero when the snapshot omits it."""
    for balance in snapshot.balances:
        if balance.asset == asset:
            return balance.free
    return ZERO


def _total_balance(snapshot: PortfolioSnapshot, asset: str) -> Decimal:
    """Return the owned amount of an asset, zero when the snapshot omits it."""
    for balance in snapshot.balances:
        if balance.asset == asset:
            return balance.total
    return ZERO


def _position_for(snapshot: PortfolioSnapshot, symbol: str) -> Position | None:
    """Return the open position on a symbol, if one exists."""
    for position in snapshot.positions:
        if position.symbol == symbol and position.is_open:
            return position
    return None


def _symbol_exposure(snapshot: PortfolioSnapshot, symbol: str) -> Decimal:
    """Return the marked value currently held in one symbol."""
    position = _position_for(snapshot, symbol)
    if position is None:
        return ZERO
    mark = snapshot.mark_prices.get(symbol)
    return position.market_value(mark) if mark is not None else position.cost_basis
