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
from quantplatform.core.models.stops import StopSpecification
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
    """Trigger price of a ``STOP``/``STOP_LIMIT`` order — a property of the *order type*.

    Not to be confused with :attr:`protective_stop`, which is a property of the *position*.
    """

    protective_stop: StopSpecification | None = None
    """Where this position stops losing money, if the proposal came with a stop.

    Distinct from :attr:`stop_price` in both meaning and consumer. ``stop_price`` says
    "this is a stop order, trigger it here"; this says "whatever fills, protect it at this
    level", and is what lets the risk engine size against a known loss and an execution
    layer enforce an exit the strategy never has to speak again to produce.

    ``None`` on every intent the platform has built so far, because the concept did not
    exist. Nothing populates it yet — sizing and enforcement are later milestones — so an
    intent carrying one behaves today exactly like an intent that does not.
    """

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
    max_execution_price: Price | None = None
    """The worst price a **market buy** may be executed at, and the basis for reserving its
    funds.

    A market order carries no price, so without this an execution adapter cannot know how
    much quote asset to hold against it, and two market buys approved against the same
    balance would each be accepted while silently competing for the same money. Requiring
    the risk engine to state a ceiling makes the debit boundable in advance
    (``max_execution_price * quantity``, plus commission on that notional) and makes any
    execution above it a contract violation rather than a surprise.

    Required for a market buy and forbidden otherwise: a limit order is already capped by
    its own ``limit_price``, and a sell incurs no quote debit to bound. A *minimum*
    acceptable sell price is a different concept and is deliberately not modelled here.
    """

    time_in_force: TimeInForce
    execution_mode: ExecutionMode
    idempotency_key: str = Field(min_length=8, max_length=128)
    approved_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_prices(self) -> Self:
        """Check the price fields against the order type and side."""
        _validate_price_fields(self.order_type, self.limit_price, self.stop_price)
        needs_cap = self.order_type is OrderType.MARKET and self.side is OrderSide.BUY
        if needs_cap and self.max_execution_price is None:
            msg = "a market buy requires a max_execution_price to bound its quote debit"
            raise ValueError(msg)
        if not needs_cap and self.max_execution_price is not None:
            msg = "only a market buy may carry a max_execution_price"
            raise ValueError(msg)
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
    """Cumulative fees across every fill on this order, denominated in the quote asset.

    A :class:`Fill` may pay its fee in a different asset (see
    :attr:`Fill.fee_asset`); converting or rejecting such a fee before it is folded into
    this quote-denominated aggregate is a Phase 3 responsibility, never a silent sum
    across currencies.
    """

    reject_reason: Text | None = None
    cancel_reason: Text | None = None
    """Why the order was canceled. Required exactly when :attr:`status` is ``CANCELED``.

    Kept distinct from :attr:`reject_reason`, which explains a venue or risk-engine
    refusal that never resulted in a working order: a rejection and a cancellation are
    different events and must never be recorded interchangeably. Each field is only ever
    populated on the status it explains, so a single ``Order`` record can never carry
    both.
    """

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
        if not self.status.may_carry_fills and self.filled_quantity > 0:
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
        if self.status is not OrderStatus.REJECTED and self.reject_reason is not None:
            msg = "only a rejected order may carry a reject_reason"
            raise ValueError(msg)
        if self.status is OrderStatus.CANCELED and self.cancel_reason is None:
            msg = "a canceled order requires a cancel_reason"
            raise ValueError(msg)
        if self.status is not OrderStatus.CANCELED and self.cancel_reason is not None:
            msg = "only a canceled order may carry a cancel_reason"
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
    """The fee charged for this fill, denominated in :attr:`fee_asset` — not necessarily
    the portfolio's quote asset. This is the only place a non-quote-asset fee is
    represented; every aggregate fee field downstream (:attr:`Order.fees_paid`,
    ``Position.fees_paid``, ``PortfolioSnapshot.total_fees``) is quote-asset denominated,
    so folding this value into one of them requires converting or rejecting it first.
    """

    fee_asset: AssetCode
    execution_mode: ExecutionMode
    is_maker: bool | None = None
    executed_at: UtcDatetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def notional(self) -> Decimal:
        """Return the quote-asset value of the fill before fees."""
        return self.quantity * self.price
