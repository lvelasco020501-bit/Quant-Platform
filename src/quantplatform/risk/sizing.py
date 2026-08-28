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

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import TYPE_CHECKING, Protocol

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.enums import OrderSide
from quantplatform.core.errors import RiskSizingError
from quantplatform.core.models.execution_policy import ExecutionPolicy
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.risk import RiskBudget
from quantplatform.core.models.stops import StopSpecification
from quantplatform.core.numeric import apply_basis_points, ceil_to_step, quantize_to_step

if TYPE_CHECKING:
    from quantplatform.risk.config import RiskConfiguration

__all__ = [
    "FixedFractionSizer",
    "PositionSizer",
    "RiskBasedSizer",
    "SizingOutcome",
    "SizingRequest",
    "break_even_price",
    "market_buy_price_cap",
    "normalize_limit_price",
    "normalize_quantity",
    "projected_stop_out_cost",
    "quantity_for_notional",
    "select_sizer",
]

_BASIS_POINT_DIVISOR = Decimal(10_000)


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


# --- Position sizing (M4) ---------------------------------------------------------------------
#
# Two ways of answering "how large?", and the platform has only ever had the first.
#
# A fixed fraction asks how much of the account to *spend*. It is what week 5 ran on, and it
# committed roughly 95% of the account to a single entry — not through a fault, but because
# that is precisely what `entry_fraction = 0.95` instructs. The question it cannot express is
# the one that governs survival: given where this position stops losing money, how large may
# it be before a stop-out costs more than the account can afford?
#
# Nothing here is wired into StandardRiskEngine. The engine still sizes exactly as it did,
# so V1 equivalence is structural rather than asserted, and connecting a sizer belongs with
# the milestone that can prove a stop is actually enforced.


@dataclass(frozen=True, slots=True)
class SizingRequest:
    """Everything a sizer may consider. Nothing else is reachable from one."""

    equity: Decimal
    available_quote: Decimal
    entry_price: Decimal
    side: OrderSide
    rules: SymbolRules
    stop: StopSpecification | None = None
    budget: RiskBudget | None = None
    policy: ExecutionPolicy | None = None


@dataclass(frozen=True, slots=True)
class SizingOutcome:
    """What a sizer decided, and what bound it.

    ``quantity`` of zero is an ordinary answer meaning no tradeable size remains, not a
    failure — a genuinely incoherent input raises
    :class:`~quantplatform.core.errors.RiskSizingError` instead.
    """

    quantity: Decimal = ZERO
    notional: Decimal | None = None
    """Set by a sizer that expresses size as a spend rather than a quantity."""

    risk_amount: Decimal | None = None
    """What a stop-out would actually cost, computed from the *final* size.

    ``None`` when the sizer cannot know — a fixed fraction of equity says nothing about what
    being wrong costs, which is the whole reason the risk-based sizer exists. Never the
    configured budget: if a cap shrank the position, less is genuinely at risk, and
    reporting the budget would overstate exposure and corrupt every R-multiple built on it.
    """

    capped_by: str | None = None
    """Which limit bound the result, for a sizer that applies limits. The risk-based sizer
    applies none — see its docstring for why that ownership sits with the engine."""

    reason: str = ""


class PositionSizer(Protocol):
    """Decides how large a position may be. Decides nothing else.

    A sizer never chooses direction, never chooses an instrument, and never decides when to
    exit. It answers one question, from the arguments it is handed and nothing more.
    """

    def size(self, request: SizingRequest) -> SizingOutcome:
        """Return the largest size this sizer's rule permits."""
        ...


class FixedFractionSizer:
    """Spends a fixed share of equity, regardless of where the loss stops.

    V1, preserved exactly. It reports no ``risk_amount`` because it genuinely cannot compute
    one: nothing in its inputs says what being stopped out would cost.
    """

    def __init__(self, *, entry_fraction: Decimal) -> None:
        """Configure the share of equity each entry spends."""
        self._entry_fraction = entry_fraction

    def size(self, request: SizingRequest) -> SizingOutcome:
        """Return the notional a fixed fraction of equity funds."""
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            notional = request.equity * self._entry_fraction
        if notional <= ZERO:
            return SizingOutcome(reason="no equity to size against")
        return SizingOutcome(
            quantity=quantity_for_notional(notional, request.entry_price, request.rules),
            notional=notional,
            reason="fixed fraction of equity",
        )


class RiskBasedSizer:
    """Sizes a position so that being stopped out costs a known, budgeted amount.

    **The cost of being wrong is the whole calculation.** A stop-out does not cost
    ``quantity * stop_distance``; it costs that plus the commission to enter, the commission
    to exit, and whatever slippage the exit suffers. Week 5's fees were 26% of its realised
    loss, so a budget computed on the price move alone would have been overshot by roughly a
    quarter — silently, which is exactly the "protection that was never applied" failure the
    risk contracts exist to make impossible.

    **It applies no caps.** Balance, exposure, venue minimums, lot precision and notional
    ceilings are all owned by :meth:`~quantplatform.risk.engine.StandardRiskEngine._constrain`,
    which has applied them to every order the platform has ever approved. An earlier draft of
    this class reapplied two of them, which integration exposed as two owners of one rule —
    idempotent today and a source of drift the moment either changed. This answers exactly
    one question: how much may be bought before a stop-out exceeds its budget.

    Costs are read from the shared
    :class:`~quantplatform.core.models.execution_policy.ExecutionPolicy` — the same object
    the broker executes under, so the two cannot hold different numbers. The commission is
    decomposed into a fixed part and a per-unit part by probing the policy at zero notional
    rather than by branching on which model is configured: a flat fee is charged once per
    leg and must be subtracted from the budget, while a rate scales with size and belongs in
    the per-unit loss. Treating one as the other is wrong in the dangerous direction for
    small positions.
    """

    def size(self, request: SizingRequest) -> SizingOutcome:
        """Return the largest size whose stop-out stays inside the risk budget.

        Raises:
            RiskSizingError: If the stop cannot be reasoned about — absent, without an
                absolute level, at or beyond the entry, or outside the budget's admissible
                distance window. Each is a statement that cannot be true rather than a
                market condition, so it surfaces rather than silently sizing to zero.
        """
        budget = self._require(request.budget, "a risk budget")
        stop = self._require(request.stop, "a stop")
        trigger = self._trigger_price(stop)
        self._check_direction(request, trigger)

        distance = abs(request.entry_price - trigger)
        self._check_distance_window(distance, request.entry_price, budget)

        policy = request.policy if request.policy is not None else ExecutionPolicy()
        exit_price = policy.slippage.adjust(trigger, self._exit_side(request.side))
        fixed_cost, unit_cost = self._decompose_costs(policy, request.entry_price, exit_price)

        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            capital_at_risk = request.equity * budget.risk_per_trade_pct
            spendable = capital_at_risk - fixed_cost
            if spendable <= ZERO:
                return SizingOutcome(reason="fixed execution cost exceeds the whole risk budget")
            loss_per_unit = abs(request.entry_price - exit_price) + unit_cost
            if loss_per_unit <= ZERO:  # pragma: no cover - guarded by the distance checks
                raise RiskSizingError("the projected loss per unit is zero")
            raw = spendable / loss_per_unit

        quantity = normalize_quantity(raw, request.rules)
        if quantity <= ZERO:
            return SizingOutcome(reason="the risk budget admits no venue-valid size")

        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            risk_amount = quantity * loss_per_unit + fixed_cost
        return SizingOutcome(
            quantity=quantity,
            risk_amount=risk_amount,
            reason="sized from the capital the stop puts at risk",
        )

    @staticmethod
    def _require[T](value: T | None, what: str) -> T:
        """Return a required input or refuse, naming what was missing.

        Raises:
            RiskSizingError: If the value is absent.
        """
        if value is None:
            msg = f"risk-based sizing requires {what}"
            raise RiskSizingError(msg)
        return value

    @staticmethod
    def _trigger_price(stop: StopSpecification) -> Decimal:
        """Return the stop's absolute level.

        Raises:
            RiskSizingError: If the stop is expressed as a distance. A relative level is
                meaningful only once a fill price exists to measure from; guessing one would
                size against a number nobody set.
        """
        if stop.trigger_price is None:
            msg = (
                f"a {stop.kind.value} stop carries no trigger price, so there is no distance "
                "to size against"
            )
            raise RiskSizingError(msg, stop_kind=stop.kind.value)
        return stop.trigger_price

    @staticmethod
    def _exit_side(side: OrderSide) -> OrderSide:
        """Return the side the stop-out itself executes on."""
        return OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY

    @staticmethod
    def _check_direction(request: SizingRequest, trigger: Decimal) -> None:
        """Check the stop sits on the losing side of the entry.

        Raises:
            RiskSizingError: If the stop is at the entry, or on the profitable side of it.
                A long protected above its entry is not protected; it is a position that
                profits by being stopped out, which means the level was set backwards.
        """
        if trigger == request.entry_price:
            msg = "the stop sits at the entry price, giving a zero distance to size against"
            raise RiskSizingError(msg, entry_price=str(request.entry_price))
        if request.side is OrderSide.BUY and trigger > request.entry_price:
            msg = "a long's stop sits above its entry price, on the profitable side"
            raise RiskSizingError(
                msg, entry_price=str(request.entry_price), stop_price=str(trigger)
            )
        if request.side is OrderSide.SELL and trigger < request.entry_price:
            msg = "a short's stop sits below its entry price, on the profitable side"
            raise RiskSizingError(
                msg, entry_price=str(request.entry_price), stop_price=str(trigger)
            )

    @staticmethod
    def _check_distance_window(distance: Decimal, entry: Decimal, budget: RiskBudget) -> None:
        """Check the stop is neither inside the noise nor so wide sizing is meaningless.

        Raises:
            RiskSizingError: If the distance falls outside the budget's window.
        """
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            distance_bps = (distance / entry) * _BASIS_POINT_DIVISOR
        if distance_bps < budget.min_stop_distance_bps:
            msg = "the stop is nearer than min_stop_distance_bps permits"
            raise RiskSizingError(
                msg,
                distance_bps=str(distance_bps),
                limit=str(budget.min_stop_distance_bps),
            )
        if distance_bps > budget.max_stop_distance_bps:
            msg = "the stop is further than max_stop_distance_bps permits"
            raise RiskSizingError(
                msg,
                distance_bps=str(distance_bps),
                limit=str(budget.max_stop_distance_bps),
            )

    @staticmethod
    def _decompose_costs(
        policy: ExecutionPolicy, entry_price: Decimal, exit_price: Decimal
    ) -> tuple[Decimal, Decimal]:
        """Split round-trip commission into a fixed part and a per-unit part.

        Probes the policy at zero notional rather than branching on the configured model. A
        flat fee answers the same amount at any notional, so it appears entirely in the fixed
        term; a rate answers zero at zero, so it appears entirely in the per-unit term. The
        decomposition is therefore exact for every affine fee model without this function
        needing to know which one is in force.
        """
        at_zero = policy.fee.fee_for(ZERO, is_first_fill=True)
        fixed = at_zero * 2
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            entry_leg = policy.fee.fee_for(entry_price, is_first_fill=True) - at_zero
            exit_leg = policy.fee.fee_for(exit_price, is_first_fill=True) - at_zero
        return fixed, entry_leg + exit_leg


def select_sizer(
    config: RiskConfiguration, *, has_stop: bool, entry_fraction: Decimal
) -> PositionSizer:
    """Choose the one sizer that governs a decision. Never two.

    The precedence is explicit because the alternative is the failure mode where
    ``entry_fraction`` and ``risk_per_trade_pct`` both apply to the same order and neither is
    obviously wrong. Risk-based sizing needs **both** a budget and a stop: a budget alone
    cannot size anything without knowing where the loss stops, and inventing a stop to
    satisfy the configuration would fabricate the very number the budget is measured against.

    Falling back is deliberate rather than silent — a caller that configured a budget and
    received fixed-fraction sizing did so because the intent carried no stop, and the
    returned sizer says so by its type.
    """
    if config.risk_budget is not None and has_stop:
        return RiskBasedSizer()
    return FixedFractionSizer(entry_fraction=entry_fraction)


def projected_stop_out_cost(
    *,
    quantity: Decimal,
    entry_price: Decimal,
    stop: StopSpecification,
    side: OrderSide,
    policy: ExecutionPolicy,
) -> Decimal | None:
    """Return what being stopped out of an existing position would cost.

    The same arithmetic :class:`RiskBasedSizer` sizes with, exposed so that the figure
    recorded against a position and the figure a position was sized to are computed by one
    function rather than two that must agree. They are asked in opposite directions — sizing
    solves for quantity given a budget, this solves for cost given a quantity — and a second
    implementation of the shared middle would drift the first time either changed.

    Args:
        quantity: Open size the cost is computed over.
        entry_price: What the position actually paid, averaged across its fills.
        stop: The level it is protected at.
        side: Direction of the open position.
        policy: Fee and slippage assumptions, shared with the executing adapter.

    Returns:
        The modelled cost, or ``None`` when the stop carries no absolute level to measure
        against — a distance-only stop describes no cost until a level exists.
    """
    if stop.trigger_price is None or quantity <= ZERO:
        return None
    exit_side = OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY
    exit_price = policy.slippage.adjust(stop.trigger_price, exit_side)
    at_zero = policy.fee.fee_for(ZERO, is_first_fill=True)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        entry_leg = policy.fee.fee_for(entry_price, is_first_fill=True) - at_zero
        exit_leg = policy.fee.fee_for(exit_price, is_first_fill=True) - at_zero
        per_unit = abs(entry_price - exit_price) + entry_leg + exit_leg
        return quantity * per_unit + at_zero * 2


def break_even_price(
    *,
    quantity: Decimal,
    entry_price: Decimal,
    side: OrderSide,
    policy: ExecutionPolicy,
) -> Decimal:
    """Return the price at which closing this position recovers exactly what it cost.

    Not the entry price. Exiting where the position opened pays the exit's slippage and the
    exit's commission, so a stop placed at entry and called break-even reports a scratch and
    books a loss — the kind of name that lies, which is what this function exists to avoid.

    ``entry_price`` is the position's ``avg_entry_price``, which the portfolio engine already
    computes fee-inclusive: it divides ``executed_notional + fee`` by the filled quantity. The
    entry's commission is therefore **already** in the figure and must not be added again, or
    the level comes out one commission too high and no arithmetic anywhere explains why.

    The net proceeds of the exit are affine in the exit price — ``slippage.adjust`` and
    ``apply_basis_points`` are exact scalings with no rounding or quantisation, and every fee
    model is either zero, a constant or a rate on notional. Two probes therefore determine the
    line exactly, and its root is the answer, without branching on which fee model is
    configured. It is the same technique that decomposes a fee into fixed and per-unit parts
    elsewhere in this module.

    The root is rounded *up*. A level that nets fractionally below zero is not a break-even
    level, and of the two directions only one can be described honestly.

    Args:
        quantity: Open size that would be closed.
        entry_price: Fee-inclusive average price the position was opened at.
        side: Direction of the open position; spot is long-only.
        policy: Fee and slippage assumptions, shared with the executing adapter.

    Returns:
        The exit price at which the modelled round trip nets zero or fractionally above.

    Raises:
        RiskSizingError: If the position is not long, or if the policy admits no such price
            — total slippage or a 100% fee leaves the exit's proceeds independent of, or
            decreasing in, the price. Returning something anyway would put a stop calling
            itself break-even at a level that loses the position.
    """
    if side is not OrderSide.BUY:
        msg = "break-even is defined for long positions only on this spot-only platform"
        raise RiskSizingError(msg, side=side.value)

    def _net(price: Decimal) -> Decimal:
        exit_price = policy.slippage.adjust(price, OrderSide.SELL)
        proceeds = quantity * exit_price
        fee = policy.fee.fee_for(proceeds, is_first_fill=True)
        return proceeds - fee - quantity * entry_price

    with localcontext() as ctx:
        ctx.prec = DECIMAL_WORKING_PRECISION
        low = entry_price
        high = entry_price * 2
        at_low = _net(low)
        slope = (_net(high) - at_low) / (high - low)
        if slope <= ZERO:
            msg = "this execution policy admits no price at which the round trip breaks even"
            raise RiskSizingError(
                msg, slope=str(slope), entry_price=str(entry_price), quantity=str(quantity)
            )
        ctx.rounding = ROUND_CEILING
        advance = -at_low / slope
    return low + advance
