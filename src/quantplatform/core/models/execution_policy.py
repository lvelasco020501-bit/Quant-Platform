"""Execution assumptions shared by the risk engine and the execution adapter.

These models exist because the two components must agree *exactly* on what a trade will
cost. The risk engine decides whether an account can fund an order; the broker then charges
it. If each reads its own copy of the fee schedule and slippage rate, the two drift, and the
symptom is not a loud failure but a quiet one: the risk engine approves orders the broker
then refuses, or approves a price cap the broker's own slippage immediately breaches.

The fix is structural rather than procedural. There is one definition of each formula, it
lives in :mod:`quantplatform.core`, which both packages may import, and a composition root
constructs a single :class:`ExecutionPolicy` and hands the same object to both. Keeping two
numbers equal is then not a thing anyone can forget to do.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from quantplatform.core.constants import BASIS_POINTS_PER_UNIT, ZERO
from quantplatform.core.enums import CommissionModel, OrderSide, SlippageModel
from quantplatform.core.errors import UnsupportedFeeAssetError
from quantplatform.core.models.base import AssetCode, DomainModel
from quantplatform.core.numeric import NonNegativeMoney, apply_basis_points

__all__ = ["ExecutionPolicy", "FeePolicy", "SlippagePolicy"]

MAX_BASIS_POINTS: Decimal = BASIS_POINTS_PER_UNIT
"""Ten thousand basis points is one hundred percent; no rate above it is one anyone means."""


class FeePolicy(DomainModel):
    """What a venue charges for a fill, and the most it can charge for a whole order.

    The two questions this answers are deliberately different. :meth:`fee_for` is what the
    broker stamps on one fill; :meth:`maximum_fee` is what the risk engine must hold back
    before approving anything. They are defined together so they cannot disagree.
    """

    model: CommissionModel = CommissionModel.NONE
    basis_points: NonNegativeMoney = Field(default=ZERO, le=MAX_BASIS_POINTS)
    """Rate charged on executed notional under
    :attr:`~quantplatform.core.enums.CommissionModel.BASIS_POINTS`, where ``10`` is 0.1%."""

    flat_amount: NonNegativeMoney = ZERO
    """Amount charged once per order under
    :attr:`~quantplatform.core.enums.CommissionModel.FLAT`."""

    fee_asset: AssetCode | None = None
    """Asset a non-zero fee is charged in; ``None`` means the traded symbol's quote asset.

    Every quote-denominated aggregate downstream assumes the quote asset, so a fee named in
    anything else cannot be summed without a conversion the platform does not implement. It
    is rejected at the point of use rather than silently converted — see
    :meth:`resolve_fee_asset`.
    """

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        """Require exactly the parameter the selected model consumes, and no other."""
        if self.model is not CommissionModel.BASIS_POINTS and self.basis_points != ZERO:
            msg = "basis_points is only meaningful for the basis-points fee model"
            raise ValueError(msg)
        if self.model is not CommissionModel.FLAT and self.flat_amount != ZERO:
            msg = "flat_amount is only meaningful for the flat fee model"
            raise ValueError(msg)
        return self

    def fee_for(self, notional: Decimal, *, is_first_fill: bool) -> Decimal:
        """Return the fee for one fill.

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

    def maximum_fee(self, maximum_notional: Decimal) -> Decimal:
        """Return the most this policy can charge across an entire order.

        The number both the broker reserves against and the risk engine funds against. It is
        an upper bound by construction: a basis-point fee on the largest notional the order
        can reach bounds the sum of its per-fill fees, and the flat fee is charged once
        however the order is sliced.

        Args:
            maximum_notional: The largest notional the order can execute at.

        Returns:
            The maximum quote-asset fee the order can incur.
        """
        return self.fee_for(maximum_notional, is_first_fill=True)

    def resolve_fee_asset(self, quote_asset: str) -> str:
        """Return the asset fees are charged in, refusing an unsupported one.

        Args:
            quote_asset: Quote asset of the symbol being traded.

        Returns:
            ``quote_asset``, which is the only asset a non-zero fee may be denominated in.

        Raises:
            UnsupportedFeeAssetError: If this policy names a different asset while capable
                of charging a non-zero fee.
        """
        if self.fee_asset is None or self.fee_asset == quote_asset:
            return quote_asset
        if self.model is CommissionModel.NONE:
            return quote_asset
        raise UnsupportedFeeAssetError(
            "fees must be denominated in the traded symbol's quote asset",
            fee_asset=self.fee_asset,
            quote_asset=quote_asset,
        )


class SlippagePolicy(DomainModel):
    """How far a market execution price moves against the taker.

    Deterministic by construction: the platform offers no probabilistic slippage, because a
    backtest whose fills depend on a random draw cannot be reproduced.
    """

    model: SlippageModel = SlippageModel.OFF
    basis_points: NonNegativeMoney = Field(default=ZERO, le=MAX_BASIS_POINTS)
    """Adverse move applied to a market fill, in basis points: ``10`` is 0.1%."""

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        """Require a rate exactly when the model consumes one."""
        if self.model is SlippageModel.OFF and self.basis_points != ZERO:
            msg = "basis_points must be zero when slippage is off"
            raise ValueError(msg)
        return self

    @property
    def effective_basis_points(self) -> Decimal:
        """Return the rate actually applied, which is zero when the model is off."""
        return ZERO if self.model is SlippageModel.OFF else self.basis_points

    def adjust(self, price: Decimal, side: OrderSide) -> Decimal:
        """Return the execution price after applying the configured adverse move.

        Args:
            price: Matched price before slippage.
            side: Side being executed; slippage always moves against it.

        Returns:
            ``price * (1 + bps)`` for a buy and ``price * (1 - bps)`` for a sell.
        """
        if self.model is SlippageModel.OFF:
            return price
        move = apply_basis_points(price, self.basis_points)
        return price + move if side is OrderSide.BUY else price - move

    def worst_buy_price(self, reference_price: Decimal) -> Decimal:
        """Return the highest price a market buy can execute at under this policy.

        The figure the risk engine's price cap must never fall below.
        """
        return self.adjust(reference_price, OrderSide.BUY)


class ExecutionPolicy(DomainModel):
    """The complete set of execution assumptions both risk and execution must share.

    A composition root builds one of these and injects it into the risk configuration and
    the execution adapter alike, so the two cannot hold different numbers.
    """

    fee: FeePolicy = Field(default_factory=FeePolicy)
    slippage: SlippagePolicy = Field(default_factory=SlippagePolicy)
