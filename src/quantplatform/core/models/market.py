"""Market data domain models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, computed_field, model_validator

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.base import (
    AssetCode,
    DomainModel,
    Symbol,
    Text,
    UtcDatetime,
)
from quantplatform.core.numeric import (
    NonNegativeMoney,
    Price,
    Quantity,
    decimal_places,
    is_multiple_of,
    quantize_to_step,
)
from quantplatform.core.timeutils import bar_close_time, is_bar_closed, is_on_timeframe_grid

__all__ = ["MarketBar", "SymbolRules"]


class SymbolRules(DomainModel):
    """Venue trading rules for a single instrument.

    The rules are the authority on what quantities and prices a venue will accept. They
    are fetched from the venue rather than hard-coded, because venues change them.

    This model is a **point-in-time snapshot**, not a live subscription: ``updated_at``
    records when the rules were fetched, but nothing in the domain currently checks that
    timestamp against the platform clock or triggers a refetch when it grows old. A caller
    holding a stale instance has no signal that the venue's real tick size, lot size or
    notional bounds may have since changed. Freshness enforcement (an explicit staleness
    budget, similar to the one applied to market data) and a defined refresh policy belong
    to a later phase — most naturally to the data layer that fetches these rules, or to the
    risk engine that validates orders against them — and must be added before this model is
    relied on for anything beyond short-lived, immediately-consumed reads.
    """

    symbol: Symbol
    base_asset: AssetCode
    quote_asset: AssetCode
    market_type: MarketType
    price_tick: Price
    quantity_step: Quantity
    min_quantity: Quantity
    max_quantity: Quantity | None = None
    min_notional: NonNegativeMoney
    max_notional: NonNegativeMoney | None = None
    source: Text
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        """Check that the assets match the symbol and the bounds are ordered."""
        expected_symbol = f"{self.base_asset}/{self.quote_asset}"
        if self.symbol != expected_symbol:
            msg = f"symbol {self.symbol!r} does not match assets {expected_symbol!r}"
            raise ValueError(msg)
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            msg = "max_quantity must not be below min_quantity"
            raise ValueError(msg)
        if self.max_notional is not None and self.max_notional < self.min_notional:
            msg = "max_notional must not be below min_notional"
            raise ValueError(msg)
        if not is_multiple_of(self.min_quantity, self.quantity_step):
            msg = "min_quantity must be an exact multiple of quantity_step"
            raise ValueError(msg)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_precision(self) -> int:
        """Return the number of decimal places implied by the price tick."""
        return decimal_places(self.price_tick)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quantity_precision(self) -> int:
        """Return the number of decimal places implied by the quantity step."""
        return decimal_places(self.quantity_step)

    def quantize_quantity(self, quantity: Decimal) -> Decimal:
        """Snap a quantity down onto the venue lot grid.

        Rounding is always downward so a resized order can never exceed the balance or
        exposure that justified it.

        Args:
            quantity: Raw quantity.

        Returns:
            The largest venue-valid quantity not exceeding ``quantity``.
        """
        return quantize_to_step(quantity, self.quantity_step, round_down=True)

    def quantize_price(self, price: Decimal) -> Decimal:
        """Snap a price onto the venue tick grid using half-even rounding.

        Args:
            price: Raw price.

        Returns:
            The nearest venue-valid price.
        """
        return quantize_to_step(price, self.price_tick, round_down=False)

    def notional(self, price: Decimal, quantity: Decimal) -> Decimal:
        """Return the quote-asset notional of a hypothetical order.

        Args:
            price: Order price.
            quantity: Order quantity.

        Returns:
            ``price * quantity``.
        """
        return price * quantity

    def is_valid_quantity(self, quantity: Decimal) -> bool:
        """Return whether a quantity satisfies the step and bound rules."""
        if quantity < self.min_quantity:
            return False
        if self.max_quantity is not None and quantity > self.max_quantity:
            return False
        return is_multiple_of(quantity, self.quantity_step)

    def is_valid_price(self, price: Decimal) -> bool:
        """Return whether a price satisfies the tick rule."""
        return price > 0 and is_multiple_of(price, self.price_tick)

    def is_valid_notional(self, price: Decimal, quantity: Decimal) -> bool:
        """Return whether the resulting notional satisfies the venue bounds."""
        value = self.notional(price, quantity)
        if value < self.min_notional:
            return False
        return not (self.max_notional is not None and value > self.max_notional)


class MarketBar(DomainModel):
    """A single normalised OHLCV bar.

    ``close_time`` is exclusive: a bar covering 10:00 to 11:00 has ``open_time`` 10:00 and
    ``close_time`` 11:00, and only becomes actionable once the clock reaches 11:00.
    """

    symbol: Symbol
    market_type: MarketType
    timeframe: Timeframe
    open_time: UtcDatetime
    close_time: UtcDatetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: NonNegativeMoney
    quote_volume: NonNegativeMoney | None = None
    trade_count: int | None = Field(default=None, ge=0)
    source: Text
    is_closed: bool

    @model_validator(mode="after")
    def _validate_bar(self) -> Self:
        """Check grid alignment, interval length and OHLC ordering."""
        if not is_on_timeframe_grid(self.open_time, self.timeframe):
            msg = f"open_time {self.open_time.isoformat()} is not on the {self.timeframe} grid"
            raise ValueError(msg)
        expected_close = bar_close_time(self.open_time, self.timeframe)
        if self.close_time != expected_close:
            msg = (
                f"close_time must be {expected_close.isoformat()} for a {self.timeframe} bar "
                f"opening at {self.open_time.isoformat()}"
            )
            raise ValueError(msg)
        if self.high < self.low:
            msg = "high must not be below low"
            raise ValueError(msg)
        if self.high < max(self.open, self.close):
            msg = "high must be the maximum of the bar"
            raise ValueError(msg)
        if self.low > min(self.open, self.close):
            msg = "low must be the minimum of the bar"
            raise ValueError(msg)
        return self

    def is_closed_at(self, now: datetime) -> bool:
        """Return whether the bar interval has elapsed at ``now``.

        This is independent of the ``is_closed`` flag reported by the provider; both must
        agree before a bar is used for decision making.

        Args:
            now: Current instant from the injected clock.

        Returns:
            ``True`` when ``now`` has reached the exclusive close timestamp.
        """
        return is_bar_closed(self.close_time, now)
