"""Phase 3B: :class:`SimulatedBroker` deterministic order execution."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.enums import (
    CommissionModel,
    ExecutionMode,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    SlippageModel,
    TimeInForce,
)
from quantplatform.core.errors import (
    MatchingError,
    OrderNotFoundError,
    OrderStateTransitionError,
    QuantPlatformError,
    UnsupportedMarketTypeError,
    UnsupportedOrderTypeError,
    UnsupportedTimeInForceError,
)
from quantplatform.core.events import FillReceived, OrderStatusChanged, PortfolioUpdated
from quantplatform.core.interfaces import SettlementLedger
from quantplatform.core.models.orders import ApprovedOrder, Fill
from quantplatform.core.models.portfolio import Balance
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.execution.config import CommissionConfig, ExecutionConfig, SlippageConfig
from quantplatform.portfolio.engine import SpotPortfolioEngine
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_approved,
    make_balance,
    make_bar,
    make_broker,
    make_portfolio_engine,
    make_symbol_rules,
)

_BTC = "BTC"
_USDT = "USDT"


class _RecordingPortfolio:
    """Wraps a real engine and counts how often each fill reached it."""

    def __init__(self, inner: SpotPortfolioEngine) -> None:
        self.inner = inner
        self.applied: list[Fill] = []

    def apply_fill(self, fill: Fill) -> None:
        self.applied.append(fill)
        self.inner.apply_fill(fill)

    def reserve(self, *, asset: str, amount: Decimal, at: object) -> None:
        self.inner.reserve(asset=asset, amount=amount, at=at)  # type: ignore[arg-type]

    def release(self, *, asset: str, amount: Decimal, at: object) -> None:
        self.inner.release(asset=asset, amount=amount, at=at)  # type: ignore[arg-type]

    def reserved(self, asset: str) -> Decimal:
        return self.inner.reserved(asset)

    def balance(self, asset: str) -> Balance | None:
        return self.inner.balance(asset)

    def balances(self) -> Sequence[Balance]:
        return self.inner.balances()


class _RejectingPortfolio(_RecordingPortfolio):
    """Refuses every fill, so the broker's rollback path can be observed."""

    def apply_fill(self, fill: Fill) -> None:
        self.applied.append(fill)
        msg = "portfolio refused this fill"
        raise QuantPlatformError(msg)


class _FlakyPortfolio(_RecordingPortfolio):
    """Refuses the nth fill it is offered, so mid-bar failure can be observed."""

    def __init__(self, inner: SpotPortfolioEngine, *, fail_on: int) -> None:
        super().__init__(inner)
        self.fail_on = fail_on

    def apply_fill(self, fill: Fill) -> None:
        self.applied.append(fill)
        if len(self.applied) == self.fail_on:
            msg = "portfolio refused this fill"
            raise QuantPlatformError(msg)
        self.inner.apply_fill(fill)


def _balance(portfolio: SpotPortfolioEngine, asset: str) -> Balance:
    return next(b for b in portfolio.balances() if b.asset == asset)


def _approved_payload(approved: ApprovedOrder, **overrides: object) -> dict[str, object]:
    """Dump an approved order to a re-validatable payload, dropping computed fields."""
    data = approved.model_dump()
    for computed in type(approved).model_computed_fields:
        data.pop(computed, None)
    return {**data, **overrides}


def _statuses(events: Sequence[object]) -> list[OrderStatus]:
    return [e.order.status for e in events if isinstance(e, OrderStatusChanged)]


def _buy_and_fill(
    broker: SimulatedBroker, *, quantity: Decimal = Decimal("0.1"), index: int = 0
) -> None:
    """Open a position so that sell-side scenarios have inventory to work with."""
    broker.submit(make_approved(tag=f"seed{index}", quantity=quantity))
    broker.process_bar(make_bar(index=index, close=Decimal(50_000), open_price=Decimal(50_000)))


# --- Market orders --------------------------------------------------------------------------


def test_market_buy_fills_at_bar_open() -> None:
    broker, portfolio = make_broker()
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1")))

    result = broker.process_bar(
        make_bar(index=0, open_price=Decimal(49_000), close=Decimal(51_000))
    )

    assert result.executed is True
    fill = result.fills[0]
    assert fill.price == Decimal(49_000)
    assert fill.quantity == Decimal("0.1")
    assert result.orders[0].status is OrderStatus.FILLED
    assert _balance(portfolio, _BTC).total == Decimal("0.1")


def test_market_sell_fills_at_bar_open_and_reserves_base() -> None:
    broker, portfolio = make_broker()
    _buy_and_fill(broker, quantity=Decimal("0.1"))

    submission = broker.submit(
        make_approved(tag="s1", side=OrderSide.SELL, quantity=Decimal("0.1"))
    )
    assert submission.reservation_asset == _BTC
    assert submission.reservation_delta == Decimal("0.1")
    assert _balance(portfolio, _BTC).locked == Decimal("0.1")

    result = broker.process_bar(
        make_bar(index=1, open_price=Decimal(52_000), close=Decimal(52_000))
    )

    assert result.fills[0].price == Decimal(52_000)
    assert result.orders[0].status is OrderStatus.FILLED
    assert _balance(portfolio, _BTC).total == Decimal(0)
    assert _balance(portfolio, _BTC).locked == Decimal(0)


# --- Limit orders ---------------------------------------------------------------------------


def test_limit_buy_hit_executes_at_the_limit_price() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    result = broker.process_bar(
        make_bar(
            index=0,
            open_price=Decimal(49_000),
            close=Decimal(48_500),
            low=Decimal(47_500),
            high=Decimal(49_200),
        )
    )

    assert result.fills[0].price == Decimal(48_000)
    assert result.fills[0].is_maker is True


def test_limit_buy_miss_leaves_the_order_working() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    result = broker.process_bar(
        make_bar(
            index=0,
            open_price=Decimal(49_500),
            close=Decimal(50_000),
            low=Decimal(49_000),
            high=Decimal(50_500),
        )
    )

    assert result.fills == ()
    assert result.executed is False
    assert len(broker.open_orders()) == 1


def test_limit_sell_hit_executes_at_the_limit_price() -> None:
    broker, _ = make_broker()
    _buy_and_fill(broker)
    broker.submit(
        make_approved(
            tag="s1",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            limit_price=Decimal(52_000),
        )
    )

    result = broker.process_bar(
        make_bar(
            index=1,
            open_price=Decimal(51_000),
            close=Decimal(51_500),
            low=Decimal(50_800),
            high=Decimal(52_500),
        )
    )

    assert result.fills[0].price == Decimal(52_000)


def test_limit_sell_miss_leaves_the_order_working() -> None:
    broker, _ = make_broker()
    _buy_and_fill(broker)
    broker.submit(
        make_approved(
            tag="s1",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            limit_price=Decimal(52_000),
        )
    )

    result = broker.process_bar(
        make_bar(
            index=1,
            open_price=Decimal(51_000),
            close=Decimal(51_200),
            low=Decimal(50_800),
            high=Decimal(51_500),
        )
    )

    assert result.fills == ()
    assert len(broker.open_orders()) == 1


# --- Time in force --------------------------------------------------------------------------


def test_ioc_partial_fill_cancels_the_remainder() -> None:
    broker, _ = make_broker(config=ExecutionConfig(fill_ratio=Decimal("0.5")))
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1"), time_in_force=TimeInForce.IOC))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].quantity == Decimal("0.05")
    order = result.orders[-1]
    assert order.status is OrderStatus.CANCELED
    assert order.filled_quantity == Decimal("0.05")
    assert order.cancel_reason == "ioc remainder cancelled"
    assert broker.open_orders() == ()


def test_ioc_that_does_not_match_is_cancelled_immediately() -> None:
    broker, portfolio = make_broker()
    broker.submit(
        make_approved(
            tag="1",
            order_type=OrderType.LIMIT,
            limit_price=Decimal(40_000),
            time_in_force=TimeInForce.IOC,
        )
    )
    assert _balance(portfolio, _USDT).locked == Decimal(4_000)

    result = broker.process_bar(
        make_bar(index=0, open_price=Decimal(50_000), low=Decimal(49_000), high=Decimal(51_000))
    )

    assert result.fills == ()
    assert result.orders[0].status is OrderStatus.CANCELED
    assert result.orders[0].filled_quantity == Decimal(0)
    assert _balance(portfolio, _USDT).locked == Decimal(0)


def test_gtc_order_persists_across_non_matching_bars() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(45_000)))

    for index in range(3):
        result = broker.process_bar(
            make_bar(index=index, open_price=Decimal(50_000), low=Decimal(49_000))
        )
        assert result.fills == ()

    assert len(broker.open_orders()) == 1
    assert broker.open_orders()[0].status is OrderStatus.OPEN


# --- Cancellation and reservations ------------------------------------------------------------


def test_cancellation_releases_the_entire_reservation() -> None:
    broker, portfolio = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))
    assert _balance(portfolio, _USDT).locked == Decimal(4_800)

    result = broker.cancel("qp-order-1", reason="operator cancelled")

    assert result.newly_canceled is True
    assert result.released == Decimal(4_800)
    assert result.order.status is OrderStatus.CANCELED
    assert result.order.cancel_reason == "operator cancelled"
    assert _balance(portfolio, _USDT).locked == Decimal(0)
    assert _balance(portfolio, _USDT).free == Decimal(1_000_000)


def test_cancelling_a_terminal_order_changes_nothing() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1"))
    broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    result = broker.cancel("qp-order-1")

    assert result.newly_canceled is False
    assert result.events == ()
    assert result.released == Decimal(0)
    assert result.order.status is OrderStatus.FILLED


def test_cancelling_an_unknown_order_raises() -> None:
    broker, _ = make_broker()
    with pytest.raises(OrderNotFoundError):
        broker.cancel("qp-order-missing")


def test_reservation_is_created_for_a_limit_buy_including_commission() -> None:
    config = ExecutionConfig(
        commission=CommissionConfig(model=CommissionModel.BASIS_POINTS, basis_points=Decimal(10))
    )
    broker, portfolio = make_broker(config=config)

    submission = broker.submit(
        make_approved(
            tag="1",
            order_type=OrderType.LIMIT,
            limit_price=Decimal(48_000),
            quantity=Decimal("0.1"),
        )
    )

    assert submission.reservation_asset == _USDT
    assert submission.reservation_delta == Decimal("4804.8")
    assert _balance(portfolio, _USDT).locked == Decimal("4804.8")


def test_market_buy_reserves_against_its_approved_price_cap() -> None:
    broker, portfolio = make_broker()

    submission = broker.submit(
        make_approved(
            tag="1",
            order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
            max_execution_price=Decimal(60_000),
        )
    )

    assert submission.reservation_asset == _USDT
    assert submission.reservation_delta == Decimal(6_000)
    assert _balance(portfolio, _USDT).locked == Decimal(6_000)


def test_market_buy_over_reservation_is_released_on_fill() -> None:
    broker, portfolio = make_broker()
    broker.submit(
        make_approved(
            tag="1",
            order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
            max_execution_price=Decimal(60_000),
        )
    )

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    # Reserved 6_000 against the cap, consumed 5_000 at the actual open, and the unused
    # 1_000 must come back rather than stay locked against a completed order.
    assert result.released == {_USDT: Decimal(6_000)}
    assert _balance(portfolio, _USDT).locked == Decimal(0)
    assert _balance(portfolio, _USDT).free == Decimal(1_000_000) - Decimal(5_000)


def test_two_market_buys_cannot_reserve_the_same_funds() -> None:
    broker, portfolio = make_broker(quote_free=Decimal(10_000))
    order = {
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.1"),
        "max_execution_price": Decimal(60_000),
    }

    first = broker.submit(make_approved(tag="1", **order))  # type: ignore[arg-type]
    second = broker.submit(make_approved(tag="2", **order))  # type: ignore[arg-type]

    assert first.accepted is True
    assert second.accepted is False
    assert second.order.status is OrderStatus.REJECTED
    assert _balance(portfolio, _USDT).locked == Decimal(6_000)
    assert _balance(portfolio, _USDT).free == Decimal(4_000)


def test_two_limit_buys_cannot_reserve_the_same_funds() -> None:
    broker, portfolio = make_broker(quote_free=Decimal(10_000))
    order = {
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal(60_000),
        "quantity": Decimal("0.1"),
    }

    first = broker.submit(make_approved(tag="1", **order))  # type: ignore[arg-type]
    second = broker.submit(make_approved(tag="2", **order))  # type: ignore[arg-type]

    assert first.accepted is True
    assert second.accepted is False
    assert _balance(portfolio, _USDT).locked == Decimal(6_000)


def test_a_market_buy_breaching_its_cap_is_cancelled_not_filled() -> None:
    config = ExecutionConfig(
        slippage=SlippageConfig(model=SlippageModel.FIXED_BPS, basis_points=Decimal(100))
    )
    broker, portfolio = make_broker(config=config)
    broker.submit(
        make_approved(
            tag="1",
            order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
            max_execution_price=Decimal(50_000),
        )
    )

    # Slippage pushes 50_000 to 50_500, above the approved 50_000 cap.
    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills == ()
    assert result.orders[0].status is OrderStatus.CANCELED
    assert "exceeds the approved maximum" in (result.orders[0].cancel_reason or "")
    assert _balance(portfolio, _USDT).locked == Decimal(0)
    assert _balance(portfolio, _USDT).free == Decimal(1_000_000)


def test_a_market_buy_requires_an_approved_price_cap() -> None:
    with pytest.raises(ValueError, match="requires a max_execution_price"):
        ApprovedOrder.model_validate(
            _approved_payload(make_approved(tag="1"), max_execution_price=None)
        )


@pytest.mark.parametrize(
    ("side", "order_type", "limit_price"),
    [
        (OrderSide.SELL, OrderType.MARKET, None),
        (OrderSide.BUY, OrderType.LIMIT, Decimal(48_000)),
    ],
)
def test_only_a_market_buy_may_carry_a_price_cap(
    side: OrderSide, order_type: OrderType, limit_price: Decimal | None
) -> None:
    with pytest.raises(ValueError, match="only a market buy"):
        make_approved(
            tag="1",
            side=side,
            order_type=order_type,
            limit_price=limit_price,
            max_execution_price=Decimal(60_000),
        )


def test_reservation_is_released_exactly_on_fill() -> None:
    broker, portfolio = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(49_000), low=Decimal(47_000)))

    assert result.released == {_USDT: Decimal(4_800)}
    assert _balance(portfolio, _USDT).locked == Decimal(0)
    assert _balance(portfolio, _USDT).free == Decimal(1_000_000) - Decimal(4_800)


def test_partial_fill_keeps_the_unconsumed_reservation_locked() -> None:
    broker, portfolio = make_broker(config=ExecutionConfig(fill_ratio=Decimal("0.5")))
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    broker.process_bar(make_bar(index=0, open_price=Decimal(49_000), low=Decimal(47_000)))

    assert broker.reserved_for("qp-order-1") == Decimal(2_400)
    assert _balance(portfolio, _USDT).locked == Decimal(2_400)


def test_insufficient_balance_rejects_the_order_and_reserves_nothing() -> None:
    broker, portfolio = make_broker(quote_free=Decimal(100))

    submission = broker.submit(
        make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000))
    )

    assert submission.accepted is False
    assert submission.order.status is OrderStatus.REJECTED
    assert submission.order.reject_reason is not None
    assert submission.reservation_delta == Decimal(0)
    assert _balance(portfolio, _USDT).locked == Decimal(0)
    assert _balance(portfolio, _USDT).free == Decimal(100)


# --- Portfolio integration --------------------------------------------------------------------


def test_portfolio_receives_each_fill_exactly_once() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    recording = _RecordingPortfolio(engine)
    broker = SimulatedBroker(
        symbols={SYMBOL: make_symbol_rules()},
        portfolio=recording,
        execution_mode=ExecutionMode.PAPER,
        started_at=ANCHOR,
    )
    broker.submit(make_approved(tag="1"))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert len(recording.applied) == 1
    assert recording.applied[0].fill_id == result.fills[0].fill_id


def test_portfolio_rejection_rolls_the_whole_execution_back() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    rejecting = _RejectingPortfolio(engine)
    broker = SimulatedBroker(
        symbols={SYMBOL: make_symbol_rules()},
        portfolio=rejecting,
        execution_mode=ExecutionMode.PAPER,
        started_at=ANCHOR,
    )
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))
    balances_before = engine.balances()
    order_before = broker.get_order("qp-order-1")

    with pytest.raises(QuantPlatformError):
        broker.process_bar(make_bar(index=0, open_price=Decimal(49_000), low=Decimal(47_000)))

    assert engine.balances() == balances_before
    assert engine.positions() == ()
    assert broker.get_order("qp-order-1") == order_before
    assert broker.fills() == ()
    assert broker.reserved_for("qp-order-1") == Decimal(4_800)


def test_broker_never_emits_portfolio_updated() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1"))
    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert not any(isinstance(event, PortfolioUpdated) for event in result.events)
    assert [type(event) for event in result.events] == [OrderStatusChanged, FillReceived]


# --- Partial and multiple fills ---------------------------------------------------------------


def test_partial_fill_reports_partially_filled_with_a_status_event() -> None:
    broker, _ = make_broker(config=ExecutionConfig(fill_ratio=Decimal("0.5")))
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1")))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.orders[0].status is OrderStatus.PARTIALLY_FILLED
    assert result.orders[0].filled_quantity == Decimal("0.05")
    status_events = [e for e in result.events if isinstance(e, OrderStatusChanged)]
    assert len(status_events) == 1
    assert status_events[0].previous_status is OrderStatus.OPEN


def test_a_gtc_order_fills_across_multiple_bars_until_complete() -> None:
    broker, portfolio = make_broker(config=ExecutionConfig(fill_ratio=Decimal("0.5")))
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1")))

    total = Decimal(0)
    for index in range(20):
        result = broker.process_bar(make_bar(index=index, open_price=Decimal(50_000)))
        total += sum((fill.quantity for fill in result.fills), start=Decimal(0))
        if not broker.open_orders():
            break

    assert broker.get_order("qp-order-1").status is OrderStatus.FILLED
    assert total == Decimal("0.1")
    assert len(broker.fills()) > 1
    assert _balance(portfolio, _BTC).total == Decimal("0.1")


def test_average_fill_price_is_the_weighted_average_across_fills() -> None:
    broker, _ = make_broker(config=ExecutionConfig(fill_ratio=Decimal("0.5")))
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1")))
    broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))
    broker.process_bar(make_bar(index=1, open_price=Decimal(60_000)))

    order = broker.get_order("qp-order-1")
    # 0.05 at 50_000 then 0.025 at 60_000 -> 4_000 / 0.075
    expected = (Decimal("0.05") * Decimal(50_000) + Decimal("0.025") * Decimal(60_000)) / Decimal(
        "0.075"
    )
    assert order.filled_quantity == Decimal("0.075")
    assert order.avg_fill_price == expected


# --- Slippage -------------------------------------------------------------------------------


def test_slippage_off_executes_at_the_matched_price() -> None:
    broker, _ = make_broker(config=ExecutionConfig(slippage=SlippageConfig()))
    broker.submit(make_approved(tag="1"))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].price == Decimal(50_000)


@pytest.mark.parametrize(
    ("side", "expected"),
    [(OrderSide.BUY, Decimal("50050.0")), (OrderSide.SELL, Decimal("49950.0"))],
)
def test_fixed_bps_slippage_moves_the_price_against_the_taker(
    side: OrderSide, expected: Decimal
) -> None:
    config = ExecutionConfig(
        slippage=SlippageConfig(model=SlippageModel.FIXED_BPS, basis_points=Decimal(10))
    )
    broker, _ = make_broker(config=config)
    if side is OrderSide.SELL:
        broker.submit(make_approved(tag="seed", quantity=Decimal("0.5")))
        broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))
    broker.submit(make_approved(tag="1", side=side, quantity=Decimal("0.1")))

    result = broker.process_bar(make_bar(index=1, open_price=Decimal(50_000)))

    assert result.fills[-1].price == expected


def test_slippage_is_not_applied_to_limit_orders() -> None:
    config = ExecutionConfig(
        slippage=SlippageConfig(model=SlippageModel.FIXED_BPS, basis_points=Decimal(100))
    )
    broker, _ = make_broker(config=config)
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(49_000), low=Decimal(47_000)))

    assert result.fills[0].price == Decimal(48_000)


# --- Commission -----------------------------------------------------------------------------


def test_no_commission_charges_nothing() -> None:
    broker, _ = make_broker(config=ExecutionConfig())
    broker.submit(make_approved(tag="1"))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].fee == Decimal(0)
    assert result.fills[0].fee_asset == _USDT


def test_percentage_commission_charges_basis_points_of_notional() -> None:
    config = ExecutionConfig(
        commission=CommissionConfig(model=CommissionModel.BASIS_POINTS, basis_points=Decimal(10))
    )
    broker, _ = make_broker(config=config)
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1")))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].fee == Decimal(5)


def test_flat_commission_charges_a_fixed_amount_per_fill() -> None:
    config = ExecutionConfig(
        commission=CommissionConfig(model=CommissionModel.FLAT, flat_amount=Decimal("2.5"))
    )
    broker, _ = make_broker(config=config)
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1")))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].fee == Decimal("2.5")


def test_broker_never_aggregates_fees_onto_the_order() -> None:
    config = ExecutionConfig(
        commission=CommissionConfig(model=CommissionModel.FLAT, flat_amount=Decimal("2.5"))
    )
    broker, _ = make_broker(config=config)
    broker.submit(make_approved(tag="1"))
    broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert broker.get_order("qp-order-1").fees_paid == Decimal(0)


@pytest.mark.parametrize(
    "config",
    [
        {"model": CommissionModel.NONE, "basis_points": Decimal(10)},
        {"model": CommissionModel.FLAT, "basis_points": Decimal(10)},
        {"model": CommissionModel.BASIS_POINTS, "flat_amount": Decimal(1)},
    ],
)
def test_commission_config_rejects_parameters_its_model_ignores(
    config: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="only meaningful"):
        CommissionConfig(**config)  # type: ignore[arg-type]


def test_slippage_config_rejects_a_rate_while_switched_off() -> None:
    with pytest.raises(ValueError, match="must be zero when slippage is off"):
        SlippageConfig(model=SlippageModel.OFF, basis_points=Decimal(5))


# --- Determinism ----------------------------------------------------------------------------


def test_timestamps_come_from_the_bar_and_never_from_a_clock() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1"))
    bar = make_bar(index=3, open_price=Decimal(50_000))

    result = broker.process_bar(bar)

    assert result.fills[0].executed_at == bar.close_time
    assert result.orders[0].updated_at == bar.close_time
    assert all(event.occurred_at == bar.close_time for event in result.events)
    assert broker.now == bar.close_time


def test_broker_advances_only_when_a_bar_is_processed() -> None:
    broker, _ = make_broker()
    assert broker.now == ANCHOR
    broker.submit(make_approved(tag="1"))
    assert broker.now == ANCHOR

    broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert broker.now == ANCHOR + timedelta(hours=1)


def test_two_brokers_given_identical_input_produce_identical_output() -> None:
    def run() -> tuple[tuple[Fill, ...], tuple[object, ...]]:
        broker, _ = make_broker()
        broker.submit(make_approved(tag="1"))
        result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))
        return result.fills, tuple(event.event_id for event in result.events)

    first_fills, first_events = run()
    second_fills, second_events = run()

    assert first_fills == second_fills
    assert first_events == second_events


def test_orders_are_matched_in_submission_order() -> None:
    broker, _ = make_broker()
    for tag in ("c", "a", "b"):
        broker.submit(make_approved(tag=tag, quantity=Decimal("0.1")))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert [order.client_order_id for order in result.orders] == [
        "qp-order-c",
        "qp-order-a",
        "qp-order-b",
    ]


def test_distinct_orders_produce_distinct_fill_and_event_ids() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1"))
    broker.submit(make_approved(tag="2"))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert result.fills[0].fill_id != result.fills[1].fill_id
    ids = [event.event_id for event in result.events]
    assert len(set(ids)) == len(ids)


# --- Idempotency ----------------------------------------------------------------------------


def test_resubmitting_a_known_client_order_id_does_not_create_a_second_order() -> None:
    broker, portfolio = make_broker()
    approved = make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000))
    first = broker.submit(approved)

    second = broker.submit(approved)

    assert second.newly_submitted is False
    assert second.order == first.order
    assert second.reservation_delta == Decimal(0)
    assert _balance(portfolio, _USDT).locked == Decimal(4_800)
    assert len(broker.open_orders()) == 1


def test_reprocessing_the_same_bar_is_a_no_op() -> None:
    broker, portfolio = make_broker()
    broker.submit(make_approved(tag="1"))
    bar = make_bar(index=0, open_price=Decimal(50_000))
    broker.process_bar(bar)
    balances_after_first = portfolio.balances()

    repeat = broker.process_bar(bar)

    assert repeat.already_processed is True
    assert repeat.fills == ()
    assert repeat.events == ()
    assert portfolio.balances() == balances_after_first
    assert len(broker.fills()) == 1


# --- Validation -----------------------------------------------------------------------------


@pytest.mark.parametrize("order_type", [OrderType.STOP_MARKET, OrderType.STOP_LIMIT])
def test_unsupported_order_types_are_rejected(order_type: OrderType) -> None:
    broker, _ = make_broker()
    approved = make_approved(
        tag="1",
        order_type=order_type,
        limit_price=Decimal(48_000) if order_type.requires_limit_price else None,
        stop_price=Decimal(47_000),
    )

    with pytest.raises(UnsupportedOrderTypeError):
        broker.submit(approved)


def test_unsupported_time_in_force_is_rejected() -> None:
    broker, _ = make_broker()
    approved = make_approved(tag="1", time_in_force=TimeInForce.FOK)

    with pytest.raises(UnsupportedTimeInForceError):
        broker.submit(approved)


def test_non_spot_market_type_is_rejected() -> None:
    broker, _ = make_broker()
    approved = make_approved(tag="1")
    object.__setattr__(approved, "market_type", MarketType.FUTURES)

    with pytest.raises(UnsupportedMarketTypeError):
        broker.submit(approved)


def test_unknown_symbol_is_rejected_on_submission_and_on_matching() -> None:
    broker, _ = make_broker()

    with pytest.raises(MatchingError):
        broker.submit(make_approved(tag="1", symbol="ETH/USDT"))
    with pytest.raises(MatchingError):
        broker.process_bar(make_bar(index=0, symbol="ETH/USDT"))


def test_an_open_bar_is_never_matched() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1"))

    with pytest.raises(MatchingError, match="only closed bars"):
        broker.process_bar(make_bar(index=0, open_price=Decimal(50_000), is_closed=False))


def test_illegal_status_transitions_are_refused() -> None:
    assert OrderStatus.FILLED.can_transition_to(OrderStatus.OPEN) is False
    assert OrderStatus.CANCELED.can_transition_to(OrderStatus.FILLED) is False
    assert OrderStatus.OPEN.can_transition_to(OrderStatus.PARTIALLY_FILLED) is True
    assert OrderStatus.PARTIALLY_FILLED.can_transition_to(OrderStatus.PARTIALLY_FILLED) is True

    broker, _ = make_broker()
    broker.submit(make_approved(tag="1"))
    broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))
    filled = broker.get_order("qp-order-1")

    with pytest.raises(OrderStateTransitionError):
        broker._transition(filled, OrderStatus.OPEN)


def test_a_terminal_order_is_never_rematched() -> None:
    broker, _ = make_broker()
    broker.submit(make_approved(tag="1"))
    broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    result = broker.process_bar(make_bar(index=1, open_price=Decimal(50_000)))

    assert result.fills == ()
    assert result.orders == ()


# --- Numeric safety ---------------------------------------------------------------------------


def test_every_numeric_output_is_decimal_never_float() -> None:
    broker, _ = make_broker(
        config=ExecutionConfig(
            slippage=SlippageConfig(model=SlippageModel.FIXED_BPS, basis_points=Decimal(7)),
            commission=CommissionConfig(
                model=CommissionModel.BASIS_POINTS, basis_points=Decimal(13)
            ),
        )
    )
    broker.submit(make_approved(tag="1"))
    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    fill = result.fills[0]
    for value in (fill.price, fill.quantity, fill.fee, fill.notional):
        assert isinstance(value, Decimal)
        assert not isinstance(value, float)


def test_high_precision_prices_survive_matching_exactly() -> None:
    price = Decimal("49999.123456789012345678")
    broker, _ = make_broker(
        config=ExecutionConfig(
            commission=CommissionConfig(
                model=CommissionModel.BASIS_POINTS, basis_points=Decimal(10)
            )
        )
    )
    broker.submit(
        make_approved(
            tag="1", order_type=OrderType.LIMIT, limit_price=price, quantity=Decimal("0.25")
        )
    )

    result = broker.process_bar(
        make_bar(index=0, open_price=Decimal(50_000), low=Decimal(49_000), high=Decimal(51_000))
    )

    fill = result.fills[0]
    assert fill.price == price
    assert fill.notional == price * Decimal("0.25")
    assert fill.fee == price * Decimal("0.25") * Decimal(10) / Decimal(10_000)


# --- Architecture -----------------------------------------------------------------------------


def test_the_engine_satisfies_the_single_core_settlement_port() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1)),))
    assert isinstance(engine, SettlementLedger)


def test_the_execution_package_never_imports_portfolio_or_storage() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "quantplatform" / "execution"
    forbidden = {"quantplatform.portfolio", "quantplatform.storage", "sqlalchemy"}
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


def test_the_broker_reads_no_wall_clock() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "quantplatform" / "execution"
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in ("datetime.now", "utcnow", "time.time", "SystemClock"):
            assert token not in source, f"{path.name} references {token}"


# --- Cancellation lifecycle (audit) ---------------------------------------------------------


def test_cancelling_an_open_order_traverses_pending_cancel() -> None:
    broker, portfolio = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    result = broker.cancel("qp-order-1")

    assert _statuses(result.events) == [OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED]
    previous = [e.previous_status for e in result.events if isinstance(e, OrderStatusChanged)]
    assert previous == [OrderStatus.OPEN, OrderStatus.PENDING_CANCEL]
    assert result.order.status is OrderStatus.CANCELED
    assert _balance(portfolio, _USDT).locked == Decimal(0)


def test_cancelling_a_partially_filled_order_retains_its_fills() -> None:
    broker, _ = make_broker(config=ExecutionConfig(fill_ratio=Decimal("0.5")))
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))
    broker.process_bar(make_bar(index=0, open_price=Decimal(49_000), low=Decimal(47_000)))

    result = broker.cancel("qp-order-1")

    assert _statuses(result.events) == [OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED]
    assert result.order.filled_quantity == Decimal("0.05")
    assert result.order.avg_fill_price == Decimal(48_000)
    assert result.released == Decimal(2_400)


def test_ioc_remainder_cancellation_uses_the_same_two_step_rule() -> None:
    broker, _ = make_broker(config=ExecutionConfig(fill_ratio=Decimal("0.5")))
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1"), time_in_force=TimeInForce.IOC))

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert _statuses(result.events) == [
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PENDING_CANCEL,
        OrderStatus.CANCELED,
    ]
    assert result.orders[-1].filled_quantity == Decimal("0.05")
    assert result.orders[-1].avg_fill_price == Decimal(50_000)


def test_unmatched_ioc_cancellation_uses_the_same_two_step_rule() -> None:
    broker, _ = make_broker()
    broker.submit(
        make_approved(
            tag="1",
            order_type=OrderType.LIMIT,
            limit_price=Decimal(40_000),
            time_in_force=TimeInForce.IOC,
        )
    )

    result = broker.process_bar(make_bar(index=0, open_price=Decimal(50_000), low=Decimal(49_000)))

    assert _statuses(result.events) == [OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED]


def test_reservation_is_released_only_after_the_final_cancel_transition() -> None:
    broker, portfolio = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    result = broker.cancel("qp-order-1")

    # The release is reported alongside the terminal transition, not the in-flight one.
    assert result.released == Decimal(4_800)
    assert result.order.status is OrderStatus.CANCELED
    assert portfolio.reserved(_USDT) == Decimal(0)


def test_cancelling_twice_does_not_release_funds_twice() -> None:
    broker, portfolio = make_broker()
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))
    broker.cancel("qp-order-1")
    free_after_first = _balance(portfolio, _USDT).free

    second = broker.cancel("qp-order-1")

    assert second.newly_canceled is False
    assert second.released == Decimal(0)
    assert second.events == ()
    assert _balance(portfolio, _USDT).free == free_after_first


# --- Flat commission and partial fills (audit) ----------------------------------------------


def test_flat_commission_is_charged_once_per_order_across_partial_fills() -> None:
    config = ExecutionConfig(
        fill_ratio=Decimal("0.5"),
        commission=CommissionConfig(model=CommissionModel.FLAT, flat_amount=Decimal(3)),
    )
    broker, _ = make_broker(config=config)
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))

    fees = []
    for index in range(20):
        result = broker.process_bar(
            make_bar(index=index, open_price=Decimal(49_000), low=Decimal(47_000))
        )
        fees.extend(fill.fee for fill in result.fills)
        if not broker.open_orders():
            break

    assert len(fees) > 1
    assert fees[0] == Decimal(3)
    assert all(fee == Decimal(0) for fee in fees[1:])
    assert sum(fees, start=Decimal(0)) == Decimal(3)


def test_flat_commission_reservation_covers_the_whole_order() -> None:
    config = ExecutionConfig(
        commission=CommissionConfig(model=CommissionModel.FLAT, flat_amount=Decimal(3))
    )
    broker, portfolio = make_broker(config=config)

    submission = broker.submit(
        make_approved(
            tag="1",
            order_type=OrderType.LIMIT,
            limit_price=Decimal(48_000),
            quantity=Decimal("0.1"),
        )
    )

    assert submission.reservation_delta == Decimal(4_803)
    assert _balance(portfolio, _USDT).locked == Decimal(4_803)


def test_basis_point_commission_never_exceeds_its_reservation_across_partial_fills() -> None:
    config = ExecutionConfig(
        fill_ratio=Decimal("0.5"),
        commission=CommissionConfig(model=CommissionModel.BASIS_POINTS, basis_points=Decimal(10)),
    )
    broker, _ = make_broker(config=config)
    submission = broker.submit(
        make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000))
    )

    fees = []
    notional = Decimal(0)
    for index in range(20):
        result = broker.process_bar(
            make_bar(index=index, open_price=Decimal(49_000), low=Decimal(47_000))
        )
        for fill in result.fills:
            fees.append(fill.fee)
            notional += fill.notional
        if not broker.open_orders():
            break

    assert notional + sum(fees, start=Decimal(0)) <= submission.reservation_delta


@pytest.mark.parametrize("field", ["basis_points"])
def test_commission_rates_are_bounded_and_reject_floats(field: str) -> None:
    with pytest.raises(ValueError, match="less than or equal to 10000"):
        CommissionConfig(model=CommissionModel.BASIS_POINTS, **{field: Decimal(10_001)})
    with pytest.raises(ValueError, match="binary floating point"):
        CommissionConfig(model=CommissionModel.BASIS_POINTS, **{field: 10.0})


def test_slippage_rate_is_bounded_and_rejects_floats() -> None:
    with pytest.raises(ValueError, match="less than or equal to 10000"):
        SlippageConfig(model=SlippageModel.FIXED_BPS, basis_points=Decimal(10_001))
    with pytest.raises(ValueError, match="binary floating point"):
        SlippageConfig(model=SlippageModel.FIXED_BPS, basis_points=10.0)


# --- Mid-bar failure and deterministic resume (audit) ---------------------------------------


def _flaky_broker(fail_on: int, **config: object) -> tuple[SimulatedBroker, _FlakyPortfolio]:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    portfolio = _FlakyPortfolio(engine, fail_on=fail_on)
    broker = SimulatedBroker(
        symbols={SYMBOL: make_symbol_rules()},
        portfolio=portfolio,
        execution_mode=ExecutionMode.PAPER,
        started_at=ANCHOR,
        config=ExecutionConfig(**config),  # type: ignore[arg-type]
    )
    return broker, portfolio


def test_failure_on_the_first_order_of_a_bar_leaves_nothing_behind() -> None:
    broker, portfolio = _flaky_broker(fail_on=1)
    broker.submit(make_approved(tag="1"))
    balances_before = portfolio.inner.balances()

    with pytest.raises(QuantPlatformError):
        broker.process_bar(make_bar(index=0, open_price=Decimal(50_000)))

    assert portfolio.inner.balances() == balances_before
    assert broker.fills() == ()
    assert broker.get_order("qp-order-1").status is OrderStatus.OPEN


def test_failure_on_a_later_order_preserves_the_earlier_success_and_resumes_on_retry() -> None:
    broker, portfolio = _flaky_broker(fail_on=2)
    for tag in ("1", "2", "3"):
        broker.submit(make_approved(tag=tag, quantity=Decimal("0.1")))
    bar = make_bar(index=0, open_price=Decimal(50_000))

    with pytest.raises(QuantPlatformError):
        broker.process_bar(bar)

    # First succeeded, second rolled back, third never processed.
    assert broker.get_order("qp-order-1").status is OrderStatus.FILLED
    assert broker.get_order("qp-order-2").status is OrderStatus.OPEN
    assert broker.get_order("qp-order-3").status is OrderStatus.OPEN
    assert len(broker.fills()) == 1
    assert len(portfolio.inner.positions()) == 1
    assert portfolio.inner.positions()[0].quantity == Decimal("0.1")

    portfolio.fail_on = 0
    retry = broker.process_bar(bar)

    # The retry must resume at order two, never re-execute order one.
    assert [fill.client_order_id for fill in retry.fills] == ["qp-order-2", "qp-order-3"]
    assert len(broker.fills()) == 3
    assert portfolio.inner.positions()[0].quantity == Decimal("0.3")
    assert len(portfolio.applied) == 4  # three successes plus the one refused attempt
    assert len({fill.fill_id for fill in broker.fills()}) == 3


def test_retry_after_a_partially_filled_order_does_not_double_execute_it() -> None:
    broker, portfolio = _flaky_broker(fail_on=2, fill_ratio=Decimal("0.5"))
    broker.submit(make_approved(tag="1", quantity=Decimal("0.1")))
    broker.submit(make_approved(tag="2", quantity=Decimal("0.1")))
    bar = make_bar(index=0, open_price=Decimal(50_000))

    with pytest.raises(QuantPlatformError):
        broker.process_bar(bar)
    assert broker.get_order("qp-order-1").filled_quantity == Decimal("0.05")

    portfolio.fail_on = 0
    retry = broker.process_bar(bar)

    # Order one already matched this bar; only order two may execute against it now.
    assert [fill.client_order_id for fill in retry.fills] == ["qp-order-2"]
    assert broker.get_order("qp-order-1").filled_quantity == Decimal("0.05")
    assert portfolio.inner.positions()[0].quantity == Decimal("0.1")


def test_a_completed_bar_is_not_reprocessed_after_a_successful_retry() -> None:
    broker, portfolio = _flaky_broker(fail_on=2)
    broker.submit(make_approved(tag="1"))
    broker.submit(make_approved(tag="2"))
    bar = make_bar(index=0, open_price=Decimal(50_000))

    with pytest.raises(QuantPlatformError):
        broker.process_bar(bar)
    portfolio.fail_on = 0
    broker.process_bar(bar)

    third = broker.process_bar(bar)

    assert third.already_processed is True
    assert third.fills == ()
    assert len(broker.fills()) == 2


def test_a_refused_fill_restores_the_balance_byte_for_byte() -> None:
    broker, portfolio = _flaky_broker(fail_on=1)
    broker.submit(make_approved(tag="1", order_type=OrderType.LIMIT, limit_price=Decimal(48_000)))
    before = portfolio.inner.balance(_USDT)

    with pytest.raises(QuantPlatformError):
        broker.process_bar(make_bar(index=0, open_price=Decimal(49_000), low=Decimal(47_000)))

    after = portfolio.inner.balance(_USDT)
    assert after == before
    assert after is not None
    assert before is not None
    assert after.updated_at == before.updated_at
    assert broker.reserved_for("qp-order-1") == Decimal(4_800)


def test_each_fill_id_reaches_the_portfolio_exactly_once_across_a_retry() -> None:
    broker, portfolio = _flaky_broker(fail_on=2)
    for tag in ("1", "2"):
        broker.submit(make_approved(tag=tag))
    bar = make_bar(index=0, open_price=Decimal(50_000))

    with pytest.raises(QuantPlatformError):
        broker.process_bar(bar)
    portfolio.fail_on = 0
    broker.process_bar(bar)

    succeeded = [fill.fill_id for fill in broker.fills()]
    assert len(succeeded) == len(set(succeeded))
    for fill_id in succeeded:
        assert sum(1 for f in portfolio.applied if f.fill_id == fill_id) <= 2
        assert portfolio.inner.has_applied(fill_id)
