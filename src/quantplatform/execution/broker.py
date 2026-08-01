"""Deterministic simulated broker (Phase 3B).

:class:`SimulatedBroker` turns risk-approved orders into fills against closed market bars.
It owns the order book — working orders, their lifecycle, their reservations and the fills
they produce — and nothing else. Balances, positions, cost basis and PnL belong exclusively
to the portfolio engine, which this module reaches only through the single narrow
:class:`~quantplatform.core.interfaces.SettlementLedger` port.

Every rule here is deterministic. Replaying the same approved orders against the same bars
always produces the same fills, the same prices, the same fees, the same identifiers and the
same event sequence, because nothing in this module reads a clock or draws a random number:
prices come from the bar, timestamps come from the bar, and identifiers are derived.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from quantplatform.core.constants import ZERO
from quantplatform.core.enums import (
    ExecutionMode,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from quantplatform.core.errors import (
    InsufficientBalanceError,
    InsufficientReservationError,
    MatchingError,
    OrderNotFoundError,
    OrderStateTransitionError,
    UnsupportedMarketTypeError,
    UnsupportedOrderTypeError,
    UnsupportedTimeInForceError,
)
from quantplatform.core.events import DomainEvent, FillReceived, OrderStatusChanged
from quantplatform.core.ids import deterministic_uuid
from quantplatform.core.interfaces import SettlementLedger
from quantplatform.core.models.market import MarketBar, SymbolRules
from quantplatform.core.models.orders import ApprovedOrder, Fill, Order
from quantplatform.execution.config import ExecutionConfig

__all__ = [
    "CancellationResult",
    "ExecutionResult",
    "SimulatedBroker",
    "SubmissionResult",
]

_SUPPORTED_ORDER_TYPES: frozenset[OrderType] = frozenset({OrderType.MARKET, OrderType.LIMIT})
_SUPPORTED_TIME_IN_FORCE: frozenset[TimeInForce] = frozenset({TimeInForce.GTC, TimeInForce.IOC})


@dataclass(frozen=True, slots=True)
class _Reservation:
    """Funds this broker is holding against one working order."""

    asset: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Outcome of submitting one approved order."""

    order: Order
    events: tuple[DomainEvent, ...]
    reservation_asset: str | None
    reservation_delta: Decimal
    """Amount newly moved into reserve; zero for a rejection or an unreserved market buy."""

    accepted: bool
    """``False`` when the venue refused the order, in which case nothing was reserved."""

    newly_submitted: bool
    """``False`` when this ``client_order_id`` was already known; nothing was re-submitted."""


@dataclass(frozen=True, slots=True)
class CancellationResult:
    """Outcome of cancelling one working order."""

    order: Order
    events: tuple[DomainEvent, ...]
    reservation_asset: str | None
    released: Decimal
    newly_canceled: bool
    """``False`` when the order had already reached a terminal state; nothing changed."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Everything one closed bar produced."""

    fills: tuple[Fill, ...]
    orders: tuple[Order, ...]
    """Each order touched by this bar, in the order it was matched."""

    events: tuple[DomainEvent, ...]
    released: Mapping[str, Decimal] = field(default_factory=dict)
    """Reservation released per asset while processing this bar."""

    executed: bool = False
    """``True`` when at least one fill was generated."""

    already_processed: bool = False
    """``True`` when this bar had been processed before and was skipped as a no-op."""


class SimulatedBroker:
    """Matches approved orders against closed bars and hands the resulting fills onward.

    **Boundaries.** The broker never touches balances, positions, PnL or snapshots. It moves
    funds only between the available and reserved halves of a balance — which changes
    availability, not ownership — and it hands every fill it generates to the portfolio,
    which remains the sole accounting authority. It emits
    :class:`~quantplatform.core.events.OrderStatusChanged` and
    :class:`~quantplatform.core.events.FillReceived`; it never emits ``PortfolioUpdated``,
    because it is not the component that updates the portfolio.

    **Time.** The broker reads no clock. It is constructed at an explicit ``started_at``
    instant and advances only when a bar is processed, taking that bar's ``close_time`` as
    the new current instant. Every fill, order timestamp and event timestamp comes from
    there, which is what makes a replay reproduce timestamps exactly.

    **Matching.** Only closed bars are matched, and only by fixed rules — see
    :meth:`process_bar`. A market order fills at the bar's open; a limit order fills at its
    own limit price when the bar traded through it. There is no probabilistic matching, no
    intrabar path reconstruction and no queue-position model: those need order-book data this
    platform does not have, and inventing them would produce confident-looking fills that
    nothing justifies.

    **Idempotency and resume.** Submitting a ``client_order_id`` that is already known
    returns the existing order without creating a second one, matching the
    :class:`~quantplatform.core.interfaces.ExecutionAdapter` contract. Matching is recorded
    **per order per bar**, not per bar alone: if settlement of one order fails partway
    through a bar, the orders that already settled are marked against that bar and are never
    matched against it again, so retrying the bar resumes at the order that failed instead of
    re-executing the ones that succeeded. Re-processing a fully completed bar is a no-op.
    """

    def __init__(
        self,
        *,
        symbols: Mapping[str, SymbolRules],
        portfolio: SettlementLedger,
        execution_mode: ExecutionMode,
        started_at: datetime,
        config: ExecutionConfig | None = None,
        venue: str = "simulated",
        source: str = "simulated_broker",
    ) -> None:
        """Construct a broker over a fixed set of symbols and a portfolio to settle into.

        Args:
            symbols: Venue rules per symbol, used for identity, lot sizes and minimums.
            portfolio: The accounting authority fills are handed to, and the ledger
                reservations move funds within.
            execution_mode: Mode stamped on every order and fill this broker produces.
            started_at: The broker's initial current instant, before any bar is processed.
            config: Deterministic slippage, commission and fill-ratio assumptions.
            venue: Identifier recorded as the simulated venue.
            source: Value recorded as ``source`` on every emitted event.
        """
        self._symbols = dict(symbols)
        self._portfolio = portfolio
        self._execution_mode = execution_mode
        self._config = config if config is not None else ExecutionConfig()
        self._venue = venue
        self._source = source

        self._now = started_at
        self._orders: dict[str, Order] = {}
        self._approved: dict[str, ApprovedOrder] = {}
        self._reservations: dict[str, _Reservation] = {}
        self._fills: list[Fill] = []
        self._filled_notional: dict[str, Decimal] = {}
        self._submission_sequence: list[str] = []
        self._status_sequence: dict[str, int] = {}
        self._processed_bars: set[tuple[str, datetime]] = set()
        self._matched_against: set[tuple[str, datetime]] = set()
        """``(client_order_id, bar open time)`` pairs already settled, so a bar retried after
        a mid-bar failure resumes rather than re-executing what already succeeded."""

    # --- Introspection --------------------------------------------------------------------

    @property
    def mode(self) -> ExecutionMode:
        """Return the execution mode this broker simulates."""
        return self._execution_mode

    @property
    def venue(self) -> str:
        """Return the simulated venue identifier."""
        return self._venue

    @property
    def now(self) -> datetime:
        """Return the broker's current instant, advanced only by processed bars."""
        return self._now

    def get_order(self, client_order_id: str) -> Order:
        """Return a known order.

        Raises:
            OrderNotFoundError: If no order was submitted under this id.
        """
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderNotFoundError("no order is known under this client order id")
        return order

    def open_orders(self, *, symbol: str | None = None) -> Sequence[Order]:
        """Return every still-working order, in submission order."""
        return tuple(
            self._orders[cid]
            for cid in self._submission_sequence
            if self._orders[cid].is_open and (symbol is None or self._orders[cid].symbol == symbol)
        )

    def fills(self, *, symbol: str | None = None) -> Sequence[Fill]:
        """Return every fill this broker has generated, in generation order."""
        return tuple(fill for fill in self._fills if symbol is None or fill.symbol == symbol)

    def reserved_for(self, client_order_id: str) -> Decimal:
        """Return what this broker still holds in reserve against one order."""
        reservation = self._reservations.get(client_order_id)
        return reservation.amount if reservation is not None else ZERO

    # --- Submission -----------------------------------------------------------------------

    def submit(self, approved: ApprovedOrder) -> SubmissionResult:
        """Accept an approved order onto the simulated book.

        Validation, reservation and acceptance happen together: either the order becomes
        working with its funds held, or nothing at all is reserved and the order is recorded
        as rejected. An order whose ``client_order_id`` is already known is returned
        unchanged rather than submitted twice.

        Args:
            approved: The risk-approved order to work.

        Returns:
            The resulting order, its events, and what was reserved.

        Raises:
            UnsupportedOrderTypeError: If the type is not market or limit.
            UnsupportedTimeInForceError: If the instruction is not GTC or IOC.
            UnsupportedMarketTypeError: If the order is not for a spot market.
            MatchingError: If the symbol is unknown to this broker.
        """
        existing = self._orders.get(approved.client_order_id)
        if existing is not None:
            return SubmissionResult(
                order=existing,
                events=(),
                reservation_asset=None,
                reservation_delta=ZERO,
                accepted=existing.status is not OrderStatus.REJECTED,
                newly_submitted=False,
            )

        rules = self._require_symbol(approved.symbol)
        self._require_supported(approved)

        pending = self._build_order(
            approved, status=OrderStatus.PENDING_NEW, filled_quantity=ZERO, avg_fill_price=None
        )
        reservation = self._planned_reservation(approved, rules)

        try:
            self._portfolio.reserve(
                asset=reservation.asset, amount=reservation.amount, at=self._now
            )
        except InsufficientBalanceError as exc:
            rejected = self._transition(
                pending, OrderStatus.REJECTED, reject_reason=_reject_reason(exc)
            )
            return self._commit_submission(
                approved, pending, rejected, reservation=None, accepted=False
            )

        accepted = self._transition(pending, OrderStatus.OPEN)
        return self._commit_submission(
            approved, pending, accepted, reservation=reservation, accepted=True
        )

    def _commit_submission(
        self,
        approved: ApprovedOrder,
        previous: Order,
        order: Order,
        *,
        reservation: _Reservation | None,
        accepted: bool,
    ) -> SubmissionResult:
        """Record a submitted order and build its result. Never fails after reservation."""
        cid = approved.client_order_id
        self._approved[cid] = approved
        self._orders[cid] = order
        self._submission_sequence.append(cid)
        self._filled_notional[cid] = ZERO
        if reservation is not None:
            self._reservations[cid] = reservation
        event = self._status_event(order, previous.status)
        return SubmissionResult(
            order=order,
            events=(event,),
            reservation_asset=reservation.asset if reservation is not None else None,
            reservation_delta=reservation.amount if reservation is not None else ZERO,
            accepted=accepted,
            newly_submitted=True,
        )

    # --- Cancellation ---------------------------------------------------------------------

    def cancel(
        self, client_order_id: str, *, reason: str = "cancelled by request"
    ) -> CancellationResult:
        """Cancel a working order and release everything still reserved against it.

        Cancellation always traverses ``PENDING_CANCEL`` — the state the order contract exists
        to model — and emits a status event for each of the two transitions. A simulated venue
        confirms instantly, so both happen in one call, but collapsing them would erase the
        in-flight state from the audit trail and make this broker's event stream shaped
        differently from a real venue's, which is precisely what a simulator must not do.

        Args:
            client_order_id: Identifier the order was submitted under.
            reason: Recorded as the order's ``cancel_reason``.

        Returns:
            The cancelled order, both status events, and what was released.

        Raises:
            OrderNotFoundError: If no order is known under this id.
        """
        order = self.get_order(client_order_id)
        if order.status.is_terminal:
            return CancellationResult(
                order=order,
                events=(),
                reservation_asset=None,
                released=ZERO,
                newly_canceled=False,
            )

        canceled, events = self._cancel_in_flight(order, reason=reason, at=self._now)
        asset, released = self._release_all(client_order_id)
        self._orders[client_order_id] = canceled
        return CancellationResult(
            order=canceled,
            events=events,
            reservation_asset=asset,
            released=released,
            newly_canceled=True,
        )

    def _cancel_in_flight(
        self, order: Order, *, reason: str, at: datetime
    ) -> tuple[Order, tuple[DomainEvent, ...]]:
        """Take an order through ``PENDING_CANCEL`` to ``CANCELED``, returning both events.

        Filled quantity and average fill price are carried through both transitions, so a
        partially filled order that is then cancelled still reports exactly what it executed.
        """
        pending = self._transition(order, OrderStatus.PENDING_CANCEL, updated_at=at)
        pending_event = self._status_event(pending, order.status)
        canceled = self._transition(
            pending, OrderStatus.CANCELED, cancel_reason=reason, updated_at=at
        )
        return canceled, (pending_event, self._status_event(canceled, pending.status))

    # --- Matching -------------------------------------------------------------------------

    def process_bar(self, bar: MarketBar) -> ExecutionResult:
        """Match every working order for this symbol against one closed bar.

        Matching rules, applied in this order and nowhere varied:

        * A **market** order always matches, at ``bar.open``, then moved adversely by the
          configured slippage.
        * A **limit buy** matches when ``bar.low <= limit_price``, and executes at
          ``limit_price``.
        * A **limit sell** matches when ``bar.high >= limit_price``, and executes at
          ``limit_price``.

        Limit orders execute at their limit price with no slippage applied: filling them
        anywhere else would breach the price the risk engine approved.

        Orders are matched in submission order, so the outcome cannot depend on dictionary
        iteration or on how the caller enumerated them. Every fill is stamped with the bar's
        ``close_time``: only closed bars are matched, so that is the first instant at which
        the bar's high and low are knowable, and using one instant for the whole bar keeps
        fill timestamps monotonic regardless of matching order.

        Args:
            bar: A closed bar for a symbol this broker knows.

        Returns:
            The fills, updated orders, events and reservation releases this bar produced.

        Raises:
            MatchingError: If the symbol is unknown or the bar is not closed.
        """
        rules = self._require_symbol(bar.symbol)
        if not bar.is_closed:
            raise MatchingError(
                "only closed bars may be matched", symbol=bar.symbol, open_time=bar.open_time
            )

        key = (bar.symbol, bar.open_time)
        if key in self._processed_bars:
            return ExecutionResult(fills=(), orders=(), events=(), already_processed=True)

        fills: list[Fill] = []
        touched: list[Order] = []
        events: list[DomainEvent] = []
        released: dict[str, Decimal] = {}

        for cid in tuple(self._submission_sequence):
            order = self._orders[cid]
            if not order.is_open or order.symbol != bar.symbol:
                continue
            if (cid, bar.open_time) in self._matched_against:
                continue
            outcome = self._match_order(cid, order, bar, rules)
            if outcome is None:
                continue
            fill, updated, order_events, release = outcome
            if fill is not None:
                fills.append(fill)
            touched.append(updated)
            events.extend(order_events)
            for asset, amount in release.items():
                released[asset] = released.get(asset, ZERO) + amount

        self._processed_bars.add(key)
        self._now = bar.close_time
        return ExecutionResult(
            fills=tuple(fills),
            orders=tuple(touched),
            events=tuple(events),
            released=released,
            executed=bool(fills),
        )

    def _match_order(
        self,
        cid: str,
        order: Order,
        bar: MarketBar,
        rules: SymbolRules,
    ) -> tuple[Fill | None, Order, tuple[DomainEvent, ...], dict[str, Decimal]] | None:
        """Match one order against one bar, settle any fill, and update broker state.

        Returns ``None`` when the order does not match and nothing changes.
        """
        price = self._execution_price(order, bar)
        if price is None:
            return self._expire_unmatched_ioc(cid, order, bar)
        if order.side is OrderSide.BUY and price > self._price_cap(self._approved[cid]):
            return self._cancel_uncapped_buy(cid, order, bar, price)

        remaining = order.remaining_quantity
        quantity = self._fill_quantity(remaining, rules)
        notional = price * quantity
        fee = self._config.commission.charge(notional, is_first_fill=order.filled_quantity == ZERO)
        executed_at = bar.close_time

        fill = Fill(
            fill_id=self._fill_id(cid, bar),
            order_id=order.order_id,
            client_order_id=cid,
            venue_trade_id=None,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            fee_asset=rules.quote_asset,
            execution_mode=self._execution_mode,
            is_maker=order.order_type is OrderType.LIMIT,
            executed_at=executed_at,
        )

        filled_quantity = order.filled_quantity + quantity
        cumulative_notional = self._filled_notional[cid] + notional
        fully_filled = filled_quantity >= order.quantity
        status = OrderStatus.FILLED if fully_filled else OrderStatus.PARTIALLY_FILLED
        updated = self._transition(
            order,
            status,
            filled_quantity=filled_quantity,
            avg_fill_price=cumulative_notional / filled_quantity,
            updated_at=executed_at,
        )

        consumed = notional + fee if order.side is OrderSide.BUY else quantity
        released = self._settle(cid, fill, consumed=consumed, at=executed_at)

        self._orders[cid] = updated
        self._filled_notional[cid] = cumulative_notional
        self._fills.append(fill)
        self._matched_against.add((cid, bar.open_time))
        events: list[DomainEvent] = [
            self._status_event(updated, order.status),
            FillReceived(
                event_id=deterministic_uuid("event", "fill_received", str(fill.fill_id)),
                occurred_at=executed_at,
                source=self._source,
                correlation_id=None,
                fill=fill,
            ),
        ]

        if fully_filled:
            asset, amount = self._release_all(cid, at=executed_at)
            _accumulate(released, asset, amount)
        elif order.time_in_force is TimeInForce.IOC:
            canceled, cancel_events = self._cancel_in_flight(
                updated, reason="ioc remainder cancelled", at=executed_at
            )
            self._orders[cid] = canceled
            asset, amount = self._release_all(cid, at=executed_at)
            _accumulate(released, asset, amount)
            events.extend(cancel_events)
            return fill, canceled, tuple(events), released

        return fill, updated, tuple(events), released

    def _expire_unmatched_ioc(
        self,
        cid: str,
        order: Order,
        bar: MarketBar,
    ) -> tuple[Fill | None, Order, tuple[DomainEvent, ...], dict[str, Decimal]] | None:
        """Cancel an IOC order that this bar did not match; leave a GTC order working."""
        if order.time_in_force is not TimeInForce.IOC:
            return None
        return self._cancel_during_matching(cid, order, bar, reason="ioc order did not match")

    def _cancel_uncapped_buy(
        self,
        cid: str,
        order: Order,
        bar: MarketBar,
        price: Decimal,
    ) -> tuple[Fill | None, Order, tuple[DomainEvent, ...], dict[str, Decimal]]:
        """Cancel a buy whose execution price would breach the approved cap.

        Deterministic slippage can push a market buy above the ``max_execution_price`` the
        risk engine approved. Filling it anyway would execute at a price nobody authorised and
        would spend more than was reserved; cancelling is the only outcome that honours both
        the approval and the reservation.
        """
        return self._cancel_during_matching(
            cid,
            order,
            bar,
            reason=f"execution price {price} exceeds the approved maximum",
        )

    def _cancel_during_matching(
        self,
        cid: str,
        order: Order,
        bar: MarketBar,
        *,
        reason: str,
    ) -> tuple[Fill | None, Order, tuple[DomainEvent, ...], dict[str, Decimal]]:
        """Cancel a working order mid-bar, releasing its reservation, with no fill produced."""
        canceled, events = self._cancel_in_flight(order, reason=reason, at=bar.close_time)
        released: dict[str, Decimal] = {}
        asset, amount = self._release_all(cid, at=bar.close_time)
        _accumulate(released, asset, amount)
        self._orders[cid] = canceled
        self._matched_against.add((cid, bar.open_time))
        return None, canceled, events, released

    def _settle(
        self,
        cid: str,
        fill: Fill,
        *,
        consumed: Decimal,
        at: datetime,
    ) -> dict[str, Decimal]:
        """Release the consumed reservation, settle the fill, and roll back if it is refused.

        The reservation must be released *before* the fill is applied: the portfolio spends
        from the available half of a balance, so funds still held in reserve are, correctly,
        not spendable. Releasing first and applying second is therefore the only ordering
        under which a reserved order can settle at all.

        The release is the single mutation that precedes the apply, so it is also the single
        thing that must be undone if the apply is refused. Re-reserving the exact amount
        restores the balance precisely, leaving the broker's own state — which has not been
        written yet — untouched. The compensating reserve is stamped with the balance's
        *original* ``updated_at`` rather than the current instant, so a rolled-back execution
        leaves no trace at all: a balance that did not change must not claim it was touched.
        """
        released: dict[str, Decimal] = {}
        reservation = self._reservations.get(cid)
        release_amount = ZERO
        asset: str | None = None
        restore_at = at
        if reservation is not None:
            asset = reservation.asset
            release_amount = min(reservation.amount, consumed)
            prior = self._portfolio.balance(asset)
            if prior is not None:
                restore_at = prior.updated_at
            self._portfolio.release(asset=asset, amount=release_amount, at=at)

        try:
            self._portfolio.apply_fill(fill)
        except Exception:
            if asset is not None and release_amount > ZERO:
                self._portfolio.reserve(asset=asset, amount=release_amount, at=restore_at)
            raise

        if reservation is not None and asset is not None:
            self._reservations[cid] = _Reservation(
                asset=asset, amount=reservation.amount - release_amount
            )
            _accumulate(released, asset, release_amount)
        return released

    # --- Reservations ---------------------------------------------------------------------

    def _planned_reservation(self, approved: ApprovedOrder, rules: SymbolRules) -> _Reservation:
        """Return the funds to hold against an order. Every accepted order reserves.

        A sell reserves the base quantity it is offering. A buy reserves the largest quote
        debit it can possibly incur: ``price_cap * quantity`` plus the most commission that
        notional can attract, where the cap is the order's ``limit_price`` for a limit buy and
        its ``max_execution_price`` for a market buy. Because neither kind of buy can execute
        above its cap — a limit buy fills at its limit, and a market buy above its cap is
        cancelled rather than filled — the held amount is always an upper bound, never a
        guess, and two buys can never be accepted against the same money.
        """
        if approved.side is OrderSide.SELL:
            return _Reservation(asset=rules.base_asset, amount=approved.quantity)
        cap = self._price_cap(approved)
        maximum_notional = cap * approved.quantity
        return _Reservation(
            asset=rules.quote_asset,
            amount=maximum_notional + self._config.commission.maximum_charge(maximum_notional),
        )

    def _price_cap(self, approved: ApprovedOrder) -> Decimal:
        """Return the worst price a buy may execute at.

        Raises:
            UnsupportedOrderTypeError: If a buy carries no cap at all, which the approved
                order contract already forbids; re-checked here so this broker can never
                reserve against an unbounded debit even if that contract is later loosened.
        """
        cap = (
            approved.limit_price
            if approved.order_type is OrderType.LIMIT
            else (approved.max_execution_price)
        )
        if cap is None:
            raise UnsupportedOrderTypeError(
                "a buy must carry a price cap before its funds can be reserved",
                order_type=str(approved.order_type),
            )
        return cap

    def _release_all(self, cid: str, *, at: datetime | None = None) -> tuple[str | None, Decimal]:
        """Release everything still reserved against an order and forget the reservation."""
        reservation = self._reservations.pop(cid, None)
        if reservation is None or reservation.amount == ZERO:
            return (reservation.asset if reservation is not None else None), ZERO
        if reservation.amount < ZERO:  # pragma: no cover - guarded by min() at release time
            raise InsufficientReservationError(
                "reservation went negative", client_order_id=cid, amount=str(reservation.amount)
            )
        self._portfolio.release(
            asset=reservation.asset,
            amount=reservation.amount,
            at=at if at is not None else self._now,
        )
        return reservation.asset, reservation.amount

    # --- Pricing and sizing ------------------------------------------------------------------

    def _execution_price(self, order: Order, bar: MarketBar) -> Decimal | None:
        """Return the price this bar executes an order at, or ``None`` when it does not match."""
        if order.order_type is OrderType.MARKET:
            return self._config.slippage.adjust(bar.open, order.side)
        limit = order.limit_price
        if limit is None:  # pragma: no cover - the order model guarantees a limit price here
            raise MatchingError("a limit order requires a limit price", symbol=order.symbol)
        if order.side is OrderSide.BUY:
            return limit if bar.low <= limit else None
        return limit if bar.high >= limit else None

    def _fill_quantity(self, remaining: Decimal, rules: SymbolRules) -> Decimal:
        """Return how much of ``remaining`` this bar executes.

        With the default ratio of one, the whole remainder fills. With a smaller ratio the
        slice is rounded down onto the venue's quantity step, and any slice that would strand
        a remainder below the venue minimum — or that rounds away to nothing — is widened to
        the full remainder instead. That is what stops a fractional ratio from halving an
        order forever without ever closing it.
        """
        ratio = self._config.fill_ratio
        if ratio >= 1:
            return remaining
        slice_quantity = rules.quantize_quantity(remaining * ratio)
        if slice_quantity <= ZERO or remaining - slice_quantity < rules.min_quantity:
            return remaining
        return slice_quantity

    # --- Order construction ------------------------------------------------------------------

    def _build_order(
        self,
        approved: ApprovedOrder,
        *,
        status: OrderStatus,
        filled_quantity: Decimal,
        avg_fill_price: Decimal | None,
    ) -> Order:
        """Build the first ``Order`` record for an approved order."""
        return Order(
            order_id=deterministic_uuid("order", approved.client_order_id),
            client_order_id=approved.client_order_id,
            venue_order_id=f"{self._venue}-{approved.client_order_id}",
            intent_id=approved.intent_id,
            decision_id=approved.decision_id,
            strategy_id=approved.strategy_id,
            symbol=approved.symbol,
            market_type=approved.market_type,
            side=approved.side,
            order_type=approved.order_type,
            time_in_force=approved.time_in_force,
            execution_mode=self._execution_mode,
            status=status,
            quantity=approved.quantity,
            filled_quantity=filled_quantity,
            avg_fill_price=avg_fill_price,
            limit_price=approved.limit_price,
            stop_price=approved.stop_price,
            fees_paid=ZERO,
            reject_reason=None,
            cancel_reason=None,
            created_at=self._now,
            updated_at=self._now,
        )

    def _transition(
        self,
        order: Order,
        status: OrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        reject_reason: str | None = None,
        cancel_reason: str | None = None,
        updated_at: datetime | None = None,
    ) -> Order:
        """Return a new immutable ``Order`` in ``status``, refusing any illegal transition.

        ``fees_paid`` is deliberately left at zero on every record this broker produces: it is
        a quote-denominated aggregate, and aggregating fees is portfolio accounting. The fee
        for each execution travels on its own :class:`~quantplatform.core.models.orders.Fill`.

        Raises:
            OrderStateTransitionError: If no lifecycle path connects the two statuses.
        """
        if not order.status.can_transition_to(status):
            raise OrderStateTransitionError(
                "no lifecycle path connects these order states",
                client_order_id=order.client_order_id,
                current=str(order.status),
                requested=str(status),
            )
        resolved_filled = filled_quantity if filled_quantity is not None else order.filled_quantity
        resolved_price = avg_fill_price if avg_fill_price is not None else order.avg_fill_price
        return order.model_copy(
            update={
                "status": status,
                "filled_quantity": resolved_filled,
                "avg_fill_price": resolved_price,
                "reject_reason": reject_reason,
                "cancel_reason": cancel_reason,
                "updated_at": updated_at if updated_at is not None else self._now,
            }
        )

    def _status_event(self, order: Order, previous: OrderStatus) -> OrderStatusChanged:
        """Build the status-change event for a transition, with a derived unique id."""
        cid = order.client_order_id
        sequence = self._status_sequence.get(cid, 0)
        self._status_sequence[cid] = sequence + 1
        return OrderStatusChanged(
            event_id=deterministic_uuid("event", "order_status_changed", cid, str(sequence)),
            occurred_at=order.updated_at,
            source=self._source,
            correlation_id=None,
            order=order,
            previous_status=previous,
        )

    def _fill_id(self, cid: str, bar: MarketBar) -> UUID:
        """Derive a stable fill id from the order, the bar and the fill's index on that order."""
        index = sum(1 for fill in self._fills if fill.client_order_id == cid)
        return deterministic_uuid("fill", cid, bar.open_time.isoformat(), str(index))

    # --- Validation -----------------------------------------------------------------------

    def _require_symbol(self, symbol: str) -> SymbolRules:
        """Return the venue rules for a symbol this broker trades, or raise."""
        rules = self._symbols.get(symbol)
        if rules is None:
            raise MatchingError("no venue rules are registered for this symbol", symbol=symbol)
        return rules

    def _require_supported(self, approved: ApprovedOrder) -> None:
        """Reject any order outside the deliberately narrow set this broker simulates."""
        if approved.order_type not in _SUPPORTED_ORDER_TYPES:
            raise UnsupportedOrderTypeError(
                "the simulated broker executes only market and limit orders",
                order_type=str(approved.order_type),
            )
        if approved.time_in_force not in _SUPPORTED_TIME_IN_FORCE:
            raise UnsupportedTimeInForceError(
                "the simulated broker honours only gtc and ioc",
                time_in_force=str(approved.time_in_force),
            )
        if approved.market_type is not MarketType.SPOT:
            raise UnsupportedMarketTypeError(
                "the simulated broker executes only spot orders",
                market_type=str(approved.market_type),
            )


def _accumulate(totals: dict[str, Decimal], asset: str | None, amount: Decimal) -> None:
    """Add a released amount into a per-asset tally, ignoring empty releases."""
    if asset is None or amount == ZERO:
        return
    totals[asset] = totals.get(asset, ZERO) + amount


def _reject_reason(error: InsufficientBalanceError) -> str:
    """Render a venue rejection reason that carries no infrastructure detail."""
    return f"insufficient available balance to reserve: {error.message}"
