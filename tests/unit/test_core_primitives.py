"""Enums, decimal maths, UTC time helpers, identifiers and clocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypedDict, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quantplatform.core.clock import Clock, SimulatedClock, SystemClock
from quantplatform.core.enums import (
    ExecutionMode,
    MarketType,
    OrderStatus,
    SignalAction,
    SystemState,
    Timeframe,
)
from quantplatform.core.errors import DomainValidationError
from quantplatform.core.ids import (
    client_order_id_from_key,
    deterministic_uuid,
    idempotency_key,
)
from quantplatform.core.numeric import (
    apply_basis_points,
    decimal_places,
    is_multiple_of,
    quantize_to_step,
    to_decimal,
)
from quantplatform.core.timeutils import (
    bar_close_time,
    ensure_utc,
    floor_to_timeframe,
    is_bar_closed,
    is_on_timeframe_grid,
)

# --- Enums ---------------------------------------------------------------------------------------


def test_timeframe_durations_are_exact() -> None:
    assert Timeframe.H1.seconds == 3_600
    assert Timeframe.H4.duration == timedelta(hours=4)
    assert Timeframe.D1.duration == timedelta(days=1)
    assert Timeframe.W1.duration == timedelta(days=7)


def test_every_timeframe_declares_a_duration() -> None:
    for timeframe in Timeframe:
        assert timeframe.seconds > 0


def test_spot_forbids_leverage_and_shorting() -> None:
    assert not MarketType.SPOT.allows_leverage
    assert not MarketType.SPOT.allows_short
    assert MarketType.PERPETUAL.allows_leverage
    assert MarketType.PERPETUAL.allows_short


def test_only_live_mode_submits_external_orders() -> None:
    external = [mode for mode in ExecutionMode if mode.submits_external_orders]
    assert external == [ExecutionMode.LIVE]


def test_shadow_mode_simulates_nothing() -> None:
    assert not ExecutionMode.SHADOW.simulates_fills
    assert not ExecutionMode.SHADOW.submits_external_orders
    assert ExecutionMode.SHADOW.uses_real_time_data


def test_only_healthy_state_allows_new_orders() -> None:
    allowed = [state for state in SystemState if state.allows_new_orders]
    assert allowed == [SystemState.HEALTHY]


def test_hold_is_the_only_non_actionable_signal() -> None:
    non_actionable = [action for action in SignalAction if not action.is_actionable]
    assert non_actionable == [SignalAction.HOLD]


@pytest.mark.parametrize(
    "status",
    [OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.UNKNOWN],
)
def test_non_executable_statuses_cannot_produce_fills(status: OrderStatus) -> None:
    assert not status.can_produce_fills


# --- Decimal maths -------------------------------------------------------------------------------


def test_to_decimal_rejects_binary_floats() -> None:
    with pytest.raises(DomainValidationError):
        to_decimal(0.1)  # type: ignore[arg-type]


def test_to_decimal_preserves_exact_string_values() -> None:
    assert to_decimal("0.1") == Decimal("0.1")
    assert to_decimal(7) == Decimal(7)


def test_decimal_places_matches_step_size() -> None:
    assert decimal_places(Decimal("0.001")) == 3
    assert decimal_places(Decimal(1)) == 0
    assert decimal_places(Decimal("10")) == 0


def test_decimal_places_rejects_non_positive_step() -> None:
    with pytest.raises(DomainValidationError):
        decimal_places(Decimal(0))


def test_quantize_rounds_quantities_down() -> None:
    assert quantize_to_step(Decimal("0.123456789"), Decimal("0.0001")) == Decimal("0.1234")


def test_quantize_rounds_prices_to_nearest_tick() -> None:
    assert quantize_to_step(Decimal("100.006"), Decimal("0.01"), round_down=False) == Decimal(
        "100.01"
    )


def test_apply_basis_points_is_exact() -> None:
    assert apply_basis_points(Decimal(10_000), Decimal(10)) == Decimal(10)


@given(
    value=st.decimals(min_value=Decimal(0), max_value=Decimal(1_000_000), places=8),
    exponent=st.integers(min_value=0, max_value=6),
)
def test_downward_quantization_never_exceeds_the_input(value: Decimal, exponent: int) -> None:
    step = Decimal(1).scaleb(-exponent)
    result = quantize_to_step(value, step)
    assert result <= value
    assert is_multiple_of(result, step)
    assert value - result < step


# --- UTC time ------------------------------------------------------------------------------------


def test_ensure_utc_rejects_naive_datetimes() -> None:
    with pytest.raises(DomainValidationError):
        ensure_utc(datetime(2026, 1, 1, 0, 0))  # noqa: DTZ001 - deliberately naive


def test_floor_aligns_to_the_hourly_grid() -> None:
    moment = datetime(2026, 3, 5, 14, 37, 12, tzinfo=UTC)
    assert floor_to_timeframe(moment, Timeframe.H1) == datetime(2026, 3, 5, 14, 0, tzinfo=UTC)


def test_weekly_bars_are_anchored_to_monday() -> None:
    # 2026-03-05 is a Thursday; the containing weekly bar opens on Monday 2026-03-02.
    moment = datetime(2026, 3, 5, 14, 37, tzinfo=UTC)
    floored = floor_to_timeframe(moment, Timeframe.W1)
    assert floored == datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
    assert floored.weekday() == 0


def test_grid_membership_matches_flooring() -> None:
    aligned = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
    assert is_on_timeframe_grid(aligned, Timeframe.H1)
    assert not is_on_timeframe_grid(aligned + timedelta(minutes=1), Timeframe.H1)


def test_bar_is_closed_only_once_the_interval_has_elapsed() -> None:
    open_time = datetime(2026, 3, 5, 14, 0, tzinfo=UTC)
    close_time = bar_close_time(open_time, Timeframe.H1)
    assert close_time == datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
    assert not is_bar_closed(close_time, close_time - timedelta(seconds=1))
    assert is_bar_closed(close_time, close_time)


# --- Identifiers ---------------------------------------------------------------------------------


class _KeyFields(TypedDict):
    """Field set that uniquely identifies an order-producing decision."""

    strategy_id: str
    strategy_version: str
    symbol: str
    signal_time: datetime
    action: SignalAction
    execution_mode: ExecutionMode


_KEY_FIELDS: _KeyFields = {
    "strategy_id": "ema_trend",
    "strategy_version": "1.0.0",
    "symbol": "BTC/USDT",
    "signal_time": datetime(2026, 1, 1, tzinfo=UTC),
    "action": SignalAction.ENTER_LONG,
    "execution_mode": ExecutionMode.PAPER,
}


def _key_with(**overrides: object) -> str:
    """Derive an idempotency key with individual fields replaced."""
    return idempotency_key(**cast("_KeyFields", {**_KEY_FIELDS, **overrides}))


def test_idempotency_key_is_deterministic() -> None:
    assert idempotency_key(**_KEY_FIELDS) == idempotency_key(**_KEY_FIELDS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strategy_id", "breakout"),
        ("strategy_version", "1.0.1"),
        ("symbol", "ETH/USDT"),
        ("signal_time", datetime(2026, 1, 1, 1, tzinfo=UTC)),
        ("action", SignalAction.EXIT_LONG),
        ("execution_mode", ExecutionMode.LIVE),
    ],
)
def test_idempotency_key_changes_with_every_field(field: str, value: object) -> None:
    assert _key_with(**{field: value}) != idempotency_key(**_KEY_FIELDS)


def test_paper_and_live_decisions_never_collide() -> None:
    paper = _key_with(execution_mode=ExecutionMode.PAPER)
    live = _key_with(execution_mode=ExecutionMode.LIVE)
    assert paper != live


def test_idempotency_key_rejects_separator_injection() -> None:
    with pytest.raises(DomainValidationError):
        _key_with(symbol="BTC|USDT")


def test_client_order_id_fits_venue_limits_and_is_stable() -> None:
    key = idempotency_key(**_KEY_FIELDS)
    client_order_id = client_order_id_from_key(key)
    assert client_order_id == client_order_id_from_key(key)
    assert len(client_order_id) <= 36
    assert client_order_id.startswith("qp-")


def test_client_order_id_rejects_foreign_keys() -> None:
    with pytest.raises(DomainValidationError):
        client_order_id_from_key("other-0123456789")


def test_deterministic_uuid_is_stable_and_field_sensitive() -> None:
    first = deterministic_uuid("signal", "ema_trend", "BTC/USDT")
    assert first == deterministic_uuid("signal", "ema_trend", "BTC/USDT")
    assert first != deterministic_uuid("signal", "ema_trend", "ETH/USDT")
    assert first != deterministic_uuid("order_intent", "ema_trend", "BTC/USDT")


def test_deterministic_uuid_requires_identifying_parts() -> None:
    with pytest.raises(DomainValidationError):
        deterministic_uuid("signal")


# --- Clocks --------------------------------------------------------------------------------------


def test_system_clock_satisfies_the_port() -> None:
    clock: Clock = SystemClock()
    assert clock.now().tzinfo is UTC


def test_simulated_clock_only_moves_when_told() -> None:
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)
    clock.advance(timedelta(hours=1))
    assert clock.now() == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert clock.monotonic() == 3_600.0


def test_simulated_clock_refuses_to_move_backwards() -> None:
    clock = SimulatedClock(datetime(2026, 1, 1, 12, tzinfo=UTC))
    with pytest.raises(DomainValidationError):
        clock.set_time(datetime(2026, 1, 1, 11, tzinfo=UTC))
    with pytest.raises(DomainValidationError):
        clock.advance(timedelta(seconds=-1))


def test_simulated_clock_rejects_naive_start() -> None:
    with pytest.raises(DomainValidationError):
        SimulatedClock(datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive


async def test_simulated_sleep_advances_without_waiting() -> None:
    clock: Clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    await clock.sleep(120)
    assert clock.now() == datetime(2026, 1, 1, 0, 2, tzinfo=UTC)


async def test_sleep_rejects_negative_durations() -> None:
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(DomainValidationError):
        await clock.sleep(-1)
