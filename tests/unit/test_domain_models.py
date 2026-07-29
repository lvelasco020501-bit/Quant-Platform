"""Domain model invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from quantplatform.core.enums import (
    CircuitBreakerReason,
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
from quantplatform.core.models.health import CircuitBreakerStatus, ComponentHealth, HealthStatus
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.orders import ApprovedOrder, Order, OrderIntent
from quantplatform.core.models.portfolio import Balance, PortfolioSnapshot
from quantplatform.core.models.risk import RiskDecision
from quantplatform.core.models.signals import Signal, StrategyContext
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_approved_order,
    make_bar,
    make_bars,
    make_check,
    make_context,
    make_decision,
    make_intent,
    make_order,
    make_position,
    make_snapshot,
    make_symbol_rules,
)


def _payload(model: BaseModel, **overrides: object) -> dict[str, object]:
    """Dump a model to a re-validatable payload, dropping derived fields."""
    data = model.model_dump()
    for computed in type(model).model_computed_fields:
        data.pop(computed, None)
    return {**data, **overrides}


# --- Immutability and float rejection ------------------------------------------------------------


def test_domain_models_are_frozen() -> None:
    bar = make_bar()
    with pytest.raises(ValidationError):
        bar.close = Decimal(1)  # type: ignore[misc]


def test_models_reject_binary_floats_for_money() -> None:
    with pytest.raises(ValidationError, match="floating point"):
        make_bar(close=50_000.5)  # type: ignore[arg-type]


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Balance(asset="USDT", free=Decimal(1), locked=Decimal(0), updated_at=ANCHOR, extra=1)  # type: ignore[call-arg]


def test_models_reject_naive_datetimes() -> None:
    with pytest.raises(ValidationError):
        Balance(
            asset="USDT",
            free=Decimal(1),
            locked=Decimal(0),
            updated_at=datetime(2026, 1, 1),  # noqa: DTZ001 - deliberately naive
        )


# --- SymbolRules ---------------------------------------------------------------------------------


def test_symbol_rules_derive_precision_from_steps() -> None:
    rules = make_symbol_rules(price_tick=Decimal("0.01"), quantity_step=Decimal("0.001"))
    assert rules.price_precision == 2
    assert rules.quantity_precision == 3


def test_symbol_rules_reject_assets_that_contradict_the_symbol() -> None:
    with pytest.raises(ValidationError, match="does not match assets"):
        make_symbol_rules(symbol="BTC/USDT", base_asset="ETH")


def test_symbol_rules_validate_venue_constraints() -> None:
    rules = make_symbol_rules(
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal(10),
    )
    assert rules.is_valid_quantity(Decimal("0.002"))
    assert not rules.is_valid_quantity(Decimal("0.0025"))
    assert not rules.is_valid_quantity(Decimal("0.0001"))
    assert rules.is_valid_price(Decimal("50000.01"))
    assert not rules.is_valid_price(Decimal("50000.005"))
    assert rules.is_valid_notional(Decimal(50_000), Decimal("0.001"))
    assert not rules.is_valid_notional(Decimal(1_000), Decimal("0.001"))


def test_symbol_rules_quantize_conservatively() -> None:
    rules = make_symbol_rules(quantity_step=Decimal("0.001"))
    assert rules.quantize_quantity(Decimal("0.0019")) == Decimal("0.001")


# --- MarketBar -----------------------------------------------------------------------------------


def test_market_bar_requires_a_consistent_close_time() -> None:
    with pytest.raises(ValidationError, match="close_time must be"):
        MarketBar(
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            open_time=ANCHOR,
            close_time=ANCHOR + timedelta(hours=2),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(1),
            source="test",
            is_closed=True,
        )


def test_market_bar_requires_grid_alignment() -> None:
    with pytest.raises(ValidationError, match="not on the"):
        MarketBar(
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            open_time=ANCHOR + timedelta(minutes=7),
            close_time=ANCHOR + timedelta(minutes=67),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(1),
            source="test",
            is_closed=True,
        )


@pytest.mark.parametrize(
    ("high", "low"),
    [(Decimal(9), Decimal(1)), (Decimal(100), Decimal(60))],
)
def test_market_bar_rejects_inconsistent_ohlc(high: Decimal, low: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_bar(open_price=Decimal(10), close=Decimal(50), high=high, low=low)


def test_market_bar_closure_depends_on_the_clock() -> None:
    bar = make_bar()
    assert not bar.is_closed_at(bar.close_time - timedelta(seconds=1))
    assert bar.is_closed_at(bar.close_time)


# --- StrategyContext and Signal ------------------------------------------------------------------


def test_context_rejects_open_bars() -> None:
    open_bar = make_bar(index=0, is_closed=False)
    with pytest.raises(ValidationError, match="only closed bars"):
        StrategyContext(
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            as_of=open_bar.close_time,
            bars=(open_bar,),
            features={},
            position_state=PositionState.FLAT,
            symbol_rules=make_symbol_rules(),
        )


def test_context_rejects_out_of_order_history() -> None:
    bars = make_bars((Decimal(1), Decimal(2)))
    with pytest.raises(ValidationError, match="strictly increasing"):
        StrategyContext(
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            as_of=bars[0].close_time,
            bars=(bars[1], bars[0]),
            features={},
            position_state=PositionState.FLAT,
            symbol_rules=make_symbol_rules(),
        )


def test_context_as_of_must_match_the_last_bar() -> None:
    bars = make_bars((Decimal(1), Decimal(2)))
    with pytest.raises(ValidationError, match="as_of must equal"):
        StrategyContext(
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            as_of=bars[0].close_time,
            bars=bars,
            features={},
            position_state=PositionState.FLAT,
            symbol_rules=make_symbol_rules(),
        )


def test_context_exposes_no_account_financials() -> None:
    context = make_context()
    forbidden = {"cash", "balance", "balances", "equity", "execution_mode", "credentials"}
    assert forbidden.isdisjoint(set(StrategyContext.model_fields))
    assert context.position_state is PositionState.FLAT


def test_signal_cannot_predate_its_bar() -> None:
    context = make_context()
    with pytest.raises(ValidationError, match="must not precede"):
        Signal(
            signal_id=make_intent().signal_id,
            strategy_id="dummy_trend",
            strategy_version="1.0.0",
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            bar_close_time=context.as_of,
            generated_at=context.as_of - timedelta(seconds=1),
            action=SignalAction.ENTER_LONG,
            confidence=Decimal("0.5"),
            reason="look-ahead attempt",
        )


def test_signal_requires_an_explanation() -> None:
    context = make_context()
    with pytest.raises(ValidationError):
        Signal(
            signal_id=make_intent().signal_id,
            strategy_id="dummy_trend",
            strategy_version="1.0.0",
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            bar_close_time=context.as_of,
            generated_at=context.as_of,
            action=SignalAction.ENTER_LONG,
            confidence=Decimal("0.5"),
            reason="",
        )


# --- OrderIntent ---------------------------------------------------------------------------------


def test_intent_requires_exactly_one_sizing_expression() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        make_intent(quantity=Decimal("0.1"), notional=Decimal(5_000))
    with pytest.raises(ValidationError, match="exactly one"):
        make_intent(quantity=None, notional=None)


def test_intent_may_defer_sizing_to_the_risk_engine() -> None:
    intent = make_intent(quantity=None, notional=Decimal(5_000))
    assert intent.requested_quantity is None
    assert intent.requested_notional == Decimal(5_000)


def test_market_orders_reject_a_limit_price() -> None:
    payload = _payload(make_intent(), limit_price=Decimal(1))
    with pytest.raises(ValidationError, match="must not carry a limit_price"):
        OrderIntent.model_validate(payload)


def test_limit_orders_require_a_limit_price() -> None:
    payload = _payload(make_intent(), order_type=OrderType.LIMIT)
    with pytest.raises(ValidationError, match="requires a limit_price"):
        OrderIntent.model_validate(payload)


# --- Risk decision -------------------------------------------------------------------------------


def test_approved_decision_carries_an_executable_order() -> None:
    decision = make_decision()
    assert decision.is_executable
    assert decision.approved_order is not None
    assert decision.approved_order.quantity == decision.requested_quantity


def test_rejected_decision_cannot_carry_an_order() -> None:
    intent = make_intent()
    decision = make_decision(intent=intent, outcome=RiskOutcome.REJECTED)
    assert decision.approved_order is None
    assert not decision.is_executable
    payload = _payload(
        decision,
        approved_order=_payload(make_approved_order(intent, decision.decision_id)),
    )
    with pytest.raises(ValidationError, match="must not carry an approved order"):
        RiskDecision.model_validate(payload)


def test_approved_decision_cannot_contain_a_failed_check() -> None:
    intent = make_intent()
    decision = make_decision(intent=intent)
    payload = _payload(
        decision,
        checks=[_payload(make_check(RiskCheckCode.MAX_EXPOSURE, RiskCheckStatus.FAILED))],
    )
    with pytest.raises(ValidationError, match="must not contain failed risk checks"):
        RiskDecision.model_validate(payload)


def test_rejection_must_be_explained() -> None:
    intent = make_intent()
    decision = make_decision(intent=intent, outcome=RiskOutcome.REJECTED)
    payload = _payload(decision, rejection_reasons=[])
    with pytest.raises(ValidationError, match="at least one rejection reason"):
        RiskDecision.model_validate(payload)


def test_rejection_requires_a_failed_check() -> None:
    intent = make_intent()
    decision = make_decision(intent=intent, outcome=RiskOutcome.REJECTED)
    payload = _payload(decision, checks=[_payload(make_check())])
    with pytest.raises(ValidationError, match="at least one failed check"):
        RiskDecision.model_validate(payload)


def test_resizing_must_reduce_the_requested_quantity() -> None:
    intent = make_intent(quantity=Decimal("0.1"))
    resized = make_decision(
        intent=intent, outcome=RiskOutcome.RESIZED, approved_quantity=Decimal("0.05")
    )
    assert resized.approved_order is not None
    assert resized.approved_order.quantity == Decimal("0.05")

    with pytest.raises(ValidationError, match="must not increase"):
        make_decision(intent=intent, outcome=RiskOutcome.RESIZED, approved_quantity=Decimal("0.5"))


def test_approval_may_not_silently_change_the_quantity() -> None:
    intent = make_intent(quantity=Decimal("0.1"))
    with pytest.raises(ValidationError, match="use resized"):
        make_decision(
            intent=intent, outcome=RiskOutcome.APPROVED, approved_quantity=Decimal("0.05")
        )


def test_approved_order_must_reference_its_own_decision() -> None:
    intent = make_intent()
    decision = make_decision(intent=intent)
    foreign = make_approved_order(intent, make_intent().intent_id)
    payload = _payload(decision, approved_order=_payload(foreign))
    with pytest.raises(ValidationError, match="must reference the decision"):
        RiskDecision.model_validate(payload)


def test_each_check_is_recorded_once() -> None:
    intent = make_intent()
    decision = make_decision(intent=intent)
    payload = _payload(decision, checks=[_payload(make_check()), _payload(make_check())])
    with pytest.raises(ValidationError, match="at most once"):
        RiskDecision.model_validate(payload)


def test_approved_order_cannot_be_built_without_a_decision_id() -> None:
    assert "decision_id" in ApprovedOrder.model_fields
    assert ApprovedOrder.model_fields["decision_id"].is_required()


# --- Order and Fill ------------------------------------------------------------------------------


def test_rejected_order_cannot_carry_fills() -> None:
    with pytest.raises(ValidationError, match="cannot carry fills"):
        make_order(
            status=OrderStatus.REJECTED,
            filled_quantity=Decimal("0.1"),
            avg_fill_price=Decimal(50_000),
            reject_reason="venue rejected",
        )


def test_rejected_order_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="requires a reject_reason"):
        make_order(status=OrderStatus.REJECTED)


def test_fills_cannot_exceed_the_order_quantity() -> None:
    with pytest.raises(ValidationError, match="must not exceed quantity"):
        make_order(
            status=OrderStatus.PARTIALLY_FILLED,
            quantity=Decimal("0.1"),
            filled_quantity=Decimal("0.2"),
            avg_fill_price=Decimal(50_000),
        )


def test_filled_order_must_be_fully_filled() -> None:
    with pytest.raises(ValidationError, match="equal to quantity"):
        make_order(
            status=OrderStatus.FILLED,
            quantity=Decimal("0.1"),
            filled_quantity=Decimal("0.05"),
            avg_fill_price=Decimal(50_000),
        )


def test_partially_filled_order_tracks_the_remainder() -> None:
    order = make_order(
        status=OrderStatus.PARTIALLY_FILLED,
        quantity=Decimal("0.1"),
        filled_quantity=Decimal("0.04"),
        avg_fill_price=Decimal(50_000),
    )
    assert order.remaining_quantity == Decimal("0.06")
    assert order.is_open


def test_a_filled_quantity_requires_a_fill_price() -> None:
    with pytest.raises(ValidationError, match="requires an avg_fill_price"):
        make_order(
            status=OrderStatus.PARTIALLY_FILLED,
            quantity=Decimal("0.1"),
            filled_quantity=Decimal("0.04"),
        )


def test_order_timestamps_must_not_go_backwards() -> None:
    order = make_order()
    payload = _payload(order, updated_at=ANCHOR - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="must not precede created_at"):
        Order.model_validate(payload)


# --- Portfolio -----------------------------------------------------------------------------------


def test_balances_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        Balance(asset="USDT", free=Decimal(-1), locked=Decimal(0), updated_at=ANCHOR)


def test_positions_cannot_be_negative_because_shorting_is_prohibited() -> None:
    with pytest.raises(ValidationError):
        make_position(quantity=Decimal("-0.1"))


def test_open_position_requires_entry_data() -> None:
    with pytest.raises(ValidationError, match="requires an avg_entry_price"):
        make_position(quantity=Decimal("0.1"), avg_entry_price=None)


def test_flat_position_reports_no_exposure() -> None:
    position = make_position(quantity=Decimal(0))
    assert not position.is_open
    assert position.state is PositionState.FLAT
    assert position.unrealized_pnl(Decimal(60_000)) == Decimal(0)


def test_unrealized_pnl_marks_against_entry() -> None:
    position = make_position(quantity=Decimal("0.5"), avg_entry_price=Decimal(40_000))
    assert position.unrealized_pnl(Decimal(50_000)) == Decimal(5_000)
    assert position.market_value(Decimal(50_000)) == Decimal(25_000)


def test_equity_equals_cash_plus_marked_positions() -> None:
    position = make_position(quantity=Decimal("0.2"), avg_entry_price=Decimal(40_000))
    snapshot = make_snapshot(
        cash=Decimal(1_000),
        positions=(position,),
        mark_prices={SYMBOL: Decimal(50_000)},
    )
    assert snapshot.positions_value == Decimal(10_000)
    assert snapshot.equity == snapshot.cash + snapshot.positions_value
    assert snapshot.equity == Decimal(11_000)
    assert snapshot.unrealized_pnl == Decimal(2_000)
    assert snapshot.open_position_count == 1


def test_equity_is_derived_and_cannot_be_overridden() -> None:
    assert "equity" not in PortfolioSnapshot.model_fields
    snapshot = make_snapshot()
    assert snapshot.equity == snapshot.cash


def test_open_positions_require_a_mark_price() -> None:
    position = make_position(quantity=Decimal("0.2"))
    with pytest.raises(ValidationError, match="mark price is required"):
        make_snapshot(positions=(position,), mark_prices={})


def test_a_symbol_may_appear_only_once() -> None:
    position = make_position(quantity=Decimal("0.2"))
    with pytest.raises(ValidationError, match="at most once in positions"):
        make_snapshot(positions=(position, position))


# --- Health --------------------------------------------------------------------------------------


def _health(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "state": SystemState.HEALTHY,
        "checked_at": ANCHOR,
        "components": (),
        "circuit_breakers": (),
        "reconciliation_status": ReconciliationStatus.IN_SYNC,
        "consecutive_api_failures": 0,
    }
    return {**base, **overrides}


def test_halted_system_must_explain_itself_and_blocks_trading() -> None:
    with pytest.raises(ValidationError, match="record why it halted"):
        HealthStatus.model_validate(_health(state=SystemState.HALTED))

    halted = HealthStatus.model_validate(
        _health(state=SystemState.HALTED, halt_reason="drawdown breach")
    )
    assert not halted.allows_trading


def test_a_tripped_breaker_forbids_the_healthy_state() -> None:
    breaker = CircuitBreakerStatus(
        reason=CircuitBreakerReason.STALE_MARKET_DATA,
        tripped=True,
        detail="no bar for 3 intervals",
        tripped_at=ANCHOR,
    )
    with pytest.raises(ValidationError, match="not healthy"):
        HealthStatus.model_validate(_health(circuit_breakers=(breaker,)))

    degraded = HealthStatus.model_validate(
        _health(state=SystemState.DEGRADED, circuit_breakers=(breaker,))
    )
    assert not degraded.allows_trading
    assert degraded.tripped_breakers == (breaker,)


def test_unreconciled_system_cannot_be_healthy() -> None:
    with pytest.raises(ValidationError, match="not reconciled"):
        HealthStatus.model_validate(
            _health(reconciliation_status=ReconciliationStatus.DRIFT_DETECTED)
        )


def test_unhealthy_component_must_explain_itself() -> None:
    with pytest.raises(ValidationError, match="explain why"):
        ComponentHealth(name="venue", healthy=False, detail=None, checked_at=ANCHOR)


def test_tripped_breaker_requires_trip_metadata() -> None:
    with pytest.raises(ValidationError, match="requires tripped_at and detail"):
        CircuitBreakerStatus(
            reason=CircuitBreakerReason.DATABASE_FAILURE,
            tripped=True,
            detail=None,
            tripped_at=None,
        )


def test_only_a_healthy_system_permits_orders() -> None:
    healthy = HealthStatus.model_validate(_health())
    assert healthy.allows_trading
    for state in (
        SystemState.STARTING,
        SystemState.DEGRADED,
        SystemState.RECONCILIATION_REQUIRED,
    ):
        status = HealthStatus.model_validate(_health(state=state))
        assert not status.allows_trading


# --- Cross-model traceability --------------------------------------------------------------------


def test_decision_path_is_fully_traceable() -> None:
    intent = make_intent()
    decision = make_decision(intent=intent)
    order = make_order()

    assert decision.intent_id == intent.intent_id
    assert decision.approved_order is not None
    assert decision.approved_order.intent_id == intent.intent_id
    assert order.decision_id == decision.decision_id
    assert order.client_order_id == decision.approved_order.client_order_id


def test_execution_modes_are_carried_end_to_end() -> None:
    intent = make_intent(execution_mode=ExecutionMode.SHADOW)
    decision = make_decision(intent=intent)
    assert decision.approved_order is not None
    assert decision.approved_order.execution_mode is ExecutionMode.SHADOW


def test_order_side_has_an_opposite() -> None:
    assert OrderSide.BUY.opposite is OrderSide.SELL
    assert OrderSide.SELL.opposite is OrderSide.BUY


def test_time_in_force_values_are_stable() -> None:
    assert TimeInForce.GTC.value == "gtc"


def test_position_snapshot_uses_utc() -> None:
    snapshot = make_snapshot()
    assert snapshot.taken_at.tzinfo is UTC
    assert isinstance(snapshot.positions, tuple)
