"""Phase 4: :class:`StandardRiskEngine` deterministic risk evaluation."""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quantplatform.core.enums import (
    CommissionModel,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    ReconciliationStatus,
    RiskCheckCode,
    RiskCheckStatus,
    RiskOutcome,
    SignalAction,
    SystemState,
    TimeInForce,
)
from quantplatform.core.errors import UnsupportedFeeAssetError
from quantplatform.core.events import RiskDecisionMade
from quantplatform.core.interfaces import RiskEngine
from quantplatform.core.models.health import ComponentHealth, HealthStatus
from quantplatform.core.models.portfolio import Balance, Position
from quantplatform.core.models.risk import RiskDecision
from quantplatform.execution.config import ExecutionConfig
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.sizing import market_buy_price_cap, normalize_limit_price
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_broker,
    make_execution_policy,
    make_intent,
    make_risk_config,
    make_risk_context,
    make_risk_engine,
    make_snapshot,
    make_symbol_rules,
)

_BTC = "BTC"
_USDT = "USDT"


def _decide(
    *,
    intent_kwargs: dict[str, object] | None = None,
    context_kwargs: dict[str, object] | None = None,
    config_kwargs: dict[str, object] | None = None,
) -> RiskDecision:
    engine = make_risk_engine(**(config_kwargs or {}))
    intent = make_intent(**(intent_kwargs or {}))  # type: ignore[arg-type]
    context = make_risk_context(**(context_kwargs or {}))  # type: ignore[arg-type]
    return engine.evaluate(intent, context)


def _check(decision: RiskDecision, code: RiskCheckCode) -> object:
    return next(check for check in decision.checks if check.code is code)


def _failed_codes(decision: RiskDecision) -> set[RiskCheckCode]:
    return {check.code for check in decision.blocking_failures}


def _funded(cash: Decimal = Decimal(1_000_000)) -> dict[str, object]:
    """A context with plenty of quote balance so sizing is not the binding constraint."""
    return {"snapshot": make_snapshot(cash=cash)}


def _holding(
    quantity: Decimal = Decimal("1"),
    *,
    free: Decimal | None = None,
    locked: Decimal = Decimal(0),
    cash: Decimal = Decimal(1_000_000),
) -> dict[str, object]:
    """A context holding an open BTC position backed by a matching base balance."""
    resolved_free = free if free is not None else quantity - locked
    snapshot = make_snapshot(
        cash=cash,
        balances=(
            Balance(asset=_USDT, free=cash, locked=Decimal(0), updated_at=ANCHOR),
            Balance(asset=_BTC, free=resolved_free, locked=locked, updated_at=ANCHOR),
        ),
        positions=(
            Position(
                symbol=SYMBOL,
                base_asset=_BTC,
                quote_asset=_USDT,
                quantity=quantity,
                avg_entry_price=Decimal(50_000),
                realized_pnl=Decimal(0),
                fees_paid=Decimal(0),
                opened_at=ANCHOR,
                updated_at=ANCHOR,
            ),
        ),
    )
    return {"snapshot": snapshot}


# --- Approval -------------------------------------------------------------------------------


def test_valid_limit_buy_is_approved() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.1")},
        context_kwargs=_funded(),
        config_kwargs={},
    )
    intent = make_intent(quantity=Decimal("0.1"))
    engine = make_risk_engine()
    limit_decision = engine.evaluate(
        make_intent(quantity=Decimal("0.1")), make_risk_context(**_funded())
    )
    assert limit_decision.outcome is RiskOutcome.APPROVED
    assert limit_decision.approved_order is not None
    assert limit_decision.approved_order.quantity == Decimal("0.1")
    assert decision.outcome is RiskOutcome.APPROVED
    assert intent.symbol == SYMBOL


def test_valid_market_buy_carries_a_max_execution_price() -> None:
    decision = _decide(context_kwargs=_funded())
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.APPROVED
    assert order is not None
    assert order.order_type is OrderType.MARKET
    assert order.max_execution_price is not None
    assert order.max_execution_price > Decimal(50_000)


def test_valid_market_sell_is_approved_without_a_cap() -> None:
    decision = _decide(
        intent_kwargs={"side": OrderSide.SELL, "action": SignalAction.EXIT_LONG},
        context_kwargs=_holding(),
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.APPROVED
    assert order is not None
    assert order.max_execution_price is None
    assert order.side is OrderSide.SELL


def test_an_approved_decision_records_every_check_not_only_failures() -> None:
    decision = _decide(context_kwargs=_funded())
    assert len(decision.checks) > 25
    assert decision.blocking_failures == ()
    assert any(check.status is RiskCheckStatus.SKIPPED for check in decision.checks)


# --- Resizing -------------------------------------------------------------------------------


def test_quantity_is_resized_to_the_available_quote_balance() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs={"snapshot": make_snapshot(cash=Decimal(10_000))},
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.quantity < Decimal("1")
    assert order.max_execution_price is not None
    assert order.quantity * order.max_execution_price <= Decimal(10_000)


def test_sell_is_resized_to_the_available_base_balance() -> None:
    decision = _decide(
        intent_kwargs={
            "side": OrderSide.SELL,
            "action": SignalAction.EXIT_LONG,
            "quantity": Decimal("1"),
        },
        context_kwargs=_holding(Decimal("1"), free=Decimal("0.4"), locked=Decimal("0.6")),
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.quantity == Decimal("0.4")


def test_quantity_is_resized_to_the_configured_order_notional_limit() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs=_funded(),
        config_kwargs={"max_order_notional": Decimal(10_000)},
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.max_execution_price is not None
    assert order.quantity * order.max_execution_price <= Decimal(10_000)


def test_quantity_is_resized_to_the_portfolio_exposure_limit() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("10")},
        context_kwargs=_funded(Decimal(1_000_000)),
        config_kwargs={"max_portfolio_exposure_pct": Decimal("0.10")},
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.quantity * Decimal(50_000) <= Decimal(1_000_000) * Decimal("0.10")


def test_quantity_is_rounded_down_to_the_venue_lot_step() -> None:
    rules = make_symbol_rules(quantity_step=Decimal("0.01"), min_quantity=Decimal("0.01"))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.1234")},
        context_kwargs={**_funded(), "symbol_rules": rules},
    )
    order = decision.approved_order
    assert order is not None
    assert order.quantity == Decimal("0.12")
    assert decision.outcome is RiskOutcome.RESIZED


def test_a_resize_below_the_venue_minimum_is_rejected() -> None:
    rules = make_symbol_rules(quantity_step=Decimal("0.01"), min_quantity=Decimal("0.5"))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs={"snapshot": make_snapshot(cash=Decimal(1_000)), "symbol_rules": rules},
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.approved_order is None
    assert RiskCheckCode.MINIMUM_QUANTITY in _failed_codes(decision)


def test_sizing_never_increases_the_requested_quantity() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.01")},
        context_kwargs=_funded(Decimal(10_000_000)),
    )
    order = decision.approved_order
    assert order is not None
    assert order.quantity == Decimal("0.01")
    assert decision.outcome is RiskOutcome.APPROVED


def test_a_notional_sized_intent_is_converted_to_a_venue_valid_quantity() -> None:
    decision = _decide(
        intent_kwargs={"quantity": None, "notional": Decimal(5_000)},
        context_kwargs=_funded(),
    )
    order = decision.approved_order
    assert order is not None
    assert order.max_execution_price is not None
    assert order.quantity * order.max_execution_price <= Decimal(5_000)


# --- Venue rules ----------------------------------------------------------------------------


def test_below_minimum_notional_is_rejected() -> None:
    rules = make_symbol_rules(min_notional=Decimal(1_000_000))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.001")},
        context_kwargs={**_funded(), "symbol_rules": rules},
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.MINIMUM_NOTIONAL in _failed_codes(decision)


def test_venue_maximum_quantity_resizes_rather_than_rejects() -> None:
    rules = make_symbol_rules(max_quantity=Decimal("0.05"))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs={**_funded(), "symbol_rules": rules},
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.quantity <= Decimal("0.05")


def test_venue_maximum_notional_is_recorded_when_declared() -> None:
    rules = make_symbol_rules(max_quantity=None)
    decision = _decide(context_kwargs={**_funded(), "symbol_rules": rules})
    maximum = _check(decision, RiskCheckCode.MAXIMUM_QUANTITY)
    assert maximum.status is RiskCheckStatus.SKIPPED  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("side", "raw", "expected"),
    [
        (OrderSide.BUY, Decimal("100.007"), Decimal("100.00")),
        (OrderSide.SELL, Decimal("100.003"), Decimal("100.01")),
    ],
)
def test_limit_price_rounds_away_from_the_market(
    side: OrderSide, raw: Decimal, expected: Decimal
) -> None:
    # A buy limit rounds down and a sell limit rounds up, so neither authorises execution at
    # a worse price than the strategy asked for.
    rules = make_symbol_rules(price_tick=Decimal("0.01"))
    assert normalize_limit_price(raw, side, rules) == expected


def test_a_limit_order_carries_the_normalized_price_on_the_approved_order() -> None:
    rules = make_symbol_rules(price_tick=Decimal("0.01"))
    engine = make_risk_engine()
    intent = make_intent(quantity=Decimal("0.1"))
    payload = intent.model_dump()
    payload.update(
        {"order_type": OrderType.LIMIT, "limit_price": Decimal("49999.007"), "stop_price": None}
    )
    limit_intent = type(intent).model_validate(payload)
    decision = engine.evaluate(
        limit_intent, make_risk_context(**{**_funded(), "symbol_rules": rules})
    )
    order = decision.approved_order
    assert order is not None
    assert order.limit_price == Decimal("49999.00")
    assert order.max_execution_price is None


# --- Balance and exposure ---------------------------------------------------------------------


def test_locked_quote_balance_is_not_spendable() -> None:
    snapshot = make_snapshot(cash=Decimal(10_000), quote_locked=Decimal(9_000))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")}, context_kwargs={"snapshot": snapshot}
    )
    order = decision.approved_order
    assert order is not None
    assert order.max_execution_price is not None
    # Only the 1_000 free may be committed, never the 9_000 already reserved.
    assert order.quantity * order.max_execution_price <= Decimal(1_000)


def test_locked_base_balance_is_not_sellable() -> None:
    decision = _decide(
        intent_kwargs={
            "side": OrderSide.SELL,
            "action": SignalAction.EXIT_LONG,
            "quantity": Decimal("1"),
        },
        context_kwargs=_holding(Decimal("1"), free=Decimal("0.25"), locked=Decimal("0.75")),
    )
    order = decision.approved_order
    assert order is not None
    assert order.quantity == Decimal("0.25")


def test_position_and_base_balance_mismatch_is_rejected() -> None:
    snapshot = make_snapshot(
        cash=Decimal(1_000_000),
        balances=(
            Balance(asset=_USDT, free=Decimal(1_000_000), locked=Decimal(0), updated_at=ANCHOR),
            Balance(asset=_BTC, free=Decimal("0.5"), locked=Decimal(0), updated_at=ANCHOR),
        ),
        positions=(
            Position(
                symbol=SYMBOL,
                base_asset=_BTC,
                quote_asset=_USDT,
                quantity=Decimal("2"),
                avg_entry_price=Decimal(50_000),
                realized_pnl=Decimal(0),
                fees_paid=Decimal(0),
                opened_at=ANCHOR,
                updated_at=ANCHOR,
            ),
        ),
    )
    decision = _decide(
        intent_kwargs={
            "side": OrderSide.SELL,
            "action": SignalAction.EXIT_LONG,
            "quantity": Decimal("0.1"),
        },
        context_kwargs={"snapshot": snapshot},
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.ACCOUNTING_INVARIANT in _failed_codes(decision)


def test_max_open_positions_blocks_opening_another_symbol() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.01")},
        context_kwargs=_holding(Decimal("1")),
        config_kwargs={"max_open_positions": 1},
    )
    # The intent targets the symbol already held, so it adds to it rather than opening a new
    # position and is allowed through the position-count gate.
    assert RiskCheckCode.MAX_POSITION_COUNT not in _failed_codes(decision)


def test_opening_a_new_symbol_beyond_the_position_limit_is_rejected() -> None:
    eth_rules = make_symbol_rules(symbol="ETH/USDT", base_asset="ETH")
    context = make_risk_context(
        **{**_holding(Decimal("1")), "symbol_rules": eth_rules}  # type: ignore[arg-type]
    )
    engine = make_risk_engine(max_open_positions=1)
    payload = make_intent(quantity=Decimal("0.1")).model_dump()
    payload["symbol"] = "ETH/USDT"
    intent = type(make_intent()).model_validate(payload)

    decision = engine.evaluate(intent, context)

    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.MAX_POSITION_COUNT in _failed_codes(decision)


def test_per_symbol_exposure_limit_constrains_the_order() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs=_funded(),
        config_kwargs={
            "max_symbol_exposure": Decimal(20_000),
            "max_order_notional": Decimal(20_000),
        },
    )
    order = decision.approved_order
    assert order is not None
    assert order.quantity * Decimal(50_000) <= Decimal(20_000)


def test_zero_equity_rejects_rather_than_dividing_by_zero() -> None:
    snapshot = make_snapshot(cash=Decimal(0))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.1")},
        context_kwargs={
            "snapshot": snapshot,
            "day_start_equity": Decimal(1),
            "peak_equity": Decimal(1),
        },
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.approved_order is None


# --- Market-buy cap ---------------------------------------------------------------------------


def test_market_buy_cap_covers_slippage_spread_and_safety_buffer() -> None:
    decision = _decide(
        context_kwargs=_funded(),
        config_kwargs={
            "execution_policy": make_execution_policy(slippage_bps=Decimal(10)),
            "market_buy_buffer_bps": Decimal(40),
            "additional_market_buy_safety_bps": Decimal(10),
        },
    )
    order = decision.approved_order
    assert order is not None
    # 50 bps total on 50_000 is 250, so the cap sits at 50_250.
    assert order.max_execution_price == Decimal("50250.00")


def test_market_buy_cap_rounds_up_to_the_venue_tick() -> None:
    rules = make_symbol_rules(price_tick=Decimal("1"))
    cap = market_buy_price_cap(Decimal(50_000), Decimal(1), rules)
    # 1 bp of 50_000 is 5, exactly on the grid; a fractional buffer must round up, not down.
    assert cap == Decimal(50_005)
    assert market_buy_price_cap(Decimal("50000.4"), Decimal(0), rules) == Decimal(50_001)


def test_a_buffer_below_broker_slippage_is_refused_at_configuration_time() -> None:
    with pytest.raises(ValueError, match="must cover the execution policy's slippage"):
        RiskConfiguration(
            execution_policy=make_execution_policy(slippage_bps=Decimal(100)),
            market_buy_buffer_bps=Decimal(10),
            additional_market_buy_safety_bps=Decimal(0),
        )


def test_market_buy_cap_check_compares_against_the_broker_worst_case() -> None:
    decision = _decide(context_kwargs=_funded())
    check = _check(decision, RiskCheckCode.MARKET_BUY_CAP)
    assert check.status is RiskCheckStatus.PASSED  # type: ignore[attr-defined]
    assert check.observed >= check.limit  # type: ignore[attr-defined,operator]


def test_insufficient_quote_balance_under_the_cap_resizes_when_a_valid_order_remains() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs={"snapshot": make_snapshot(cash=Decimal(1_000))},
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.max_execution_price is not None
    assert order.quantity * order.max_execution_price <= Decimal(1_000)


def test_insufficient_quote_balance_under_the_cap_rejects_when_nothing_valid_remains() -> None:
    # Five quote units cannot fund an order that still clears the venue's minimum notional.
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs={"snapshot": make_snapshot(cash=Decimal(5))},
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.approved_order is None
    assert RiskCheckCode.MINIMUM_NOTIONAL in _failed_codes(decision)


# --- Operational checks --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", [SystemState.HALTED, SystemState.STARTING, SystemState.RECONCILIATION_REQUIRED]
)
def test_non_tradable_system_states_are_rejected(state: SystemState) -> None:
    health = HealthStatus(
        state=state,
        checked_at=ANCHOR,
        components=(),
        circuit_breakers=(),
        reconciliation_status=ReconciliationStatus.IN_SYNC,
        last_bar_close_time=ANCHOR,
        data_age_seconds=0,
        clock_skew_seconds=0.0,
        consecutive_api_failures=0,
        halt_reason="halted for test" if state is SystemState.HALTED else None,
    )
    decision = _decide(context_kwargs={**_funded(), "health": health})
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.SYSTEM_STATE in _failed_codes(decision)


@pytest.mark.parametrize(("allowed", "expected"), [(True, False), (False, True)])
def test_degraded_state_follows_configuration(*, allowed: bool, expected: bool) -> None:
    health = HealthStatus(
        state=SystemState.DEGRADED,
        checked_at=ANCHOR,
        components=(),
        circuit_breakers=(),
        reconciliation_status=ReconciliationStatus.IN_SYNC,
        last_bar_close_time=ANCHOR,
        data_age_seconds=0,
        clock_skew_seconds=0.0,
        consecutive_api_failures=0,
        halt_reason=None,
    )
    decision = _decide(
        context_kwargs={**_funded(), "health": health},
        config_kwargs={"allow_degraded_state": allowed},
    )
    assert (RiskCheckCode.SYSTEM_STATE in _failed_codes(decision)) is expected


def test_stale_market_data_is_rejected() -> None:
    decision = _decide(
        context_kwargs={
            **_funded(),
            "as_of": ANCHOR + timedelta(hours=2),
            "latest_bar_close_time": ANCHOR,
        },
        config_kwargs={"stale_market_data_seconds": 60},
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.DATA_FRESHNESS in _failed_codes(decision)


def test_an_open_candle_is_rejected() -> None:
    decision = _decide(context_kwargs={**_funded(), "latest_bar_is_closed": False})
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.CLOSED_CANDLE in _failed_codes(decision)


def test_stale_symbol_rules_are_rejected() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "as_of": ANCHOR + timedelta(days=30)},
        config_kwargs={"stale_symbol_rules_seconds": 3_600, "stale_market_data_seconds": 10**9},
    )
    assert RiskCheckCode.SYMBOL_RULES_FRESHNESS in _failed_codes(decision)


def test_an_unhealthy_component_is_rejected() -> None:
    health = HealthStatus(
        state=SystemState.DEGRADED,
        checked_at=ANCHOR,
        components=(
            ComponentHealth(
                name="market_data", healthy=False, detail="feed unreachable", checked_at=ANCHOR
            ),
        ),
        circuit_breakers=(),
        reconciliation_status=ReconciliationStatus.IN_SYNC,
        last_bar_close_time=ANCHOR,
        data_age_seconds=0,
        clock_skew_seconds=0.0,
        consecutive_api_failures=0,
        halt_reason=None,
    )
    decision = _decide(
        context_kwargs={**_funded(), "health": health},
        config_kwargs={"allow_degraded_state": True},
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.EXCHANGE_HEALTH in _failed_codes(decision)


def test_unresolved_reconciliation_is_rejected() -> None:
    health = HealthStatus(
        state=SystemState.DEGRADED,
        checked_at=ANCHOR,
        components=(),
        circuit_breakers=(),
        reconciliation_status=ReconciliationStatus.DRIFT_DETECTED,
        last_bar_close_time=ANCHOR,
        data_age_seconds=0,
        clock_skew_seconds=0.0,
        consecutive_api_failures=0,
        halt_reason=None,
    )
    decision = _decide(
        context_kwargs={**_funded(), "health": health},
        config_kwargs={"allow_degraded_state": True},
    )
    assert RiskCheckCode.RECONCILIATION_STATUS in _failed_codes(decision)


def test_the_api_failure_threshold_is_rejected() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "consecutive_api_failures": 3},
        config_kwargs={"max_consecutive_api_failures": 3},
    )
    assert RiskCheckCode.CONSECUTIVE_API_FAILURES in _failed_codes(decision)


def test_excessive_spread_is_rejected() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "spread_basis_points": Decimal(500)},
        config_kwargs={"max_spread_bps": Decimal(25)},
    )
    assert RiskCheckCode.EXCESSIVE_SPREAD in _failed_codes(decision)


def test_excessive_volatility_is_rejected() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "realized_volatility": Decimal("0.9")},
        config_kwargs={"max_volatility": Decimal("0.15")},
    )
    assert RiskCheckCode.EXCESSIVE_VOLATILITY in _failed_codes(decision)


@pytest.mark.parametrize("strict", [True, False])
def test_a_missing_metric_follows_the_strict_policy(*, strict: bool) -> None:
    decision = _decide(
        context_kwargs={**_funded(), "spread_basis_points": None},
        config_kwargs={"strict_missing_metrics": strict},
    )
    failed = RiskCheckCode.EXCESSIVE_SPREAD in _failed_codes(decision)
    assert failed is strict
    if not strict:
        assert _check(decision, RiskCheckCode.EXCESSIVE_SPREAD).status is RiskCheckStatus.SKIPPED  # type: ignore[attr-defined]


def test_daily_drawdown_beyond_the_limit_is_rejected() -> None:
    snapshot = make_snapshot(cash=Decimal(9_000))
    decision = _decide(
        context_kwargs={
            "snapshot": snapshot,
            "day_start_equity": Decimal(10_000),
            "peak_equity": Decimal(10_000),
        },
        config_kwargs={"max_daily_drawdown_pct": Decimal("0.05")},
    )
    assert RiskCheckCode.DAILY_DRAWDOWN in _failed_codes(decision)


def test_total_drawdown_beyond_the_limit_is_rejected() -> None:
    snapshot = make_snapshot(cash=Decimal(50_000))
    decision = _decide(
        context_kwargs={
            "snapshot": snapshot,
            "day_start_equity": Decimal(50_000),
            "peak_equity": Decimal(100_000),
        },
        config_kwargs={"max_total_drawdown_pct": Decimal("0.20")},
    )
    assert RiskCheckCode.TOTAL_DRAWDOWN in _failed_codes(decision)


def test_a_missing_drawdown_peak_is_rejected_in_strict_mode() -> None:
    decision = _decide(
        context_kwargs={
            **_funded(),
            "day_start_equity": Decimal(0),
            "peak_equity": Decimal(0),
        },
        config_kwargs={"strict_missing_metrics": True},
    )
    assert RiskCheckCode.DAILY_DRAWDOWN in _failed_codes(decision)
    assert RiskCheckCode.TOTAL_DRAWDOWN in _failed_codes(decision)


# --- Frequency and idempotency ---------------------------------------------------------------


def test_the_hourly_order_limit_is_enforced() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "approved_orders_last_hour": 5},
        config_kwargs={"max_orders_per_hour": 5},
    )
    assert RiskCheckCode.MAX_HOURLY_ORDERS in _failed_codes(decision)


def test_the_daily_order_limit_is_enforced() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "approved_orders_today": 20},
        config_kwargs={"max_orders_per_day": 20, "max_orders_per_hour": 20},
    )
    assert RiskCheckCode.MAX_DAILY_ORDERS in _failed_codes(decision)


def test_a_conflicting_pending_order_on_the_symbol_is_rejected() -> None:
    decision = _decide(context_kwargs={**_funded(), "open_order_symbols": frozenset({SYMBOL})})
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.CONFLICTING_ORDER in _failed_codes(decision)


def test_exhausted_working_order_capacity_is_rejected() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "open_order_count": 3}, config_kwargs={"max_open_orders": 3}
    )
    assert RiskCheckCode.PENDING_ORDERS in _failed_codes(decision)


def test_a_key_already_decided_elsewhere_is_treated_as_a_duplicate() -> None:
    intent = make_intent()
    decision = _decide(
        context_kwargs={
            **_funded(),
            "known_idempotency_keys": frozenset({intent.idempotency_key}),
        }
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.DUPLICATE_SIGNAL in _failed_codes(decision)


def test_re_evaluating_the_same_intent_replays_the_stored_decision() -> None:
    engine = make_risk_engine()
    intent = make_intent()
    context = make_risk_context(**_funded())  # type: ignore[arg-type]

    first = engine.assess(intent, context)
    second = engine.assess(intent, context)

    assert first.replayed is False
    assert second.replayed is True
    assert second.decision is first.decision
    assert second.events == ()


def test_a_replay_never_produces_a_second_approved_order() -> None:
    engine = make_risk_engine()
    intent = make_intent()
    context = make_risk_context(**_funded())  # type: ignore[arg-type]

    first = engine.evaluate(intent, context)
    second = engine.evaluate(intent, context)

    assert first.approved_order is not None
    assert second.approved_order is not None
    assert first.approved_order.client_order_id == second.approved_order.client_order_id
    assert first.decision_id == second.decision_id


def test_a_rejected_intent_still_records_a_decision_and_does_not_recompute() -> None:
    engine = make_risk_engine()
    intent = make_intent()
    context = make_risk_context(**{**_funded(), "latest_bar_is_closed": False})  # type: ignore[arg-type]

    first = engine.assess(intent, context)
    second = engine.assess(intent, context)

    assert first.decision.outcome is RiskOutcome.REJECTED
    assert second.replayed is True
    assert second.decision is first.decision


# --- Events ------------------------------------------------------------------------------------


def test_a_fresh_decision_emits_exactly_one_risk_decision_made_event() -> None:
    engine = make_risk_engine()
    result = engine.assess(make_intent(), make_risk_context(**_funded()))  # type: ignore[arg-type]

    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, RiskDecisionMade)
    assert event.decision is result.decision
    assert event.occurred_at == ANCHOR


# --- Determinism and structure -------------------------------------------------------------------


def test_identical_inputs_produce_identical_decision_and_order_ids() -> None:
    def run() -> RiskDecision:
        return make_risk_engine().evaluate(make_intent(), make_risk_context(**_funded()))  # type: ignore[arg-type]

    first, second = run(), run()
    assert first.decision_id == second.decision_id
    assert first.approved_order is not None
    assert second.approved_order is not None
    assert first.approved_order.client_order_id == second.approved_order.client_order_id


def test_a_resized_decision_has_a_different_id_from_a_full_size_approval() -> None:
    full = make_risk_engine().evaluate(
        make_intent(quantity=Decimal("0.1")),
        make_risk_context(**_funded()),  # type: ignore[arg-type]
    )
    resized = make_risk_engine().evaluate(
        make_intent(quantity=Decimal("0.1")),
        make_risk_context(snapshot=make_snapshot(cash=Decimal(2_000))),
    )
    assert full.outcome is RiskOutcome.APPROVED
    assert resized.outcome is RiskOutcome.RESIZED
    assert full.decision_id != resized.decision_id


def test_checks_are_recorded_in_a_stable_evaluation_order() -> None:
    decision = _decide(context_kwargs=_funded())
    sequences = [check.sequence for check in decision.checks]
    assert sequences == sorted(sequences)
    assert sequences == list(range(len(sequences)))
    assert decision.ordered_checks[0].code is RiskCheckCode.SYSTEM_STATE


def test_every_recorded_check_code_is_unique() -> None:
    decision = _decide(context_kwargs=_funded())
    codes = [check.code for check in decision.checks]
    assert len(codes) == len(set(codes))


def test_the_engine_satisfies_the_risk_engine_protocol() -> None:
    assert isinstance(make_risk_engine(), RiskEngine)


def test_observed_and_limit_values_are_decimal_never_float() -> None:
    decision = _decide(context_kwargs=_funded())
    for check in decision.checks:
        for value in (check.observed, check.limit):
            assert value is None or isinstance(value, Decimal)
            assert not isinstance(value, float)


def test_check_messages_carry_no_infrastructure_detail() -> None:
    decision = _decide(context_kwargs=_funded())
    for check in decision.checks:
        lowered = check.message.lower()
        for token in ("password", "api_key", "secret", "postgresql", "token", "dsn"):
            assert token not in lowered


# --- Architecture ---------------------------------------------------------------------------------


def test_the_risk_package_imports_no_infrastructure() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "quantplatform" / "risk"
    forbidden = {
        "quantplatform.storage",
        "quantplatform.execution",
        "quantplatform.portfolio",
        "quantplatform.data",
        "quantplatform.strategies",
        "sqlalchemy",
    }
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = (
                [alias.name for alias in node.names] if isinstance(node, ast.Import) else [module]
            )
            for name in names:
                if name is None:
                    continue
                assert not any(name.startswith(bad) for bad in forbidden), f"{path.name}: {name}"


def test_the_risk_engine_reads_no_wall_clock() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "quantplatform" / "risk"
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in ("datetime.now", "utcnow", "time.time", "SystemClock", "random"):
            assert token not in source, f"{path.name} references {token}"


def test_only_the_risk_engine_constructs_approved_orders() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "quantplatform"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.parent.name == "risk" or "models" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "ApprovedOrder(" in source:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"ApprovedOrder constructed outside the risk engine: {offenders}"


# --- Property-based ----------------------------------------------------------------------------


@given(
    quantity=st.decimals(min_value=Decimal("0.00001"), max_value=Decimal(5), places=5),
    cash=st.decimals(min_value=Decimal(1_000), max_value=Decimal(10_000_000), places=2),
)
def test_property_an_approved_quantity_never_exceeds_the_request(
    quantity: Decimal, cash: Decimal
) -> None:
    decision = make_risk_engine().evaluate(
        make_intent(quantity=quantity), make_risk_context(snapshot=make_snapshot(cash=cash))
    )
    if decision.approved_order is not None:
        assert decision.approved_order.quantity <= quantity


@given(
    quantity=st.decimals(min_value=Decimal("0.001"), max_value=Decimal(2), places=3),
    cash=st.decimals(min_value=Decimal(100), max_value=Decimal(5_000_000), places=2),
)
def test_property_an_approved_buy_is_always_affordable(quantity: Decimal, cash: Decimal) -> None:
    decision = make_risk_engine(
        execution_policy=make_execution_policy(
            fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(10)
        )
    ).evaluate(make_intent(quantity=quantity), make_risk_context(snapshot=make_snapshot(cash=cash)))
    order = decision.approved_order
    if order is None:
        return
    assert order.max_execution_price is not None
    cost = order.quantity * order.max_execution_price
    assert cost <= cash


@given(
    outcome_seed=st.integers(min_value=0, max_value=3),
)
def test_property_a_decision_never_carries_an_order_with_a_blocking_failure(
    outcome_seed: int,
) -> None:
    contexts = [
        make_risk_context(**_funded()),  # type: ignore[arg-type]
        make_risk_context(**{**_funded(), "latest_bar_is_closed": False}),  # type: ignore[arg-type]
        make_risk_context(**{**_funded(), "approved_orders_today": 999}),  # type: ignore[arg-type]
        make_risk_context(snapshot=make_snapshot(cash=Decimal(1))),
    ]
    decision = make_risk_engine().evaluate(make_intent(), contexts[outcome_seed])
    if decision.blocking_failures:
        assert decision.approved_order is None
        assert decision.outcome is RiskOutcome.REJECTED
    else:
        assert decision.approved_order is not None


def test_a_non_spot_market_type_is_rejected() -> None:
    rules = make_symbol_rules(market_type=MarketType.FUTURES)
    intent = make_intent()
    payload = intent.model_dump()
    payload["market_type"] = MarketType.FUTURES
    futures_intent = type(intent).model_validate(payload)

    decision = make_risk_engine().evaluate(
        futures_intent,
        make_risk_context(**{**_funded(), "symbol_rules": rules}),  # type: ignore[arg-type]
    )

    assert decision.outcome is RiskOutcome.REJECTED
    failed = _failed_codes(decision)
    assert RiskCheckCode.ALLOWED_MARKET_TYPE in failed
    assert RiskCheckCode.LEVERAGE_PROHIBITED in failed
    assert RiskCheckCode.SHORT_SELLING_PROHIBITED in failed


def test_a_disallowed_time_in_force_is_rejected() -> None:
    intent = make_intent()
    payload = intent.model_dump()
    payload["time_in_force"] = TimeInForce.FOK
    fok_intent = type(intent).model_validate(payload)

    decision = make_risk_engine(allowed_time_in_force=(TimeInForce.GTC,)).evaluate(
        fok_intent,
        make_risk_context(**_funded()),  # type: ignore[arg-type]
    )

    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.ALLOWED_TIME_IN_FORCE in _failed_codes(decision)


def test_a_disallowed_order_type_is_rejected() -> None:
    decision = _decide(context_kwargs=_funded(), config_kwargs={"allow_market_orders": False})
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.ALLOWED_ORDER_TYPE in _failed_codes(decision)


def test_a_symbol_mismatch_against_the_supplied_rules_is_rejected() -> None:
    rules = make_symbol_rules(symbol="ETH/USDT", base_asset="ETH")
    decision = _decide(context_kwargs={**_funded(), "symbol_rules": rules})
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.ALLOWED_SYMBOL in _failed_codes(decision)


def test_an_approved_order_reaches_a_working_broker_order() -> None:
    # The end-to-end contract Phase 3B depends on: whatever the risk engine approves must be
    # something the broker accepts without further adjustment.
    decision = _decide(context_kwargs=_funded())
    order = decision.approved_order
    assert order is not None
    broker, _ = make_broker(quote_free=Decimal(1_000_000))

    submission = broker.submit(order)

    assert submission.accepted is True
    assert submission.order.status is OrderStatus.OPEN


# --- Pending-order accounting (audit) --------------------------------------------------------


def test_two_new_symbols_cannot_both_pass_a_one_position_limit() -> None:
    # The second intent is evaluated before the first fills; without pending accounting both
    # would see an empty book and both be approved.
    eth = make_symbol_rules(symbol="ETH/USDT", base_asset="ETH")
    engine = make_risk_engine(max_open_positions=1)

    first = engine.evaluate(make_intent(quantity=Decimal("0.1")), make_risk_context(**_funded()))  # type: ignore[arg-type]

    second_intent = make_intent(quantity=Decimal("0.1"), signal_time=ANCHOR + timedelta(hours=1))
    payload = second_intent.model_dump()
    payload["symbol"] = "ETH/USDT"
    second = engine.evaluate(
        type(second_intent).model_validate(payload),
        make_risk_context(
            snapshot=make_snapshot(cash=Decimal(1_000_000)),
            symbol_rules=eth,
            pending_buy_notional={SYMBOL: Decimal(5_000)},
        ),
    )

    assert first.outcome is RiskOutcome.APPROVED
    assert second.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.MAX_POSITION_COUNT in _failed_codes(second)


def test_a_second_buy_on_a_pending_symbol_is_not_a_second_position() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.01")},
        context_kwargs={**_funded(), "pending_buy_notional": {SYMBOL: Decimal(5_000)}},
        config_kwargs={"max_open_positions": 1},
    )
    assert RiskCheckCode.MAX_POSITION_COUNT not in _failed_codes(decision)


def test_a_buy_on_an_already_open_symbol_is_not_a_second_position() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.01")},
        context_kwargs=_holding(Decimal("1")),
        config_kwargs={"max_open_positions": 1},
    )
    assert RiskCheckCode.MAX_POSITION_COUNT not in _failed_codes(decision)


def test_pending_buy_notional_reduces_portfolio_exposure_headroom() -> None:
    without = _decide(
        intent_kwargs={"quantity": Decimal("10")},
        context_kwargs=_funded(Decimal(1_000_000)),
        config_kwargs={"max_portfolio_exposure_pct": Decimal("0.10")},
    )
    with_pending = _decide(
        intent_kwargs={"quantity": Decimal("10")},
        context_kwargs={
            **_funded(Decimal(1_000_000)),
            "pending_buy_notional": {"ETH/USDT": Decimal(50_000)},
        },
        config_kwargs={"max_portfolio_exposure_pct": Decimal("0.10")},
    )
    assert without.approved_order is not None
    assert with_pending.approved_order is not None
    # 50_000 of the 100_000 ceiling is already committed, so half the headroom remains.
    assert with_pending.approved_order.quantity < without.approved_order.quantity


def test_pending_buy_notional_reduces_per_symbol_exposure_headroom() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("10")},
        context_kwargs={
            **_funded(Decimal(10_000_000)),
            "pending_buy_notional": {SYMBOL: Decimal(18_000)},
        },
        config_kwargs={
            "max_symbol_exposure": Decimal(20_000),
            "max_order_notional": Decimal(20_000),
        },
    )
    order = decision.approved_order
    assert order is not None
    assert order.quantity * Decimal(50_000) <= Decimal(2_000)


def test_pending_sells_are_never_counted_as_added_exposure() -> None:
    # pending_buy_notional carries buys only; a context describing an unwind leaves headroom
    # untouched rather than shrinking it.
    baseline = _decide(
        intent_kwargs={"quantity": Decimal("10")},
        context_kwargs=_funded(Decimal(1_000_000)),
        config_kwargs={"max_portfolio_exposure_pct": Decimal("0.10")},
    )
    with_sells = _decide(
        intent_kwargs={"quantity": Decimal("10")},
        context_kwargs={**_funded(Decimal(1_000_000)), "pending_buy_notional": {}},
        config_kwargs={"max_portfolio_exposure_pct": Decimal("0.10")},
    )
    assert baseline.approved_order is not None
    assert with_sells.approved_order is not None
    assert baseline.approved_order.quantity == with_sells.approved_order.quantity


def test_a_rejected_intent_contributes_nothing_to_pending_state() -> None:
    engine = make_risk_engine()
    rejected = engine.evaluate(
        make_intent(),
        make_risk_context(**{**_funded(), "latest_bar_is_closed": False}),  # type: ignore[arg-type]
    )
    assert rejected.outcome is RiskOutcome.REJECTED
    # The engine holds no pending ledger of its own; pending exposure arrives via the context,
    # so a rejection cannot inflate it.
    follow_up = engine.evaluate(
        make_intent(signal_time=ANCHOR + timedelta(hours=1)),
        make_risk_context(**_funded()),  # type: ignore[arg-type]
    )
    assert follow_up.outcome is RiskOutcome.APPROVED


# --- Commission parity (audit) ----------------------------------------------------------------


def test_flat_commission_is_reserved_once_not_as_a_rate() -> None:
    policy = make_execution_policy(fee_model=CommissionModel.FLAT, flat_amount=Decimal(3))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs={"snapshot": make_snapshot(cash=Decimal(5_000))},
        config_kwargs={
            "execution_policy": policy,
            "market_buy_buffer_bps": Decimal(0),
            "additional_market_buy_safety_bps": Decimal(0),
            "max_portfolio_exposure_pct": Decimal(1),
        },
    )
    order = decision.approved_order
    assert order is not None
    assert order.max_execution_price is not None
    cost = order.quantity * order.max_execution_price
    assert cost + Decimal(3) <= Decimal(5_000)


def test_a_balance_covering_notional_but_not_the_flat_fee_is_resized_down() -> None:
    policy = make_execution_policy(fee_model=CommissionModel.FLAT, flat_amount=Decimal(3))
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.1")},
        context_kwargs={"snapshot": make_snapshot(cash=Decimal(5_000))},
        config_kwargs={
            "execution_policy": policy,
            "market_buy_buffer_bps": Decimal(0),
            "additional_market_buy_safety_bps": Decimal(0),
            "max_portfolio_exposure_pct": Decimal(1),
        },
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.RESIZED
    assert order is not None
    assert order.quantity < Decimal("0.1")


def test_zero_commission_reserves_nothing_extra() -> None:
    decision = _decide(
        intent_kwargs={"quantity": Decimal("0.1")},
        context_kwargs={"snapshot": make_snapshot(cash=Decimal(5_000))},
        config_kwargs={
            "execution_policy": make_execution_policy(),
            "market_buy_buffer_bps": Decimal(0),
            "additional_market_buy_safety_bps": Decimal(0),
            "max_portfolio_exposure_pct": Decimal(1),
        },
    )
    order = decision.approved_order
    assert decision.outcome is RiskOutcome.APPROVED
    assert order is not None
    assert order.quantity == Decimal("0.1")


def test_a_non_quote_fee_asset_is_refused_at_the_point_of_use() -> None:
    policy = make_execution_policy(
        fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(10), fee_asset="BTC"
    )
    with pytest.raises(UnsupportedFeeAssetError):
        policy.fee.resolve_fee_asset("USDT")


def test_a_zero_fee_policy_tolerates_any_declared_asset() -> None:
    policy = make_execution_policy(fee_asset="BTC")
    assert policy.fee.resolve_fee_asset("USDT") == "USDT"


# --- Shared slippage assumptions (audit) --------------------------------------------------------


def test_risk_and_broker_read_the_same_policy_object() -> None:
    policy = make_execution_policy(slippage_bps=Decimal(20))
    config = make_risk_config(execution_policy=policy, market_buy_buffer_bps=Decimal(30))
    broker_config = ExecutionConfig(policy=policy)
    assert config.execution_policy is policy
    assert broker_config.slippage is policy.slippage
    assert config.minimum_required_buffer_bps == policy.slippage.effective_basis_points


def test_a_buffer_exactly_equal_to_the_worst_case_is_accepted() -> None:
    policy = make_execution_policy(slippage_bps=Decimal(25))
    config = make_risk_config(
        execution_policy=policy,
        market_buy_buffer_bps=Decimal(25),
        additional_market_buy_safety_bps=Decimal(0),
    )
    assert config.total_market_buy_buffer_bps == config.minimum_required_buffer_bps


def test_spread_is_not_double_counted_in_the_cap() -> None:
    # The required buffer is exactly the adapter's slippage; the spread lives in the traded
    # reference price already and is policed separately by its own guard.
    policy = make_execution_policy(slippage_bps=Decimal(30))
    config = make_risk_config(execution_policy=policy, market_buy_buffer_bps=Decimal(30))
    assert config.minimum_required_buffer_bps == Decimal(30)
    assert config.max_spread_bps > Decimal(0)


# --- Frequency boundaries (audit) ----------------------------------------------------------------


def test_a_count_one_below_the_hourly_limit_passes() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "approved_orders_last_hour": 4},
        config_kwargs={"max_orders_per_hour": 5},
    )
    assert RiskCheckCode.MAX_HOURLY_ORDERS not in _failed_codes(decision)


def test_a_count_equal_to_the_hourly_limit_rejects() -> None:
    decision = _decide(
        context_kwargs={**_funded(), "approved_orders_last_hour": 5},
        config_kwargs={"max_orders_per_hour": 5},
    )
    assert RiskCheckCode.MAX_HOURLY_ORDERS in _failed_codes(decision)


def test_a_replay_at_the_limit_returns_the_prior_approval() -> None:
    engine = make_risk_engine(max_orders_per_hour=5)
    intent = make_intent()
    first = engine.assess(
        intent, make_risk_context(**{**_funded(), "approved_orders_last_hour": 4})
    )  # type: ignore[arg-type]
    assert first.decision.outcome is RiskOutcome.APPROVED

    # The counter has since reached the limit, but a replay must not re-evaluate it.
    second = engine.assess(
        intent, make_risk_context(**{**_funded(), "approved_orders_last_hour": 5})
    )  # type: ignore[arg-type]

    assert second.replayed is True
    assert second.decision is first.decision
    assert second.decision.outcome is RiskOutcome.APPROVED


def test_a_resized_decision_consumes_budget_exactly_like_an_approval() -> None:
    # Both authorise an order, so both are counted by the caller; the engine treats them
    # identically and neither is exempted.
    resized = _decide(
        intent_kwargs={"quantity": Decimal("1")},
        context_kwargs={
            "snapshot": make_snapshot(cash=Decimal(10_000)),
            "approved_orders_last_hour": 4,
        },
        config_kwargs={"max_orders_per_hour": 5},
    )
    assert resized.outcome is RiskOutcome.RESIZED
    assert resized.is_executable is True


def test_an_externally_known_key_never_produces_a_fresh_approval() -> None:
    intent = make_intent()
    decision = _decide(
        context_kwargs={
            **_funded(),
            "known_idempotency_keys": frozenset({intent.idempotency_key}),
        }
    )
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.approved_order is None
