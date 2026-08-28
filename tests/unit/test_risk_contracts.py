"""Risk V2 contracts: the vocabulary for surviving a trade, before anything enforces it.

Week 5 ran seven days with ~95% of the account committed to a single entry and nothing
behind it — no stop, no risk budget, no way for the risk engine to close what the strategy
had opened. These models are the missing vocabulary for that layer. **Nothing consumes them
yet**, deliberately: a contract that no execution path reads cannot change how a run
behaves, which is what makes this milestone provably equivalent to V1.

Each test below pins a refusal rather than a happy path, because the failure these models
exist to prevent is a limit that looks present and is not.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from quantplatform.core.enums import (
    CircuitBreakerReason,
    ExecutionMode,
    MarketType,
    OrderSide,
    OrderType,
    RiskActionKind,
    StopKind,
    TimeInForce,
)
from quantplatform.core.models.orders import OrderIntent
from quantplatform.core.models.risk import (
    CircuitBreakerState,
    PositionRiskState,
    RiskAction,
    RiskBudget,
    StopSpecification,
)
from quantplatform.risk.config import RiskConfiguration
from tests.factories import ANCHOR, SYMBOL


def _intent(**overrides: object) -> OrderIntent:
    """Build a minimal valid intent, so a test can vary exactly one thing."""
    defaults: dict[str, object] = {
        "intent_id": uuid4(),
        "signal_id": uuid4(),
        "strategy_id": "ema_trend",
        "strategy_version": "1.0.0",
        "symbol": SYMBOL,
        "market_type": MarketType.SPOT,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "requested_notional": Decimal(1000),
        "time_in_force": TimeInForce.GTC,
        "execution_mode": ExecutionMode.PAPER,
        "idempotency_key": "test-key-0001",
        "reason": "test",
        "created_at": ANCHOR,
    }
    return OrderIntent(**{**defaults, **overrides})  # type: ignore[arg-type]


# --- StopSpecification ------------------------------------------------------------------------


def test_a_stop_may_be_expressed_as_an_absolute_price() -> None:
    stop = StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000"))

    assert stop.kind is StopKind.HARD
    assert stop.trigger_price == Decimal("70000")
    assert stop.distance_bps is None


def test_a_stop_may_be_expressed_as_a_distance() -> None:
    stop = StopSpecification(kind=StopKind.TRAILING, distance_bps=Decimal(200))

    assert stop.distance_bps == Decimal(200)
    assert stop.trigger_price is None


def test_a_stop_with_neither_a_price_nor_a_distance_is_refused() -> None:
    # The failure this whole module exists to prevent, in its smallest form: a stop that
    # cannot be evaluated is not a stop, and accepting one would put a field named `stop`
    # on an intent that protects nothing.
    with pytest.raises(ValidationError, match="requires either a trigger_price"):
        StopSpecification(kind=StopKind.HARD)


def test_a_stop_carrying_both_a_price_and_a_distance_is_refused() -> None:
    # Two sources of truth for one trigger. Whichever the enforcement layer picked, the
    # other would be a silent lie about where the stop sits.
    with pytest.raises(ValidationError, match="not both"):
        StopSpecification(
            kind=StopKind.HARD, trigger_price=Decimal("70000"), distance_bps=Decimal(200)
        )


def test_a_time_stop_is_expressed_in_seconds_rather_than_price() -> None:
    stop = StopSpecification(kind=StopKind.TIME, max_holding_seconds=3600)

    assert stop.max_holding_seconds == 3600
    assert stop.trigger_price is None
    assert stop.distance_bps is None


def test_a_time_stop_without_a_duration_is_refused() -> None:
    with pytest.raises(ValidationError, match="max_holding_seconds"):
        StopSpecification(kind=StopKind.TIME)


def test_a_stop_specification_is_frozen() -> None:
    stop = StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000"))

    with pytest.raises(ValidationError):
        stop.trigger_price = Decimal("60000")  # type: ignore[misc]


# --- RiskBudget -------------------------------------------------------------------------------


def test_a_budget_describes_what_may_be_lost_not_what_may_be_spent() -> None:
    budget = RiskBudget(
        risk_per_trade_pct=Decimal("0.01"),
        max_position_exposure_pct=Decimal("0.25"),
        min_stop_distance_bps=Decimal(50),
        max_stop_distance_bps=Decimal(1000),
    )

    assert budget.risk_per_trade_pct == Decimal("0.01")
    assert budget.max_position_exposure_pct == Decimal("0.25")


def test_a_budget_whose_stop_bounds_cross_is_refused() -> None:
    with pytest.raises(ValidationError, match="min_stop_distance_bps"):
        RiskBudget(
            risk_per_trade_pct=Decimal("0.01"),
            max_position_exposure_pct=Decimal("0.25"),
            min_stop_distance_bps=Decimal(1000),
            max_stop_distance_bps=Decimal(50),
        )


def test_a_budget_risking_the_whole_account_on_one_trade_is_refused() -> None:
    # Not an arbitrary ceiling: risk_per_trade_pct is by definition the fraction of equity
    # a single stop-out may destroy, and 1.0 means one trade may end the account.
    with pytest.raises(ValidationError):
        RiskBudget(
            risk_per_trade_pct=Decimal("1.5"),
            max_position_exposure_pct=Decimal("0.25"),
            min_stop_distance_bps=Decimal(50),
            max_stop_distance_bps=Decimal(1000),
        )


# --- PositionRiskState ------------------------------------------------------------------------


def test_position_risk_state_records_what_was_actually_risked() -> None:
    state = PositionRiskState(
        symbol=SYMBOL,
        stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000")),
        quantity=Decimal("0.05"),
        risk_amount=Decimal("100"),
        entry_price=Decimal("72000"),
        opened_at=ANCHOR,
    )

    assert state.risk_amount == Decimal("100")
    assert state.highest_price_seen is None


def test_a_position_risking_nothing_is_refused() -> None:
    # A recorded risk of zero would make every R-multiple computed from it a division by
    # zero, and would claim a position was protected when nothing was at stake.
    with pytest.raises(ValidationError):
        PositionRiskState(
            symbol=SYMBOL,
            stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000")),
            quantity=Decimal("0.05"),
            risk_amount=Decimal(0),
            entry_price=Decimal("72000"),
            opened_at=ANCHOR,
        )


# --- CircuitBreakerState ----------------------------------------------------------------------


def test_an_untripped_breaker_carries_no_reason() -> None:
    state = CircuitBreakerState()

    assert state.tripped_at is None
    assert state.reason is None
    assert state.is_tripped is False


def test_a_tripped_breaker_must_say_why() -> None:
    # A breaker that halted trading without recording its reason is indistinguishable at
    # 3am from a process that stopped for an unrelated fault.
    with pytest.raises(ValidationError, match="reason"):
        CircuitBreakerState(tripped_at=ANCHOR)


def test_a_tripped_breaker_reports_itself_tripped() -> None:
    state = CircuitBreakerState(
        tripped_at=ANCHOR,
        reason=CircuitBreakerReason.DAILY_LOSS_LIMIT,
        consecutive_losses=3,
    )

    assert state.is_tripped is True
    assert state.reason is CircuitBreakerReason.DAILY_LOSS_LIMIT


def test_a_breaker_reset_time_must_follow_the_trip() -> None:
    with pytest.raises(ValidationError, match="resets_at"):
        CircuitBreakerState(
            tripped_at=ANCHOR,
            reason=CircuitBreakerReason.EXCESSIVE_DRAWDOWN,
            resets_at=ANCHOR - timedelta(hours=1),
        )


# --- RiskAction -------------------------------------------------------------------------------


def test_a_no_op_action_names_nothing_to_act_on() -> None:
    action = RiskAction(kind=RiskActionKind.NONE, reason="all limits within budget")

    assert action.symbol is None
    assert action.quantity is None


def test_an_action_that_closes_a_position_must_name_it() -> None:
    with pytest.raises(ValidationError, match="symbol"):
        RiskAction(kind=RiskActionKind.CLOSE, reason="stop breached")


def test_an_action_that_reduces_a_position_must_say_by_how_much() -> None:
    with pytest.raises(ValidationError, match="quantity"):
        RiskAction(kind=RiskActionKind.REDUCE, symbol=SYMBOL, reason="exposure over budget")


def test_a_close_action_is_well_formed_with_a_symbol() -> None:
    action = RiskAction(kind=RiskActionKind.CLOSE, symbol=SYMBOL, reason="stop breached")

    assert action.symbol == SYMBOL
    assert action.kind is RiskActionKind.CLOSE


# --- V1 equivalence ---------------------------------------------------------------------------


def test_an_unconfigured_risk_configuration_enables_nothing_new() -> None:
    # The equivalence guarantee, stated as a test rather than as a comment. Every V2 field
    # defaults to "not configured" — never to a permissive-looking value that would read as
    # protection nobody actually asked for.
    config = RiskConfiguration()

    assert config.risk_budget is None
    assert config.max_daily_loss_pct is None
    assert config.max_consecutive_losses is None
    assert config.require_stop_on_entry is False


def test_the_v2_fields_can_be_configured_without_disturbing_v1_limits() -> None:
    budget = RiskBudget(
        risk_per_trade_pct=Decimal("0.01"),
        max_position_exposure_pct=Decimal("0.25"),
        min_stop_distance_bps=Decimal(50),
        max_stop_distance_bps=Decimal(1000),
    )
    baseline = RiskConfiguration()
    configured = RiskConfiguration(
        risk_budget=budget,
        initial_stop_distance_bps=Decimal(200),
        max_daily_loss_pct=Decimal("0.02"),
        max_consecutive_losses=3,
        require_stop_on_entry=True,
    )

    assert configured.risk_budget == budget
    # Every V1 limit is untouched by configuring V2.
    assert configured.max_orders_per_day == baseline.max_orders_per_day
    assert configured.max_portfolio_exposure_pct == baseline.max_portfolio_exposure_pct
    assert configured.max_total_drawdown_pct == baseline.max_total_drawdown_pct


# --- M3: stop state persistence ---------------------------------------------------------------
#
# The stop now has somewhere to live: on the intent that proposed it, and on the state that
# outlives the process. Nothing writes either field yet — sizing lands in M4 and enforcement
# in M5 — so every assertion below is about *shape surviving a round trip*, not about
# behaviour. That is the whole milestone: give the data a home before anything depends on it.


def test_an_intent_without_a_protective_stop_is_still_valid() -> None:
    # Every intent the platform has ever built carries no stop, because the concept did not
    # exist. All of them must keep working unchanged.
    intent = _intent()

    assert intent.protective_stop is None


def test_an_intent_can_carry_a_protective_stop() -> None:
    stop = StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000"))

    intent = _intent(protective_stop=stop)

    assert intent.protective_stop == stop


def test_a_protective_stop_is_not_the_order_type_stop_price() -> None:
    # The two fields answer different questions and must not be conflated: stop_price says
    # "this is a stop order, trigger it here"; protective_stop says "this position is
    # protected at this level, by something other than the strategy".
    intent = _intent(
        protective_stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000"))
    )

    assert intent.stop_price is None
    assert intent.protective_stop is not None


def test_an_intent_round_trips_its_stop_through_json() -> None:
    stop = StopSpecification(kind=StopKind.TRAILING, distance_bps=Decimal(250))
    intent = _intent(protective_stop=stop)

    restored = OrderIntent.model_validate_json(intent.model_dump_json())

    assert restored.protective_stop == stop
    assert restored == intent


def test_position_risk_state_round_trips_through_json() -> None:
    state = PositionRiskState(
        symbol=SYMBOL,
        stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000")),
        quantity=Decimal("0.05"),
        risk_amount=Decimal("100.25"),
        entry_price=Decimal("72000"),
        highest_price_seen=Decimal("73500"),
        opened_at=ANCHOR,
    )

    restored = PositionRiskState.model_validate_json(state.model_dump_json())

    assert restored == state
    assert restored.risk_amount == Decimal("100.25")
