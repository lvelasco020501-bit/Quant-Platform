"""Deterministic order sizing and price normalisation.

Pure functions over :class:`~decimal.Decimal`: no state, no clock, no configuration lookups.
Keeping them separate from the engine makes the arithmetic that decides how much is traded
directly testable, and makes it obvious that none of it can consult anything but its
arguments.

Every rounding direction here is chosen so that the error is on the safe side. A quantity
always rounds **down**, because rounding a size up can spend money the account does not have.
A buy limit price rounds **down** and a sell limit price rounds **up**, because rounding
either toward the market would execute at a worse price than was asked for. A market-buy cap
rounds **up**, because a cap that rounds down under-reserves.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.enums import OrderSide
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.numeric import apply_basis_points, ceil_to_step, quantize_to_step

__all__ = [
    "market_buy_price_cap",
    "normalize_limit_price",
    "normalize_quantity",
    "quantity_for_notional",
]


def normalize_quantity(quantity: Decimal, rules: SymbolRules) -> Decimal:
    """Snap a quantity down onto the venue lot grid.

    Always downward, never upward: an order rounded up can exceed the balance or exposure
    that justified its size, which is the one direction a risk engine must never move in.

    Args:
        quantity: Raw, unrounded quantity.
        rules: Venue rules carrying the lot step.

    Returns:
        The largest venue-valid quantity not exceeding ``quantity``; zero when the input is
        smaller than a single lot.
    """
    if quantity <= ZERO:
        return ZERO
    return rules.quantize_quantity(quantity)


def quantity_for_notional(notional: Decimal, price: Decimal, rules: SymbolRules) -> Decimal:
    """Convert a requested quote-asset notional into a venue-valid quantity.

    Args:
        notional: Quote-asset amount the intent asked to deploy.
        price: Reference price used to convert it into a quantity.
        rules: Venue rules carrying the lot step.

    Returns:
        The largest venue-valid quantity whose value does not exceed ``notional``; zero when
        the notional does not buy a single lot.
    """
    if notional <= ZERO or price <= ZERO:
        return ZERO
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        raw = notional / price
    return normalize_quantity(raw, rules)


def normalize_limit_price(price: Decimal, side: OrderSide, rules: SymbolRules) -> Decimal:
    """Snap a limit price onto the venue tick grid, away from the market.

    A **buy** limit rounds down and a **sell** limit rounds up. Both move the price to the
    side that is more favourable to the account: rounding a buy limit up, or a sell limit
    down, would authorise execution at a worse price than the strategy asked for, which is a
    silent loosening of the very constraint a limit order exists to express.

    Args:
        price: Raw limit price.
        side: Side of the order.
        rules: Venue rules carrying the price tick.

    Returns:
        The nearest venue-valid price on the favourable side of ``price``.
    """
    if side is OrderSide.BUY:
        return quantize_to_step(price, rules.price_tick, round_down=True)
    return ceil_to_step(price, rules.price_tick)


def market_buy_price_cap(
    reference_price: Decimal,
    buffer_basis_points: Decimal,
    rules: SymbolRules,
) -> Decimal:
    """Return the highest price a market buy may be executed at.

    ``reference_price * (1 + buffer_bps / 10_000)``, rounded **up** to the venue tick. The
    upward rounding matters: a cap rounded down could land below the price the venue actually
    fills at, and the resulting order would be reserved for less than it costs.

    Args:
        reference_price: Price the intent was valued at.
        buffer_basis_points: Total headroom above it, covering slippage, spread and any
            configured safety margin.
        rules: Venue rules carrying the price tick.

    Returns:
        A venue-valid price at or above the buffered reference price.
    """
    buffered = reference_price + apply_basis_points(reference_price, buffer_basis_points)
    return ceil_to_step(buffered, rules.price_tick)
