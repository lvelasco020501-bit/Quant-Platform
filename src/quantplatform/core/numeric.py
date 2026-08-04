"""Decimal-first numeric primitives.

Money, prices, quantities, fees and PnL are represented exclusively with
:class:`decimal.Decimal`. Binary floating point is rejected at the domain boundary because
it cannot represent decimal venue tick sizes exactly and silently accumulates error in
accounting.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, InvalidOperation, localcontext
from typing import Annotated, Final

from pydantic import BeforeValidator, Field

from quantplatform.core.constants import (
    BASIS_POINTS_PER_UNIT,
    DECIMAL_WORKING_PRECISION,
    ZERO,
)
from quantplatform.core.errors import DomainValidationError

__all__ = [
    "Fee",
    "Money",
    "NonNegativeMoney",
    "NonNegativeQuantity",
    "Price",
    "Quantity",
    "Rate",
    "apply_basis_points",
    "ceil_to_step",
    "decimal_places",
    "is_multiple_of",
    "quantize_to_step",
    "to_decimal",
]

_FLOAT_REJECTION_MESSAGE: Final[str] = (
    "binary floating point is not accepted for monetary values; provide a Decimal, int or string"
)


def _reject_float(value: object) -> object:
    """Reject :class:`float` inputs before pydantic coerces them to ``Decimal``.

    Args:
        value: Raw input supplied to a decimal-typed model field.

    Returns:
        The unmodified value when it is not a float.

    Raises:
        ValueError: If the value is a binary float.
    """
    if isinstance(value, float):
        raise ValueError(_FLOAT_REJECTION_MESSAGE)
    return value


_NoFloat = BeforeValidator(_reject_float)

Money = Annotated[Decimal, _NoFloat]
"""Signed monetary amount (PnL and cash deltas may be negative)."""

NonNegativeMoney = Annotated[Decimal, _NoFloat, Field(ge=0)]
"""Monetary amount that must never be negative, such as a cash balance."""

Price = Annotated[Decimal, _NoFloat, Field(gt=0)]
"""Strictly positive traded price."""

Quantity = Annotated[Decimal, _NoFloat, Field(gt=0)]
"""Strictly positive order or fill quantity."""

NonNegativeQuantity = Annotated[Decimal, _NoFloat, Field(ge=0)]
"""Quantity that may be zero, such as a flat position size."""

Fee = Annotated[Decimal, _NoFloat, Field(ge=0)]
"""Commission or venue fee; never negative (rebates are modelled separately)."""

Rate = Annotated[Decimal, _NoFloat, Field(ge=0, le=1)]
"""Unit-interval ratio such as a confidence score or a fractional limit."""


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert a safe input type to :class:`~decimal.Decimal`.

    Args:
        value: A ``Decimal``, ``int`` or numeric string. Floats are rejected.

    Returns:
        The exact decimal representation of ``value``.

    Raises:
        DomainValidationError: If the value is a float or is not parseable.
    """
    if isinstance(value, float):
        raise DomainValidationError(_FLOAT_REJECTION_MESSAGE, value=repr(value))
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise DomainValidationError("value is not a valid decimal", value=repr(value)) from exc


def decimal_places(step: Decimal) -> int:
    """Return the number of decimal places implied by a venue step size.

    Args:
        step: Positive step size, for example ``Decimal("0.001")``.

    Returns:
        Count of significant fractional digits, zero for integral steps.

    Raises:
        DomainValidationError: If ``step`` is not strictly positive.
    """
    if step <= ZERO:
        raise DomainValidationError("step must be strictly positive", step=str(step))
    exponent = step.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - only for NaN/Infinity
        raise DomainValidationError("step must be a finite decimal", step=str(step))
    return max(0, -exponent)


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    """Return whether ``value`` is an exact integer multiple of ``step``.

    Args:
        value: Value to test.
        step: Positive step size.

    Returns:
        ``True`` when ``value % step`` is exactly zero.

    Raises:
        DomainValidationError: If ``step`` is not strictly positive.
    """
    if step <= ZERO:
        raise DomainValidationError("step must be strictly positive", step=str(step))
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        return value % step == ZERO


def quantize_to_step(
    value: Decimal,
    step: Decimal,
    *,
    round_down: bool = True,
) -> Decimal:
    """Snap ``value`` onto the venue ``step`` grid.

    Args:
        value: Value to quantize.
        step: Positive step size such as a lot size or tick size.
        round_down: When ``True`` (default) always round toward zero, which is the safe
            direction for order quantities because it can never exceed available balance.
            When ``False`` round half to even, appropriate for reference prices.

    Returns:
        The quantized value, carrying the number of decimal places implied by ``step``.

    Raises:
        DomainValidationError: If ``step`` is not strictly positive.
    """
    if step <= ZERO:
        raise DomainValidationError("step must be strictly positive", step=str(step))
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        multiples = value / step
        rounding = ROUND_DOWN if round_down else ROUND_HALF_EVEN
        snapped = multiples.quantize(Decimal(1), rounding=rounding) * step
    return snapped.quantize(step.normalize(), rounding=ROUND_HALF_EVEN)


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Snap ``value`` up onto the venue ``step`` grid.

    The upward counterpart of :func:`quantize_to_step`. Rounding up is the safe direction
    for a price ceiling — a sell limit that must not end up below what was asked for, or a
    market-buy cap that must not end up under-reserving — exactly as rounding down is the
    safe direction for a quantity.

    Args:
        value: Value to quantize.
        step: Positive step size such as a tick size.

    Returns:
        The smallest venue-valid value not below ``value``.

    Raises:
        DomainValidationError: If ``step`` is not strictly positive.
    """
    if step <= ZERO:
        raise DomainValidationError("step must be strictly positive", step=str(step))
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        multiples = value / step
        snapped = multiples.quantize(Decimal(1), rounding=ROUND_UP) * step
    return snapped.quantize(step.normalize(), rounding=ROUND_HALF_EVEN)


def apply_basis_points(value: Decimal, basis_points: Decimal) -> Decimal:
    """Return the portion of ``value`` represented by ``basis_points``.

    Args:
        value: Base amount, typically a notional.
        basis_points: Rate expressed in basis points, where ``10_000`` equals 100 percent.

    Returns:
        ``value * basis_points / 10_000`` computed in a high-precision decimal context.
    """
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        return value * basis_points / BASIS_POINTS_PER_UNIT
