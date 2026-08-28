"""Turning strategy signals into order intents.

A signal is an opinion with no size and no authority; an intent is a concrete proposal the
risk engine can evaluate. Translating between them is orchestration's job, and deliberately
not the strategy's: a strategy cannot see the account, so it cannot be the thing that decides
how much to buy.

What is decided here is only *what to ask for*. A long entry asks for a share of equity; an
exit asks to close exactly what is held. The risk engine reduces or refuses either.

Risk authors intents too, when a position breaches what protects it. Those are built here for
the same reason: an exit forced by risk must be an ordinary proposal travelling the ordinary
path, so that it is sized, fee-modelled and filled exactly like any other order rather than by
a second execution path nobody benchmarks.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quantplatform.core.enums import (
    ExecutionMode,
    MarketType,
    OrderSide,
    OrderType,
    SignalAction,
    TimeInForce,
)
from quantplatform.core.errors import UnsupportedRiskInputError
from quantplatform.core.ids import deterministic_uuid, idempotency_key
from quantplatform.core.models.orders import OrderIntent
from quantplatform.core.models.portfolio import PortfolioSnapshot
from quantplatform.core.models.risk import RiskAction
from quantplatform.core.models.signals import Signal

__all__ = ["build_forced_exit_intent", "build_intent"]

_SIDE_BY_ACTION = {
    SignalAction.ENTER_LONG: OrderSide.BUY,
    SignalAction.EXIT_LONG: OrderSide.SELL,
}

FORCED_EXIT_AUTHOR = "risk_protective_stop"
"""Recorded as the author of an exit risk forced, so the audit trail never attributes to a
strategy an order that strategy did not ask for."""

FORCED_EXIT_AUTHOR_VERSION = "1.0.0"


def build_intent(
    signal: Signal,
    *,
    snapshot: PortfolioSnapshot,
    entry_fraction: Decimal,
    execution_mode: ExecutionMode,
    market_type: MarketType = MarketType.SPOT,
) -> OrderIntent | None:
    """Translate one actionable signal into an order intent.

    Returns ``None`` for a signal that expresses no action, and for an exit with nothing to
    close — asking the risk engine to sell a position that does not exist would produce a
    rejection that says nothing about risk.

    Args:
        signal: The strategy's opinion.
        snapshot: Account state used to size the request; never shown to the strategy.
        entry_fraction: Share of equity a long entry asks for.
        execution_mode: Mode recorded on the intent.
        market_type: Market being traded; spot only for now.

    Returns:
        The intent to evaluate, or ``None`` when there is nothing to propose.

    Raises:
        UnsupportedRiskInputError: If the signal asks for short exposure, which this
            spot-only platform prohibits rather than silently drops.
    """
    if not signal.is_actionable:
        return None
    if signal.action.requires_short_selling or signal.action is SignalAction.EXIT_SHORT:
        raise UnsupportedRiskInputError(
            "short exposure is prohibited on this spot-only platform",
            action=signal.action.value,
            symbol=signal.symbol,
        )

    side = _SIDE_BY_ACTION[signal.action]
    quantity: Decimal | None = None
    notional: Decimal | None = None

    if side is OrderSide.BUY:
        notional = snapshot.equity * entry_fraction
        if notional <= 0:
            return None
    else:
        quantity = _held_quantity(snapshot, signal.symbol)
        if quantity <= 0:
            return None

    key = idempotency_key(
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        signal_time=signal.bar_close_time,
        action=signal.action,
        execution_mode=execution_mode,
    )
    return OrderIntent(
        intent_id=deterministic_uuid("order_intent", key),
        signal_id=signal.signal_id,
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        market_type=market_type,
        side=side,
        order_type=OrderType.MARKET,
        requested_quantity=quantity,
        requested_notional=notional,
        limit_price=None,
        stop_price=None,
        time_in_force=TimeInForce.GTC,
        execution_mode=execution_mode,
        idempotency_key=key,
        reason=signal.reason,
        created_at=signal.bar_close_time,
    )


def _held_quantity(snapshot: PortfolioSnapshot, symbol: str) -> Decimal:
    """Return the open quantity held for a symbol, zero when none is."""
    for position in snapshot.positions:
        if position.symbol == symbol and position.is_open:
            return position.quantity
    return Decimal(0)


def build_forced_exit_intent(
    action: RiskAction,
    *,
    snapshot: PortfolioSnapshot,
    bar_close_time: datetime,
    execution_mode: ExecutionMode,
    market_type: MarketType = MarketType.SPOT,
) -> OrderIntent | None:
    """Translate one risk action into the market exit that carries it out.

    The intent is deliberately indistinguishable from a strategic exit in everything the
    broker can see: a market sell of what is held, good until cancelled. Only its authorship
    differs, and that difference is recorded rather than disguised.

    Args:
        action: What risk decided must happen; only a close is expressed today.
        snapshot: Account state, used to close exactly what is held.
        bar_close_time: Close of the bar that triggered the action, which dates the intent.
        execution_mode: Mode recorded on the intent.
        market_type: Market being traded; spot only for now.

    Returns:
        The exit to evaluate, or ``None`` when the action names no symbol or the account
        holds nothing to sell — proposing a sale of what does not exist would produce a
        rejection that says nothing about risk.
    """
    if action.symbol is None:
        return None
    held = _held_quantity(snapshot, action.symbol)
    quantity = action.quantity if action.quantity is not None else held
    quantity = min(quantity, held)
    if quantity <= 0:
        return None

    key = idempotency_key(
        strategy_id=FORCED_EXIT_AUTHOR,
        strategy_version=FORCED_EXIT_AUTHOR_VERSION,
        symbol=action.symbol,
        signal_time=bar_close_time,
        action=SignalAction.EXIT_LONG,
        execution_mode=execution_mode,
    )
    return OrderIntent(
        intent_id=deterministic_uuid("order_intent", key),
        signal_id=deterministic_uuid("forced_exit_signal", key),
        strategy_id=FORCED_EXIT_AUTHOR,
        strategy_version=FORCED_EXIT_AUTHOR_VERSION,
        symbol=action.symbol,
        market_type=market_type,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        requested_quantity=quantity,
        requested_notional=None,
        limit_price=None,
        stop_price=None,
        time_in_force=TimeInForce.GTC,
        execution_mode=execution_mode,
        idempotency_key=key,
        reason=action.reason,
        created_at=bar_close_time,
    )
