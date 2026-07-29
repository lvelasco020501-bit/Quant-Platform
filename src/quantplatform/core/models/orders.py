"""Order lifecycle domain models: intent, approved order, order and fill."""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, computed_field, model_validator

from quantplatform.core.enums import (
    ExecutionMode,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from quantplatform.core.models.base import (
    AssetCode,
    DomainModel,
    SemanticVersion,
    StrategyId,
    Symbol,
    Text,
    UtcDatetime,
    VenueId,
)
from quantplatform.core.numeric import Fee, Money, NonNegativeQuantity, Price, Quantity

__all__ = ["ApprovedOrder", "Fill", "Order", "OrderIntent"]

_CLIENT_ORDER_ID_CONSTRAINT = Field(min_length=8, max_length=36)


def _validate_price_fields(
    order_type: OrderType,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
) -> None:
    """Check that the price fields match the requirements of the order type.

    Args:
        order_type: Type of the order.
        limit_price: Supplied limit price, if any.
        stop_price: Supplied stop trigger price, if any.

    Raises:
        ValueError: If a required price is missing or an irrelevant price is supplied.
    """
    if order_type.requires_limit_price and limit_price is None:
        msg = f"{order_type} requires a limit_price"
        raise ValueError(msg)
    if not order_type.requires_limit_price and limit_price is not None:
        msg = f"{order_type} must not carry a limit_price"
        raise ValueError(msg)
    if order_type.requires_stop_price and stop_price is None:
        msg = f"{order_type} requires a stop_price"
        raise ValueError(msg)
    if not order_type.requires_stop_price and stop_price is not None:
        msg = f"{order_type} must not carry a stop_price"
        raise ValueError(msg)


class OrderIntent(DomainModel):
    """A proposed order derived from an actionable signal.

    An intent is what the orchestration layer asks the risk engine to evaluate. It may
    express size either as an explicit quantity or as a target notional to be sized by the
    risk engine, but never both.
    """

    intent_id: UUID
    signal_id: UUID
    strategy_id: StrategyId
    strategy_version: SemanticVersion
    symbol: Symbol
    market_type: MarketType
    side: OrderSide
    order_type: OrderType
    requested_quantity: Quantity | None = None
    requested_notional: Money | None = Field(default=None, gt=0)
    limit_price: Price | None = None
    stop_price: Price | None = None
    time_in_force: TimeInForce
    execution_mode: ExecutionMode
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: Text
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_intent(self) -> Self:
        """Check the sizing expression and price fields."""
        has_quantity = self.requested_quantity is not None
        has_notional = self.requested_notional is not None
        if has_quantity == has_notional:
            msg = "exactly one of requested_quantity or requested_notional must be provided"
            raise ValueError(msg)
        _validate_price_fields(self.order_type, self.limit_price, self.stop_price)
        return self


class ApprovedOrder(DomainModel):
    """An order that the risk engine has authorised for submission.

    Instances are only meaningful when carried by a
    :class:`~quantplatform.core.models.risk.RiskDecision`, which enforces that every risk
    check passed. Execution adapters accept nothing else, so a strategy cannot reach a
    venue without traversing the risk engine.
    """

    client_order_id: str = _CLIENT_ORDER_ID_CONSTRAINT
    intent_id: UUID
    decision_id: UUID
    strategy_id: StrategyId
    strategy_version: SemanticVersion
    symbol: Symbol
    market_type: MarketType
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    limit_price: Price | None = None
    stop_price: Price | None = None
    time_in_force: TimeInForce
    execution_mode: ExecutionMode
    idempotency_key: str = Field(min_length=8, max_length=128)
    approved_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_prices(self) -> Self:
        """Check the price fields against the order type."""
        _validate_price_fields(self.order_type, self.limit_price, self.stop_price)
        return self


class Order(DomainModel):
    """The platform's view of an order's lifecycle state.

    Each transition produces a new immutable record, so the full history of an order is
    reconstructable from persistence.
    """

    order_id: UUID
    client_order_id: str = _CLIENT_ORDER_ID_CONSTRAINT
    venue_order_id: VenueId | None = None
    intent_id: UUID
    decision_id: UUID
    strategy_id: StrategyId
    symbol: Symbol
    market_type: MarketType
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    execution_mode: ExecutionMode
    status: OrderStatus
    quantity: Quantity
    filled_quantity: NonNegativeQuantity
    avg_fill_price: Price | None = None
    limit_price: Price | None = None
    stop_price: Price | None = None
    fees_paid: Fee
    reject_reason: Text | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> Self:
        """Check the fill accounting and status-specific invariants."""
        if self.updated_at < self.created_at:
            msg = "updated_at must not precede created_at"
            raise ValueError(msg)
        if self.filled_quantity > self.quantity:
            msg = "filled_quantity must not exceed quantity"
            raise ValueError(msg)
        if self.filled_quantity > 0 and self.avg_fill_price is None:
            msg = "a partially or fully filled order requires an avg_fill_price"
            raise ValueError(msg)
        if self.filled_quantity == 0 and self.avg_fill_price is not None:
            msg = "an unfilled order must not carry an avg_fill_price"
            raise ValueError(msg)
        if not self.status.can_produce_fills and self.filled_quantity > 0:
            msg = f"status {self.status} cannot carry fills"
            raise ValueError(msg)
        if self.status is OrderStatus.FILLED and self.filled_quantity != self.quantity:
            msg = "a filled order must have filled_quantity equal to quantity"
            raise ValueError(msg)
        if self.status is OrderStatus.PARTIALLY_FILLED and not (
            0 < self.filled_quantity < self.quantity
        ):
            msg = "a partially filled order must have 0 < filled_quantity < quantity"
            raise ValueError(msg)
        if self.status is OrderStatus.REJECTED and self.reject_reason is None:
            msg = "a rejected order requires a reject_reason"
            raise ValueError(msg)
        _validate_price_fields(self.order_type, self.limit_price, self.stop_price)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining_quantity(self) -> Decimal:
        """Return the quantity still working at the venue."""
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        """Return whether the order still consumes venue capacity."""
        return self.status.is_open


class Fill(DomainModel):
    """An executed trade against an order.

    Fills are the only source of portfolio state changes and must be applied exactly once.
    """

    fill_id: UUID
    order_id: UUID
    client_order_id: str = _CLIENT_ORDER_ID_CONSTRAINT
    venue_trade_id: VenueId | None = None
    symbol: Symbol
    side: OrderSide
    quantity: Quantity
    price: Price
    fee: Fee
    fee_asset: AssetCode
    execution_mode: ExecutionMode
    is_maker: bool | None = None
    executed_at: UtcDatetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def notional(self) -> Decimal:
        """Return the quote-asset value of the fill before fees."""
        return self.quantity * self.price
