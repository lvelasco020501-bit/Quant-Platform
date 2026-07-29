"""Portfolio accounting domain models."""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, computed_field, model_validator

from quantplatform.core.constants import ZERO
from quantplatform.core.enums import ExecutionMode, PositionState
from quantplatform.core.models.base import (
    AssetCode,
    DomainModel,
    Symbol,
    UtcDatetime,
)
from quantplatform.core.numeric import (
    Fee,
    Money,
    NonNegativeMoney,
    NonNegativeQuantity,
    Price,
)

__all__ = ["Balance", "PortfolioSnapshot", "Position"]


class Balance(DomainModel):
    """Holding of a single asset, split between available and reserved amounts."""

    asset: AssetCode
    free: NonNegativeMoney
    locked: NonNegativeMoney
    updated_at: UtcDatetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> Decimal:
        """Return the sum of the available and reserved amounts."""
        return self.free + self.locked


class Position(DomainModel):
    """Exposure to a single instrument.

    Quantities are non-negative because the platform is spot-only and short selling is
    prohibited; the constraint is enforced by the type rather than by convention.
    """

    symbol: Symbol
    base_asset: AssetCode
    quote_asset: AssetCode
    quantity: NonNegativeQuantity
    avg_entry_price: Price | None = None
    realized_pnl: Money
    fees_paid: Fee
    opened_at: UtcDatetime | None = None
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _validate_position(self) -> Self:
        """Check that entry data is present exactly when the position is open."""
        if self.symbol != f"{self.base_asset}/{self.quote_asset}":
            msg = "symbol must match base and quote assets"
            raise ValueError(msg)
        is_open = self.quantity > ZERO
        if is_open and self.avg_entry_price is None:
            msg = "an open position requires an avg_entry_price"
            raise ValueError(msg)
        if is_open and self.opened_at is None:
            msg = "an open position requires an opened_at timestamp"
            raise ValueError(msg)
        if not is_open and self.avg_entry_price is not None:
            msg = "a flat position must not carry an avg_entry_price"
            raise ValueError(msg)
        return self

    @property
    def is_open(self) -> bool:
        """Return whether any exposure remains."""
        return self.quantity > ZERO

    @property
    def state(self) -> PositionState:
        """Return the coarse position state exposed to strategies."""
        return PositionState.LONG if self.is_open else PositionState.FLAT

    def market_value(self, mark_price: Decimal) -> Decimal:
        """Return the quote-asset value of the exposure at ``mark_price``.

        Args:
            mark_price: Price used to mark the position.

        Returns:
            ``quantity * mark_price``.
        """
        return self.quantity * mark_price

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        """Return the open profit or loss at ``mark_price``.

        Args:
            mark_price: Price used to mark the position.

        Returns:
            Zero when flat, otherwise ``(mark_price - avg_entry_price) * quantity``.
        """
        if not self.is_open or self.avg_entry_price is None:
            return ZERO
        return (mark_price - self.avg_entry_price) * self.quantity


class PortfolioSnapshot(DomainModel):
    """An immutable, marked view of the account at a single instant.

    ``equity`` is derived rather than stored, so the invariant
    ``equity == cash + marked position value`` holds by construction and cannot drift.
    """

    snapshot_id: UUID
    taken_at: UtcDatetime
    execution_mode: ExecutionMode
    quote_asset: AssetCode
    cash: NonNegativeMoney
    balances: tuple[Balance, ...] = ()
    positions: tuple[Position, ...] = ()
    mark_prices: dict[Symbol, Price] = Field(default_factory=dict)
    realized_pnl: Money
    total_fees: Fee

    @model_validator(mode="after")
    def _validate_snapshot(self) -> Self:
        """Check uniqueness of assets and symbols and the presence of marks."""
        assets = [balance.asset for balance in self.balances]
        if len(set(assets)) != len(assets):
            msg = "each asset may appear at most once in balances"
            raise ValueError(msg)
        symbols = [position.symbol for position in self.positions]
        if len(set(symbols)) != len(symbols):
            msg = "each symbol may appear at most once in positions"
            raise ValueError(msg)
        for position in self.positions:
            if position.is_open and position.symbol not in self.mark_prices:
                msg = f"a mark price is required to value open position {position.symbol!r}"
                raise ValueError(msg)
            if position.is_open and position.quote_asset != self.quote_asset:
                msg = "open positions must be quoted in the snapshot quote asset"
                raise ValueError(msg)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def positions_value(self) -> Decimal:
        """Return the marked value of every open position in the quote asset."""
        return sum(
            (
                position.market_value(self.mark_prices[position.symbol])
                for position in self.positions
                if position.is_open
            ),
            start=ZERO,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unrealized_pnl(self) -> Decimal:
        """Return the aggregate open profit or loss in the quote asset."""
        return sum(
            (
                position.unrealized_pnl(self.mark_prices[position.symbol])
                for position in self.positions
                if position.is_open
            ),
            start=ZERO,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def equity(self) -> Decimal:
        """Return total account value: cash plus the marked value of open positions."""
        return self.cash + self.positions_value

    @property
    def open_position_count(self) -> int:
        """Return the number of instruments with non-zero exposure."""
        return sum(1 for position in self.positions if position.is_open)
