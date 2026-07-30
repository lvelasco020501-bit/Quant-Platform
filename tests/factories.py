"""Deterministic builders and test doubles shared across the suite."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar, Final
from uuid import UUID

from pydantic import BaseModel

from quantplatform.core.enums import (
    ExecutionMode,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionState,
    ReconciliationStatus,
    RiskCheckCode,
    RiskCheckStatus,
    RiskOutcome,
    SignalAction,
    SystemState,
    Timeframe,
    TimeInForce,
)
from quantplatform.core.ids import client_order_id_from_key, deterministic_uuid, idempotency_key
from quantplatform.core.models.health import HealthStatus
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order, OrderIntent
from quantplatform.core.models.portfolio import Balance, PortfolioSnapshot, Position
from quantplatform.core.models.risk import RiskCheckResult, RiskContext, RiskDecision
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.core.timeutils import bar_close_time
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.strategies.base import BaseStrategy

ANCHOR: Final[datetime] = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
SYMBOL: Final[str] = "BTC/USDT"
TIMEFRAME: Final[Timeframe] = Timeframe.H1

_STRATEGY_ID: Final[str] = "dummy_trend"
_STRATEGY_VERSION: Final[str] = "1.0.0"


def make_symbol_rules(
    *,
    symbol: str = SYMBOL,
    base_asset: str = "BTC",
    quote_asset: str = "USDT",
    market_type: MarketType = MarketType.SPOT,
    price_tick: Decimal = Decimal("0.01"),
    quantity_step: Decimal = Decimal("0.00001"),
    min_quantity: Decimal | None = None,
    min_notional: Decimal = Decimal(10),
    max_quantity: Decimal | None = None,
) -> SymbolRules:
    return SymbolRules(
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        market_type=market_type,
        price_tick=price_tick,
        quantity_step=quantity_step,
        min_quantity=min_quantity if min_quantity is not None else quantity_step,
        max_quantity=max_quantity,
        min_notional=min_notional,
        max_notional=None,
        source="test",
        updated_at=ANCHOR,
    )


def make_bar(
    *,
    index: int = 0,
    close: Decimal = Decimal(50_000),
    open_price: Decimal | None = None,
    high: Decimal | None = None,
    low: Decimal | None = None,
    volume: Decimal = Decimal(10),
    timeframe: Timeframe = TIMEFRAME,
    symbol: str = SYMBOL,
    is_closed: bool = True,
) -> MarketBar:
    open_time = ANCHOR + timedelta(seconds=timeframe.seconds * index)
    opened = open_price if open_price is not None else close
    return MarketBar(
        symbol=symbol,
        market_type=MarketType.SPOT,
        timeframe=timeframe,
        open_time=open_time,
        close_time=bar_close_time(open_time, timeframe),
        open=opened,
        high=high if high is not None else max(opened, close),
        low=low if low is not None else min(opened, close),
        close=close,
        volume=volume,
        quote_volume=None,
        trade_count=None,
        source="test",
        is_closed=is_closed,
    )


def make_bars(
    closes: Sequence[Decimal], *, timeframe: Timeframe = TIMEFRAME
) -> tuple[MarketBar, ...]:
    return tuple(
        make_bar(index=index, close=close, timeframe=timeframe)
        for index, close in enumerate(closes)
    )


def make_context(
    *,
    closes: Sequence[Decimal] = (Decimal(50_000), Decimal(51_000)),
    features: Mapping[str, Decimal] | None = None,
    position_state: PositionState = PositionState.FLAT,
    timeframe: Timeframe = TIMEFRAME,
) -> StrategyContext:
    bars = make_bars(closes, timeframe=timeframe)
    return StrategyContext(
        symbol=SYMBOL,
        market_type=MarketType.SPOT,
        timeframe=timeframe,
        as_of=bars[-1].close_time,
        bars=bars,
        features=dict(features if features is not None else {"ema_fast": Decimal(50_500)}),
        position_state=position_state,
        symbol_rules=make_symbol_rules(),
    )


def make_intent(
    *,
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal | None = Decimal("0.1"),
    notional: Decimal | None = None,
    action: SignalAction = SignalAction.ENTER_LONG,
    execution_mode: ExecutionMode = ExecutionMode.PAPER,
    signal_time: datetime = ANCHOR,
) -> OrderIntent:
    key = idempotency_key(
        strategy_id=_STRATEGY_ID,
        strategy_version=_STRATEGY_VERSION,
        symbol=SYMBOL,
        signal_time=signal_time,
        action=action,
        execution_mode=execution_mode,
    )
    return OrderIntent(
        intent_id=deterministic_uuid("order_intent", key),
        signal_id=deterministic_uuid("signal", key),
        strategy_id=_STRATEGY_ID,
        strategy_version=_STRATEGY_VERSION,
        symbol=SYMBOL,
        market_type=MarketType.SPOT,
        side=side,
        order_type=OrderType.MARKET,
        requested_quantity=quantity,
        requested_notional=notional,
        limit_price=None,
        stop_price=None,
        time_in_force=TimeInForce.GTC,
        execution_mode=execution_mode,
        idempotency_key=key,
        reason="test intent",
        created_at=signal_time,
    )


def make_approved_order(
    intent: OrderIntent,
    decision_id: UUID,
    *,
    quantity: Decimal = Decimal("0.1"),
) -> ApprovedOrder:
    return ApprovedOrder(
        client_order_id=client_order_id_from_key(intent.idempotency_key),
        intent_id=intent.intent_id,
        decision_id=decision_id,
        strategy_id=intent.strategy_id,
        strategy_version=intent.strategy_version,
        symbol=intent.symbol,
        market_type=intent.market_type,
        side=intent.side,
        order_type=intent.order_type,
        quantity=quantity,
        limit_price=None,
        stop_price=None,
        time_in_force=intent.time_in_force,
        execution_mode=intent.execution_mode,
        idempotency_key=intent.idempotency_key,
        approved_at=intent.created_at,
    )


def make_check(
    code: RiskCheckCode = RiskCheckCode.SYSTEM_STATE,
    status: RiskCheckStatus = RiskCheckStatus.PASSED,
) -> RiskCheckResult:
    return RiskCheckResult(
        code=code,
        status=status,
        message=f"{code.value} {status.value}",
        observed=None,
        limit=None,
        evaluated_at=ANCHOR,
    )


def make_decision(
    *,
    outcome: RiskOutcome = RiskOutcome.APPROVED,
    intent: OrderIntent | None = None,
    approved_quantity: Decimal | None = None,
    checks: Sequence[RiskCheckResult] | None = None,
) -> RiskDecision:
    resolved_intent = intent if intent is not None else make_intent()
    decision_id = deterministic_uuid("risk_decision", str(resolved_intent.intent_id), outcome.value)
    requested = resolved_intent.requested_quantity
    order = None
    reasons: tuple[str, ...] = ()
    resolved_checks = tuple(checks) if checks is not None else (make_check(),)

    if outcome is RiskOutcome.REJECTED:
        if checks is None:
            resolved_checks = (make_check(RiskCheckCode.AVAILABLE_BALANCE, RiskCheckStatus.FAILED),)
        reasons = ("insufficient balance",)
    else:
        quantity = approved_quantity if approved_quantity is not None else requested
        assert quantity is not None
        order = make_approved_order(resolved_intent, decision_id, quantity=quantity)

    return RiskDecision(
        decision_id=decision_id,
        intent_id=resolved_intent.intent_id,
        strategy_id=resolved_intent.strategy_id,
        outcome=outcome,
        checks=resolved_checks,
        requested_quantity=requested,
        approved_order=order,
        rejection_reasons=reasons,
        decided_at=resolved_intent.created_at,
    )


def make_order(
    *,
    status: OrderStatus = OrderStatus.OPEN,
    quantity: Decimal = Decimal("0.1"),
    filled_quantity: Decimal = Decimal(0),
    avg_fill_price: Decimal | None = None,
    reject_reason: str | None = None,
    cancel_reason: str | None = None,
) -> Order:
    intent = make_intent()
    decision = make_decision(intent=intent)
    assert decision.approved_order is not None
    return Order(
        order_id=deterministic_uuid("order", decision.approved_order.client_order_id),
        client_order_id=decision.approved_order.client_order_id,
        venue_order_id=None,
        intent_id=intent.intent_id,
        decision_id=decision.decision_id,
        strategy_id=intent.strategy_id,
        symbol=intent.symbol,
        market_type=intent.market_type,
        side=intent.side,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        execution_mode=intent.execution_mode,
        status=status,
        quantity=quantity,
        filled_quantity=filled_quantity,
        avg_fill_price=avg_fill_price,
        limit_price=None,
        stop_price=None,
        fees_paid=Decimal(0),
        reject_reason=reject_reason,
        cancel_reason=cancel_reason,
        created_at=ANCHOR,
        updated_at=ANCHOR,
    )


def make_fill(
    *,
    symbol: str = SYMBOL,
    quantity: Decimal = Decimal("0.1"),
    price: Decimal = Decimal(50_000),
    fee: Decimal = Decimal(5),
    fee_asset: str = "USDT",
    side: OrderSide = OrderSide.BUY,
    executed_at: datetime = ANCHOR,
) -> Fill:
    order = make_order()
    return Fill(
        fill_id=deterministic_uuid(
            "fill",
            str(order.order_id),
            str(quantity),
            str(price),
            side.value,
            executed_at.isoformat(),
        ),
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        venue_trade_id=None,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
        fee_asset=fee_asset,
        execution_mode=order.execution_mode,
        is_maker=False,
        executed_at=executed_at,
    )


def make_balance(
    *,
    asset: str = "USDT",
    free: Decimal = Decimal(100_000),
    locked: Decimal = Decimal(0),
    updated_at: datetime = ANCHOR,
) -> Balance:
    return Balance(asset=asset, free=free, locked=locked, updated_at=updated_at)


def make_position(
    *,
    quantity: Decimal = Decimal("0.1"),
    avg_entry_price: Decimal | None = Decimal(50_000),
) -> Position:
    is_open = quantity > 0
    return Position(
        symbol=SYMBOL,
        base_asset="BTC",
        quote_asset="USDT",
        quantity=quantity,
        avg_entry_price=avg_entry_price if is_open else None,
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=ANCHOR if is_open else None,
        updated_at=ANCHOR,
    )


def make_snapshot(
    *,
    cash: Decimal = Decimal(5_000),
    positions: Sequence[Position] = (),
    mark_prices: Mapping[str, Decimal] | None = None,
) -> PortfolioSnapshot:
    resolved_marks = dict(mark_prices) if mark_prices is not None else {SYMBOL: Decimal(50_000)}
    return PortfolioSnapshot(
        snapshot_id=deterministic_uuid("snapshot", ANCHOR.isoformat(), str(cash)),
        taken_at=ANCHOR,
        execution_mode=ExecutionMode.PAPER,
        quote_asset="USDT",
        cash=cash,
        balances=(Balance(asset="USDT", free=cash, locked=Decimal(0), updated_at=ANCHOR),),
        positions=tuple(positions),
        mark_prices=resolved_marks,
        realized_pnl=Decimal(0),
        total_fees=Decimal(0),
    )


def make_portfolio_engine(
    *,
    quote_asset: str = "USDT",
    symbols: Mapping[str, SymbolRules] | None = None,
    initial_balances: Sequence[Balance] = (),
    execution_mode: ExecutionMode = ExecutionMode.PAPER,
) -> SpotPortfolioEngine:
    resolved_symbols = symbols if symbols is not None else {SYMBOL: make_symbol_rules()}
    return SpotPortfolioEngine(
        quote_asset=quote_asset,
        symbols=resolved_symbols,
        execution_mode=execution_mode,
        initial_balances=initial_balances,
        source="test",
    )


def make_health(
    *,
    state: SystemState = SystemState.HEALTHY,
    halt_reason: str | None = None,
) -> HealthStatus:
    return HealthStatus(
        state=state,
        checked_at=ANCHOR,
        components=(),
        circuit_breakers=(),
        reconciliation_status=ReconciliationStatus.IN_SYNC,
        last_bar_close_time=ANCHOR,
        data_age_seconds=0,
        clock_skew_seconds=0.0,
        consecutive_api_failures=0,
        halt_reason=halt_reason,
    )


def make_risk_context(
    *,
    snapshot: PortfolioSnapshot | None = None,
    health: HealthStatus | None = None,
) -> RiskContext:
    resolved_snapshot = snapshot if snapshot is not None else make_snapshot()
    return RiskContext(
        as_of=ANCHOR,
        health=health if health is not None else make_health(),
        snapshot=resolved_snapshot,
        symbol_rules=make_symbol_rules(),
        reference_price=Decimal(50_000),
        latest_bar_close_time=ANCHOR,
        latest_bar_is_closed=True,
        open_order_count=0,
        orders_in_last_hour=0,
        orders_today=0,
        day_start_equity=resolved_snapshot.equity,
        peak_equity=resolved_snapshot.equity,
        spread_basis_points=Decimal(1),
        realized_volatility=Decimal("0.01"),
        consecutive_api_failures=0,
        known_idempotency_keys=frozenset(),
    )


class DummyParams(BaseModel):
    """Parameter schema of the test strategy."""

    fast_period: int = 2
    slow_period: int = 5


class DummyStrategy(BaseStrategy):
    """Minimal strategy used to exercise the contract, registry and boundaries."""

    METADATA: ClassVar[StrategyMetadata] = StrategyMetadata(
        strategy_id=_STRATEGY_ID,
        version=_STRATEGY_VERSION,
        name="Dummy trend",
        description="Emits a long signal when the latest close exceeds the previous close.",
        required_history=2,
        required_features=("ema_fast",),
        supported_timeframes=(Timeframe.H1,),
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=DummyParams,
        operates_intrabar=False,
        allows_short=False,
    )

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        closes = context.closes()
        if closes[-1] > closes[-2]:
            return (
                self.build_signal(
                    context=context,
                    action=SignalAction.ENTER_LONG,
                    confidence=Decimal("0.6"),
                    reason="latest close exceeds the previous close",
                    features=context.features,
                ),
            )
        return ()
