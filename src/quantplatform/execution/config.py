"""Deterministic execution assumptions for the simulated broker.

Every knob here is a fixed rule, never a random draw: a backtest whose fills depend on a
random number is not reproducible, and reproducibility is the whole point of simulating
execution rather than guessing at it. Each configuration therefore maps a matched order and
a closed bar onto exactly one price, one quantity and one fee.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantplatform.core.constants import BASIS_POINTS_PER_UNIT, ONE, ZERO
from quantplatform.core.enums import CommissionModel, OrderSide, SlippageModel
from quantplatform.core.numeric import NonNegativeMoney, apply_basis_points

__all__ = ["CommissionConfig", "ExecutionConfig", "SlippageConfig"]

_MAX_BASIS_POINTS = BASIS_POINTS_PER_UNIT
"""Ten thousand basis points is one hundred percent: no fee or slippage rate above this is
an assumption anyone means to make, so it is rejected as a configuration error rather than
silently modelled."""


class _ExecutionSection(BaseModel):
    """Base class for the broker's frozen configuration sections."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class SlippageConfig(_ExecutionSection):
    """How far the execution price moves against the taker.

    Slippage is applied to **market orders only**. A limit order executes at its limit price
    by definition; moving that price by slippage would fill the order outside the limit the
    risk engine approved, which is a worse lie than modelling no slippage at all.
    """

    model: SlippageModel = SlippageModel.OFF
    basis_points: NonNegativeMoney = Field(default=ZERO, le=_MAX_BASIS_POINTS)
    """Adverse move applied to a market fill, in basis points: ``10`` is 0.1 percent."""

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        """Require a rate exactly when the model consumes one."""
        if self.model is SlippageModel.OFF and self.basis_points != ZERO:
            msg = "basis_points must be zero when slippage is off"
            raise ValueError(msg)
        return self

    def adjust(self, price: Decimal, side: OrderSide) -> Decimal:
        """Return the execution price after applying the configured adverse move.

        Args:
            price: Matched price before slippage.
            side: Side being executed; slippage always moves against it.

        Returns:
            ``price * (1 + bps)`` for a buy and ``price * (1 - bps)`` for a sell, or ``price``
            unchanged when the model is :attr:`~quantplatform.core.enums.SlippageModel.OFF`.
        """
        if self.model is SlippageModel.OFF:
            return price
        move = apply_basis_points(price, self.basis_points)
        return price + move if side is OrderSide.BUY else price - move


class CommissionConfig(_ExecutionSection):
    """What the venue charges for a fill, always in the quote asset.

    The broker computes this per fill and stamps it onto
    :attr:`~quantplatform.core.models.orders.Fill.fee`. It never aggregates fees: summing
    them across an order or an account belongs to portfolio accounting.

    Every model is **boundable before an order starts executing**, which is what lets a buy
    reserve its commission up front and never under-hold. That is why the flat model charges
    once per order rather than once per fill: the number of partial fills a resting order
    will need is unknowable at reservation time.
    """

    model: CommissionModel = CommissionModel.NONE
    basis_points: NonNegativeMoney = Field(default=ZERO, le=_MAX_BASIS_POINTS)
    """Rate charged on the executed notional under
    :attr:`~quantplatform.core.enums.CommissionModel.BASIS_POINTS`, where ``10`` is 0.1
    percent."""

    flat_amount: NonNegativeMoney = ZERO
    """Quote-asset amount charged once per order under
    :attr:`~quantplatform.core.enums.CommissionModel.FLAT`."""

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        """Require exactly the parameter the selected model consumes, and no other."""
        if self.model is not CommissionModel.BASIS_POINTS and self.basis_points != ZERO:
            msg = "basis_points is only meaningful for the basis-points commission model"
            raise ValueError(msg)
        if self.model is not CommissionModel.FLAT and self.flat_amount != ZERO:
            msg = "flat_amount is only meaningful for the flat commission model"
            raise ValueError(msg)
        return self

    def charge(self, notional: Decimal, *, is_first_fill: bool) -> Decimal:
        """Return the quote-asset fee for one fill.

        Args:
            notional: Executed price multiplied by executed quantity.
            is_first_fill: Whether this is the order's first execution. Only the flat model
                consults it, charging on the first fill and never again.

        Returns:
            Zero, the flat per-order amount, or a basis-point share of ``notional``.
        """
        if self.model is CommissionModel.NONE:
            return ZERO
        if self.model is CommissionModel.FLAT:
            return self.flat_amount if is_first_fill else ZERO
        return apply_basis_points(notional, self.basis_points)

    def maximum_charge(self, maximum_notional: Decimal) -> Decimal:
        """Return the most this model can ever charge across an entire order.

        Used to size a buy reservation. Because a buy never executes above the notional it
        reserved against — a limit buy fills at its limit, a market buy is capped by
        ``max_execution_price`` — a basis-point fee on ``maximum_notional`` bounds the sum of
        the per-fill fees, and the flat fee is charged once regardless of how the order is
        sliced.
        """
        return self.charge(maximum_notional, is_first_fill=True)


class ExecutionConfig(_ExecutionSection):
    """Complete deterministic execution profile of a simulated venue."""

    slippage: SlippageConfig = Field(default_factory=SlippageConfig)
    commission: CommissionConfig = Field(default_factory=CommissionConfig)
    fill_ratio: Decimal = Field(default=ONE, gt=0, le=1)
    """Fraction of an order's remaining quantity executed per matching bar.

    ``1`` (the default) fills everything matchable in one go. A smaller value models a book
    that absorbs an order gradually — ``0.25``, ``0.5`` and ``0.75`` are the intended
    settings — without any randomness: the same order against the same bars always produces
    the same sequence of partial fills. The broker rounds each slice down onto the symbol's
    quantity step and never leaves a remainder below the venue minimum, so a partially
    filled order always terminates rather than halving forever.
    """
