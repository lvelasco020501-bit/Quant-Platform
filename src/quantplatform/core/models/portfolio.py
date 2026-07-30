"""Portfolio accounting domain models.

There is exactly one ledger of truth for what the account owns: :class:`Balance`.
Everything else in this module is a view derived from it:

* :attr:`PortfolioSnapshot.cash` is a **convenience projection** of the quote asset's
  :attr:`Balance.total` at snapshot time, not an independent record of how much cash the
  account holds. Nothing may write to it except by copying that balance.
* :class:`Position` is a **cost-basis overlay** on top of a base-asset :class:`Balance`. It
  exists to carry information — average entry price, realized PnL — that a bare balance
  cannot express, not to hold a second, competing count of how much of the asset is owned.
  For a spot symbol, ``Position.quantity`` must equal the corresponding base-asset
  ``Balance.total``; maintaining that reconciliation after every fill is the
  responsibility of the Phase 3 portfolio engine and is not enforced by this module, since
  it requires observing fills and multiple balances together, which a single model cannot
  do on its own.

Aggregate fee fields (:attr:`Position.fees_paid`, :attr:`PortfolioSnapshot.total_fees`, and
:attr:`~quantplatform.core.models.orders.Order.fees_paid`) are always denominated in the
portfolio's quote asset. A :class:`~quantplatform.core.models.orders.Fill` may be executed
with a fee in any asset (:attr:`~quantplatform.core.models.orders.Fill.fee_asset`), but
converting or rejecting a non-quote-asset fee before folding it into one of these aggregates
is a Phase 3 responsibility; no component may silently sum fees across currencies.
"""

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
    """Holding of a single asset. The authoritative record of what the account owns.

    Every other portfolio-level view of holdings — :attr:`PortfolioSnapshot.cash`, a
    :class:`Position`'s base-asset exposure — is a derived projection of a ``Balance`` and
    must ultimately trace back to one, never carry an independently maintained count.
    """

    asset: AssetCode
    free: NonNegativeMoney
    """Available balance: not reserved against any order, spendable immediately."""

    locked: NonNegativeMoney
    """Reserved balance: held against working orders, not currently spendable."""

    updated_at: UtcDatetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> Decimal:
        """Return the sum of the available and reserved amounts."""
        return self.free + self.locked


class Position(DomainModel):
    """Cost-basis overlay for exposure to a single instrument.

    Quantities are non-negative because the platform is spot-only and short selling is
    prohibited; the constraint is enforced by the type rather than by convention.

    A ``Position`` is not a second ledger: ``quantity`` must equal the corresponding
    base-asset :class:`Balance`'s ``total`` (maintained by the Phase 3 portfolio engine,
    not by this model). What a ``Position`` adds beyond a balance is cost basis — average
    entry price and realized PnL — that a bare asset holding cannot express.

    **Realized-PnL lifecycle.** ``realized_pnl`` is cumulative for the current *lifecycle*
    of the position, not for the instrument's entire history:

    * A lifecycle begins when the position opens from flat (``quantity`` goes from zero to
      positive) with ``realized_pnl`` starting at zero and a fresh ``avg_entry_price`` and
      ``opened_at``.
    * Every closing trade during that lifecycle adds to ``realized_pnl``; it never resets
      mid-lifecycle and a past contribution is never altered, only added to.
    * When ``quantity`` reaches zero, the lifecycle ends. The closed position's final
      snapshot — including its accumulated ``realized_pnl`` — is immutable, exactly like
      every other record in this domain.
    * If the same instrument is opened again later, that is a **new lifecycle**: the
      portfolio engine constructs a new ``Position`` with ``realized_pnl`` reset to zero, a
      new ``avg_entry_price`` and a new ``opened_at``. Nothing carries over from the closed
      lifecycle's snapshot except its historical record.

    This module does not implement that engine; it only shapes the model so the lifecycle
    above is representable and so a closed snapshot cannot be mistaken for an active one
    (enforced by :meth:`_validate_position` below: a flat position carries no entry price).
    """

    symbol: Symbol
    base_asset: AssetCode
    quote_asset: AssetCode
    quantity: NonNegativeQuantity
    avg_entry_price: Price | None = None
    realized_pnl: Money
    """Cumulative realized PnL for the current lifecycle only; see the class docstring."""

    fees_paid: Fee
    """Cumulative fees for the current lifecycle, denominated in the quote asset.

    A fee incurred in a different asset must be converted (or rejected) before being
    folded in here; that conversion is a Phase 3 responsibility, not performed by this
    model.
    """

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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost_basis(self) -> Decimal:
        """Return what the current exposure cost to acquire, in the quote asset.

        Returns:
            Zero when flat, otherwise ``avg_entry_price * quantity``. Unlike
            :meth:`market_value`, this needs no current price: it reports what was paid,
            not what the position is worth now.
        """
        if not self.is_open or self.avg_entry_price is None:
            return ZERO
        return self.avg_entry_price * self.quantity

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

    ``cash`` is a **convenience projection**, not an independent ledger: it must equal the
    ``total`` of whichever :class:`Balance` in :attr:`balances` carries :attr:`quote_asset`.
    Phase 3 must always construct ``cash`` by copying that balance, never by tracking cash
    separately. This model enforces the cross-check whenever that balance entry is present
    (see :meth:`_validate_snapshot`); it cannot require the entry to be present, since
    ``balances`` may legitimately be a partial or empty view.

    ``realized_pnl`` and ``total_fees`` are account-level aggregates over
    :attr:`positions`; both are quote-asset denominated, matching every per-position
    aggregate (see :class:`Position`). Nothing here sums a differently denominated fee
    into either field — that conversion or rejection is a Phase 3 responsibility.
    """

    snapshot_id: UUID
    taken_at: UtcDatetime
    execution_mode: ExecutionMode
    quote_asset: AssetCode
    cash: NonNegativeMoney
    """Convenience projection of the quote-asset :class:`Balance`'s ``total``; see above."""

    balances: tuple[Balance, ...] = ()
    positions: tuple[Position, ...] = ()
    mark_prices: dict[Symbol, Price] = Field(default_factory=dict)
    realized_pnl: Money
    total_fees: Fee

    @model_validator(mode="after")
    def _validate_snapshot(self) -> Self:
        """Check uniqueness of assets and symbols, mark coverage, and the cash projection."""
        balances_by_asset = {balance.asset: balance for balance in self.balances}
        if len(balances_by_asset) != len(self.balances):
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
        quote_balance = balances_by_asset.get(self.quote_asset)
        if quote_balance is not None and quote_balance.total != self.cash:
            msg = (
                "cash must equal the total of the quote-asset balance; cash is a "
                "projection of that balance, not an independently tracked ledger"
            )
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gross_exposure(self) -> Decimal:
        """Return the marked value of every open position, in the quote asset.

        Equal to :attr:`positions_value` by definition — spot-only, unleveraged holdings
        have no notion of gross exposure distinct from marked position value. This
        property exists as the named, discoverable entry point for "how exposed is this
        account", so a caller does not need to know that identity holds. Leverage is
        deliberately not defined anywhere in this model: leverage is prohibited for the
        spot-only accounts this platform manages.
        """
        return self.positions_value

    @property
    def open_position_count(self) -> int:
        """Return the number of instruments with non-zero exposure."""
        return sum(1 for position in self.positions if position.is_open)
