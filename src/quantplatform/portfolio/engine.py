"""Deterministic spot portfolio accounting engine (Phase 3A).

:class:`SpotPortfolioEngine` implements the :class:`~quantplatform.core.interfaces.PortfolioEngine`
port. It applies immutable :class:`~quantplatform.core.models.orders.Fill` records — it never
creates them — and maintains balances, cost-basis positions and cumulative realised PnL and
fees, producing a fresh immutable :class:`~quantplatform.core.models.portfolio.PortfolioSnapshot`
after every successful fill.

Supported scope is deliberately narrow: spot markets, one base and one quote asset per
symbol, long-only exposure, average-cost accounting, and fees denominated in the account's
single quote asset. Order matching, simulated execution, commissions, slippage and
persistence are the responsibility of later phases (the Phase 3B simulated broker and
beyond); this engine only consumes fills that already exist.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.enums import ExecutionMode, MarketType, OrderSide
from quantplatform.core.errors import (
    AccountingInvariantError,
    InconsistentSeedStateError,
    InsufficientBalanceError,
    InsufficientPositionError,
    InvalidFillSideError,
    OutOfOrderFillError,
    SymbolMismatchError,
    UnsupportedFeeAssetError,
    UnsupportedMarketTypeError,
)
from quantplatform.core.events import DomainEvent, PortfolioUpdated, PositionChanged
from quantplatform.core.ids import deterministic_uuid
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.orders import Fill
from quantplatform.core.models.portfolio import Balance, PortfolioSnapshot, Position

__all__ = ["FillApplicationResult", "SpotPortfolioEngine"]

_SUPPORTED_SIDES: tuple[OrderSide, ...] = (OrderSide.BUY, OrderSide.SELL)


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Divide inside a high-precision local context, matching :mod:`core.numeric`.

    Average entry price is division-heavy math; using the ambient default context (28
    significant digits) would make results depend on whatever context the caller happens to
    be running under. Localizing precision keeps the computation exact and deterministic.
    """
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        return numerator / denominator


@dataclass(frozen=True, slots=True)
class FillApplicationResult:
    """Outcome of presenting a single fill to :class:`SpotPortfolioEngine`.

    Returned in addition to (not instead of) the :meth:`SpotPortfolioEngine.apply_fill`
    method required by the :class:`~quantplatform.core.interfaces.PortfolioEngine` protocol,
    which returns ``None``. Callers that need the emitted events or the freshly produced
    snapshot should call :meth:`SpotPortfolioEngine.apply` directly instead.
    """

    snapshot: PortfolioSnapshot
    events: tuple[DomainEvent, ...]
    newly_applied: bool
    """``False`` when ``fill_id`` had already been applied; the fill was not reprocessed."""


class SpotPortfolioEngine:
    """Applies spot fills exactly once and maintains balances, positions and PnL.

    **Symbol resolution.** The engine must know each symbol's base asset, quote asset and
    market type. Rather than parsing them from the symbol string, it is constructed with an
    explicit ``symbols`` mapping of already-known
    :class:`~quantplatform.core.models.market.SymbolRules` (the same model
    :class:`~quantplatform.core.interfaces.MarketDataProvider` implementations already
    produce, so no parallel metadata concept is introduced). Only the identity fields
    (``base_asset``, ``quote_asset``, ``market_type``) are used; venue tick/lot/notional
    rules carried on the same object are ignored by this engine.

    **Accounting authority.** :class:`~quantplatform.core.models.portfolio.Balance` is the
    single ledger of truth. ``cash`` on every produced snapshot is always a projection of the
    quote-asset balance, and ``Position.quantity`` is always kept equal to the corresponding
    base-asset balance's ``total`` (``free + locked``) — per the committed contract in
    :mod:`quantplatform.core.models.portfolio`. Reserving base asset against a working sell
    order (moving it from ``free`` to ``locked``) does not close or reduce the economic
    position; only a fill changes owned quantity. Applying a fill only ever spends ``free``
    — never ``locked``. The one way ``locked`` moves at all is
    :meth:`reserve`/:meth:`release`, which this engine exposes for execution adapters as
    :class:`~quantplatform.core.interfaces.SettlementLedger`; both leave
    ``Balance.total`` unchanged and so are invisible to every accounting figure.

    **Seed state (flat-start).** The engine only ever starts flat: positions always start
    empty and cumulative realised PnL and fees always start at zero, with no constructor
    parameter able to override either. Consistently, any ``initial_balances`` entry for an
    asset that is a *base* asset of a known symbol must have zero ``total`` — a nonzero
    seeded base balance with no corresponding position would violate the reconciliation
    invariant above from the moment the engine exists, not just once a fill is applied, so it
    is rejected at construction with :class:`~quantplatform.core.errors.InconsistentSeedStateError`
    rather than left to surface confusingly later. Quote-asset balances (starting cash) are
    unaffected by this rule. Seeding an existing open position — for example to resume a
    process — is out of scope for Phase 3A and requires a future, explicitly seedable
    contract; this engine does not invent one.

    **Chronology.** Fills must be applied in non-decreasing ``executed_at`` order. A fill
    whose ``executed_at`` precedes the engine's last successfully applied fill raises
    :class:`~quantplatform.core.errors.OutOfOrderFillError` and mutates nothing. Fills sharing
    the same ``executed_at`` are accepted; determinism between them comes from each fill
    carrying its own unique ``fill_id`` (event ids are derived from it) and from the engine's
    monotonically increasing internal sequence counter (which snapshot ids are derived from),
    not from the timestamp alone. A repeated ``fill_id`` remains idempotent even if its
    timestamp is now older than the engine's current state — idempotency is checked first, so
    replaying history never fails chronology.

    **Idempotency.** Applying a fill whose ``fill_id`` was already applied leaves every piece
    of state — balances, positions, PnL, fees, timestamps — unchanged and emits no events;
    see :meth:`apply`.

    **Account-level realised PnL and fees.** :attr:`PortfolioSnapshot.realized_pnl` and
    :attr:`PortfolioSnapshot.total_fees` are cumulative across every lifecycle a position has
    ever had, including lifecycles already closed. They are tracked as engine state
    (``_cumulative_realized_pnl`` / ``_cumulative_fees``) rather than summed from
    :meth:`positions` at snapshot time, because a closed lifecycle's contribution would
    otherwise be lost the moment a position reopens and its per-lifecycle counters reset.

    **Unrealized PnL.** This engine never reads a market price. The snapshot it builds
    immediately after applying a fill (embedded in the emitted
    :class:`~quantplatform.core.events.PortfolioUpdated`) marks every open position at its
    own average entry price, so that snapshot's ``unrealized_pnl`` is always zero by
    construction — it exists to make the accounting fields (cash, positions, realised PnL,
    fees) queryable immediately, not to report live marked PnL. A caller that wants a
    genuinely marked view must call :meth:`snapshot` with real prices from a market data
    source.
    """

    def __init__(
        self,
        *,
        quote_asset: str,
        symbols: Mapping[str, SymbolRules],
        execution_mode: ExecutionMode,
        initial_balances: Sequence[Balance] = (),
        source: str = "portfolio_engine",
    ) -> None:
        """Construct an engine seeded with starting balances and known symbols.

        The engine only ever starts flat (see the class docstring): positions and cumulative
        PnL/fees always start empty/zero, and no *base*-asset balance in ``initial_balances``
        may have a nonzero total, since nothing would back a matching position for it.

        Args:
            quote_asset: The single asset every position's cost basis, realised PnL and fees
                are denominated in. A fill for a symbol whose quote asset differs is rejected.
            symbols: Identity metadata (base asset, quote asset, market type) for every
                symbol this engine may be asked to account for.
            execution_mode: Mode recorded on every snapshot this engine produces.
            initial_balances: Starting asset balances, for example seed cash. Every base-asset
                balance among these must have zero total.
            source: Value recorded as ``source`` on every emitted event.

        Raises:
            InconsistentSeedStateError: If a base-asset balance has a nonzero total.
        """
        self._quote_asset = quote_asset
        self._symbols = dict(symbols)
        self._execution_mode = execution_mode
        self._source = source
        self._balances: dict[str, Balance] = {
            balance.asset: balance for balance in initial_balances
        }
        self._positions: dict[str, Position] = {}
        self._applied_fill_ids: set[UUID] = set()
        self._cumulative_realized_pnl: Decimal = ZERO
        self._cumulative_fees: Decimal = ZERO
        self._sequence: int = 0
        self._last_executed_at: datetime | None = None

        base_assets = {rules.base_asset for rules in self._symbols.values()}
        for balance in initial_balances:
            if balance.asset in base_assets and balance.total != ZERO:
                raise InconsistentSeedStateError(
                    "this engine starts flat: a base-asset balance must have zero total "
                    "at construction, since no position exists yet to back it",
                    asset=balance.asset,
                    total=str(balance.total),
                )

    # --- PortfolioEngine protocol --------------------------------------------------------

    def has_applied(self, fill_id: UUID) -> bool:
        """Return whether a fill has already been incorporated into portfolio state."""
        return fill_id in self._applied_fill_ids

    def apply_fill(self, fill: Fill) -> None:
        """Incorporate a fill into balances, positions and realised PnL.

        Satisfies the :class:`~quantplatform.core.interfaces.PortfolioEngine` protocol,
        which returns ``None``. Use :meth:`apply` for the emitted events and the resulting
        snapshot.
        """
        self.apply(fill)

    def positions(self) -> Sequence[Position]:
        """Return the current positions, ordered deterministically by symbol."""
        return tuple(self._positions[symbol] for symbol in sorted(self._positions))

    def balances(self) -> Sequence[Balance]:
        """Return the current asset balances, ordered deterministically by asset."""
        return tuple(self._balances[asset] for asset in sorted(self._balances))

    def snapshot(
        self,
        *,
        as_of: datetime,
        mark_prices: Mapping[str, Decimal],
    ) -> PortfolioSnapshot:
        """Produce an immutable marked snapshot of the account using caller-supplied prices."""
        return self._build_snapshot(as_of=as_of, mark_prices=dict(mark_prices))

    # --- SettlementLedger protocol --------------------------------------------------------

    def reserve(self, *, asset: str, amount: Decimal, at: datetime) -> None:
        """Move ``amount`` of ``asset`` from available to reserved.

        Implements :class:`~quantplatform.core.interfaces.SettlementLedger` so an
        execution adapter can hold funds against a working order without owning balances.
        This is deliberately **not** an accounting operation: ``Balance.total`` is unchanged,
        so the ``Position.quantity == Balance.total`` reconciliation and every PnL and
        cost-basis figure are untouched. Reserving changes availability, never ownership.

        Args:
            asset: Asset to reserve against.
            amount: Non-negative amount to move from ``free`` to ``locked``.
            at: Instant recorded on the updated balance.

        Raises:
            InsufficientBalanceError: If ``free`` does not cover ``amount``.
        """
        if amount == ZERO:
            return
        balance = self._get_balance(asset, at=at)
        if balance.free < amount:
            raise InsufficientBalanceError(
                "insufficient available balance to reserve",
                asset=asset,
                required=str(amount),
                available=str(balance.free),
            )
        self._balances[asset] = Balance(
            asset=asset,
            free=balance.free - amount,
            locked=balance.locked + amount,
            updated_at=at,
        )

    def release(self, *, asset: str, amount: Decimal, at: datetime) -> None:
        """Move ``amount`` of ``asset`` from reserved back to available.

        The exact inverse of :meth:`reserve`, and equally not an accounting operation.

        Args:
            asset: Asset whose reservation is being released.
            amount: Non-negative amount to move from ``locked`` back to ``free``.
            at: Instant recorded on the updated balance.

        Raises:
            InsufficientBalanceError: If ``locked`` does not cover ``amount``.
        """
        if amount == ZERO:
            return
        balance = self._get_balance(asset, at=at)
        if balance.locked < amount:
            raise InsufficientBalanceError(
                "insufficient reserved balance to release",
                asset=asset,
                required=str(amount),
                available=str(balance.locked),
            )
        self._balances[asset] = Balance(
            asset=asset,
            free=balance.free + amount,
            locked=balance.locked - amount,
            updated_at=at,
        )

    def reserved(self, asset: str) -> Decimal:
        """Return the currently reserved amount of ``asset``, zero when it is unknown."""
        balance = self._balances.get(asset)
        return balance.locked if balance is not None else ZERO

    def balance(self, asset: str) -> Balance | None:
        """Return the current balance for ``asset``, or ``None`` when it is unknown."""
        return self._balances.get(asset)

    # --- Extended API: explicit result with events ---------------------------------------

    def apply(self, fill: Fill) -> FillApplicationResult:
        """Apply a fill and return the resulting snapshot together with its events.

        A fill whose ``fill_id`` was already applied is a no-op: state is left byte-for-byte
        unchanged, no events are emitted, and ``newly_applied`` is ``False``. This is the
        chosen idempotency behaviour (returning the unchanged current state) rather than
        raising, matching the protocol's requirement that repeated application must not
        double-count a fill.

        All preconditions are validated, and every new balance and position object is
        constructed, before any engine state is mutated: if validation fails, this engine's
        balances, positions, PnL, fees, applied-fill tracking and event history are left
        exactly as they were before the call.

        Args:
            fill: Executed trade to apply.

        Returns:
            The post-application snapshot, the events raised by this call (empty for a
            repeated fill), and whether the fill was newly applied.

        Raises:
            OutOfOrderFillError: If ``fill.executed_at`` precedes the engine's last
                successfully applied fill. Not raised for a repeated ``fill_id``: idempotency
                is checked first, so replaying history is always safe.
            SymbolMismatchError: If the fill's symbol is unknown to this engine, or its
                quote asset differs from the engine's configured quote asset.
            UnsupportedMarketTypeError: If the symbol's market type is not spot.
            UnsupportedFeeAssetError: If the fill carries a non-zero fee in an asset other
                than the engine's quote asset.
            InsufficientBalanceError: If the quote balance (buy) or base balance (sell) is
                insufficient to cover the fill.
            InsufficientPositionError: If a sell fill exceeds the open position quantity.
            InvalidFillSideError: If the fill's side is neither buy nor sell.
            AccountingInvariantError: If, after computing the new state, the position
                quantity would no longer reconcile with the base-asset balance.
        """
        if self.has_applied(fill.fill_id):
            assert self._last_executed_at is not None  # noqa: S101 - a prior apply() set it
            return FillApplicationResult(
                snapshot=self._build_snapshot(
                    as_of=self._last_executed_at, mark_prices=self._cost_basis_mark_prices()
                ),
                events=(),
                newly_applied=False,
            )

        if self._last_executed_at is not None and fill.executed_at < self._last_executed_at:
            raise OutOfOrderFillError(
                "fill executed_at precedes the engine's last applied fill",
                fill_executed_at=fill.executed_at.isoformat(),
                last_applied_executed_at=self._last_executed_at.isoformat(),
            )

        metadata = self._resolve_symbol(fill.symbol)
        quote_fee = self._validate_fee(fill)
        previous_position = self._positions.get(fill.symbol)

        if fill.side not in _SUPPORTED_SIDES:
            raise InvalidFillSideError(
                "fill side is neither buy nor sell", symbol=fill.symbol, side=str(fill.side)
            )
        if fill.side is OrderSide.BUY:
            new_base, new_quote, new_position = self._compute_buy(
                fill, metadata, quote_fee, previous_position
            )
            realized_delta = ZERO
        else:
            new_base, new_quote, new_position, realized_delta = self._compute_sell(
                fill, metadata, quote_fee, previous_position
            )

        if new_position.quantity != new_base.total:
            raise AccountingInvariantError(
                "position quantity diverged from base balance after applying fill",
                symbol=fill.symbol,
                position_quantity=str(new_position.quantity),
                base_total=str(new_base.total),
            )

        self._balances[new_base.asset] = new_base
        self._balances[new_quote.asset] = new_quote
        self._positions[fill.symbol] = new_position
        self._applied_fill_ids.add(fill.fill_id)
        self._cumulative_realized_pnl += realized_delta
        self._cumulative_fees += quote_fee
        self._sequence += 1
        self._last_executed_at = fill.executed_at

        events = self._build_events(fill, previous_position, new_position)
        snapshot = self._build_snapshot(
            as_of=fill.executed_at, mark_prices=self._cost_basis_mark_prices()
        )
        return FillApplicationResult(snapshot=snapshot, events=events, newly_applied=True)

    # --- Symbol and fee validation ---------------------------------------------------------

    def _resolve_symbol(self, symbol: str) -> SymbolRules:
        """Return the known identity metadata for ``symbol``, or raise."""
        metadata = self._symbols.get(symbol)
        if metadata is None:
            raise SymbolMismatchError(
                "no symbol metadata is registered with this engine for this fill",
                symbol=symbol,
            )
        if metadata.quote_asset != self._quote_asset:
            raise SymbolMismatchError(
                "symbol quote asset does not match the portfolio quote asset",
                symbol=symbol,
                symbol_quote_asset=metadata.quote_asset,
                portfolio_quote_asset=self._quote_asset,
            )
        if metadata.market_type is not MarketType.SPOT:
            raise UnsupportedMarketTypeError(
                "phase 3a accounting supports only the spot market type",
                symbol=symbol,
                market_type=str(metadata.market_type),
            )
        return metadata

    def _validate_fee(self, fill: Fill) -> Decimal:
        """Return the quote-denominated fee to apply, or raise for an unsupported asset.

        A fee denominated in the quote asset is always accepted. A fee in any other asset is
        accepted only when it is exactly zero, since the ``Fill`` contract requires a
        ``fee_asset`` even when there is nothing to charge; a non-zero fee in any other asset
        cannot be folded into the quote-denominated fee aggregates without a conversion this
        phase does not implement, so it is rejected.
        """
        if fill.fee_asset == self._quote_asset:
            return fill.fee
        if fill.fee > ZERO:
            raise UnsupportedFeeAssetError(
                "a non-zero fee must be denominated in the portfolio quote asset",
                fee_asset=fill.fee_asset,
                quote_asset=self._quote_asset,
            )
        return ZERO

    def _get_balance(self, asset: str, *, at: datetime) -> Balance:
        """Return the current balance for ``asset``, or a fresh zero balance if none exists."""
        existing = self._balances.get(asset)
        if existing is not None:
            return existing
        return Balance(asset=asset, free=ZERO, locked=ZERO, updated_at=at)

    # --- Buy / sell accounting --------------------------------------------------------------

    def _compute_buy(
        self,
        fill: Fill,
        metadata: SymbolRules,
        quote_fee: Decimal,
        previous: Position | None,
    ) -> tuple[Balance, Balance, Position]:
        """Compute the resulting base balance, quote balance and position for a buy fill.

        Cost basis rule: quote-asset buy fees are included in cost basis, so the weighted
        average entry price is ``(prior cost basis + executed notional + quote fee) /
        new quantity``.
        """
        executed_notional = fill.notional
        quote_balance = self._get_balance(metadata.quote_asset, at=fill.executed_at)
        required = executed_notional + quote_fee
        if quote_balance.free < required:
            raise InsufficientBalanceError(
                "insufficient quote balance for buy fill",
                asset=metadata.quote_asset,
                required=str(required),
                available=str(quote_balance.free),
            )
        base_balance = self._get_balance(metadata.base_asset, at=fill.executed_at)

        new_base_balance = Balance(
            asset=base_balance.asset,
            free=base_balance.free + fill.quantity,
            locked=base_balance.locked,
            updated_at=fill.executed_at,
        )
        new_quote_balance = Balance(
            asset=quote_balance.asset,
            free=quote_balance.free - required,
            locked=quote_balance.locked,
            updated_at=fill.executed_at,
        )

        is_new_lifecycle = previous is None or not previous.is_open
        if is_new_lifecycle:
            new_quantity = fill.quantity
            new_avg_entry = _divide(required, fill.quantity)
            realized_pnl = ZERO
            fees_paid = quote_fee
            opened_at = fill.executed_at
        else:
            assert previous is not None  # noqa: S101 - narrows for mypy; guarded by is_open above
            assert previous.avg_entry_price is not None  # noqa: S101
            assert previous.opened_at is not None  # noqa: S101
            new_quantity = previous.quantity + fill.quantity
            new_avg_entry = _divide(previous.cost_basis + required, new_quantity)
            realized_pnl = previous.realized_pnl
            fees_paid = previous.fees_paid + quote_fee
            opened_at = previous.opened_at

        new_position = Position(
            symbol=fill.symbol,
            base_asset=metadata.base_asset,
            quote_asset=metadata.quote_asset,
            quantity=new_quantity,
            avg_entry_price=new_avg_entry,
            realized_pnl=realized_pnl,
            fees_paid=fees_paid,
            opened_at=opened_at,
            updated_at=fill.executed_at,
        )
        return new_base_balance, new_quote_balance, new_position

    def _compute_sell(
        self,
        fill: Fill,
        metadata: SymbolRules,
        quote_fee: Decimal,
        previous: Position | None,
    ) -> tuple[Balance, Balance, Position, Decimal]:
        """Compute the resulting balances, position and realised-PnL delta for a sell fill."""
        if previous is None or not previous.is_open:
            raise InsufficientPositionError(
                "no open position to sell",
                symbol=fill.symbol,
                requested=str(fill.quantity),
                available="0",
            )
        if previous.quantity < fill.quantity:
            raise InsufficientPositionError(
                "sell quantity exceeds open position quantity",
                symbol=fill.symbol,
                requested=str(fill.quantity),
                available=str(previous.quantity),
            )
        base_balance = self._get_balance(metadata.base_asset, at=fill.executed_at)
        if base_balance.free < fill.quantity:
            raise InsufficientBalanceError(
                "insufficient base balance for sell fill",
                asset=metadata.base_asset,
                required=str(fill.quantity),
                available=str(base_balance.free),
            )
        quote_balance = self._get_balance(metadata.quote_asset, at=fill.executed_at)

        assert previous.avg_entry_price is not None  # noqa: S101 - guarded by is_open above
        executed_notional = fill.notional
        cost_released = previous.avg_entry_price * fill.quantity
        realized_after_fee = executed_notional - cost_released - quote_fee

        new_base_balance = Balance(
            asset=base_balance.asset,
            free=base_balance.free - fill.quantity,
            locked=base_balance.locked,
            updated_at=fill.executed_at,
        )
        new_quote_balance = Balance(
            asset=quote_balance.asset,
            free=quote_balance.free + executed_notional - quote_fee,
            locked=quote_balance.locked,
            updated_at=fill.executed_at,
        )

        new_quantity = previous.quantity - fill.quantity
        new_avg_entry = previous.avg_entry_price if new_quantity > ZERO else None

        new_position = Position(
            symbol=fill.symbol,
            base_asset=metadata.base_asset,
            quote_asset=metadata.quote_asset,
            quantity=new_quantity,
            avg_entry_price=new_avg_entry,
            realized_pnl=previous.realized_pnl + realized_after_fee,
            fees_paid=previous.fees_paid + quote_fee,
            opened_at=previous.opened_at,
            updated_at=fill.executed_at,
        )
        return new_base_balance, new_quote_balance, new_position, realized_after_fee

    # --- Events and snapshots ---------------------------------------------------------------

    def _build_events(
        self,
        fill: Fill,
        previous: Position | None,
        position: Position,
    ) -> tuple[DomainEvent, ...]:
        """Build the ``PositionChanged`` and ``PortfolioUpdated`` events for a new fill.

        Event ids are derived deterministically from the fill id and the event type, per the
        platform's identifier-derivation convention, rather than drawn at random.
        """
        position_changed = PositionChanged(
            event_id=deterministic_uuid("event", "position_changed", str(fill.fill_id)),
            occurred_at=fill.executed_at,
            source=self._source,
            correlation_id=None,
            previous_position=previous,
            position=position,
            fill=fill,
        )
        portfolio_updated = PortfolioUpdated(
            event_id=deterministic_uuid("event", "portfolio_updated", str(fill.fill_id)),
            occurred_at=fill.executed_at,
            source=self._source,
            correlation_id=None,
            snapshot=self._build_snapshot(
                as_of=fill.executed_at, mark_prices=self._cost_basis_mark_prices()
            ),
        )
        return (position_changed, portfolio_updated)

    def _cost_basis_mark_prices(self) -> dict[str, Decimal]:
        """Return each open position marked at its own average entry price.

        Used only for snapshots this engine builds internally (for example, the one embedded
        in ``PortfolioUpdated``), since the engine has no market price source of its own; see
        the class docstring's note on unrealised PnL.
        """
        return {
            position.symbol: position.avg_entry_price
            for position in self._positions.values()
            if position.is_open and position.avg_entry_price is not None
        }

    def _build_snapshot(
        self,
        *,
        as_of: datetime,
        mark_prices: dict[str, Decimal],
    ) -> PortfolioSnapshot:
        """Build a snapshot from current engine state, deterministic in the fill sequence."""
        quote_balance = self._balances.get(self._quote_asset)
        cash = quote_balance.total if quote_balance is not None else ZERO
        return PortfolioSnapshot(
            snapshot_id=deterministic_uuid(
                "portfolio_snapshot", str(self._sequence), as_of.isoformat()
            ),
            taken_at=as_of,
            execution_mode=self._execution_mode,
            quote_asset=self._quote_asset,
            cash=cash,
            balances=tuple(self._balances[asset] for asset in sorted(self._balances)),
            positions=tuple(self._positions[symbol] for symbol in sorted(self._positions)),
            mark_prices=mark_prices,
            realized_pnl=self._cumulative_realized_pnl,
            total_fees=self._cumulative_fees,
        )
