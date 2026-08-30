"""The stop stops being metadata.

Five milestones built the vocabulary: a stop that could be expressed, sized against,
carried to an approved order, and recorded against the position it protects. None of it
protected anything — a position whose price fell straight through its stop kept running,
because nothing looked.

These tests pin the looking. `evaluate_open_positions` reads a closed bar and the risk state
of what is open, and says whether a position must be closed. It does not close anything: it
returns an action, the engine turns that action into an ordinary intent, and the ordinary
broker fills it on the next bar at that bar's open. There is no second fill engine inside
risk, and no fill at the stop price — a stop is a trigger, never a guaranteed execution.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quantplatform.core.enums import (
    OrderSide,
    RiskActionKind,
    RiskCheckSeverity,
    RiskOutcome,
    StopKind,
)
from quantplatform.core.errors import PositionRiskUnavailableError
from quantplatform.core.models.risk import PositionRiskState, RiskContext
from quantplatform.core.models.stops import StopSpecification
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_balance,
    make_bar,
    make_intent,
    make_position,
    make_risk_context,
    make_risk_engine,
    make_snapshot,
)

_STOP_PRICE = Decimal("49000")


def _holding_context(
    *,
    approved_orders_last_hour: int = 0,
    approved_orders_today: int = 0,
    spread_basis_points: Decimal | None = Decimal(1),
    as_of: datetime = ANCHOR,
) -> RiskContext:
    """A context whose account actually holds the position a forced exit would sell.

    Without it every sell sizes to zero against a flat book, and these tests would pass or
    fail for a reason unrelated to the authority being pinned.
    """
    return make_risk_context(
        snapshot=make_snapshot(
            positions=(make_position(quantity=Decimal("0.1")),),
            balances=(
                make_balance(free=Decimal(5_000)),
                make_balance(asset="BTC", free=Decimal("0.1")),
            ),
        ),
        approved_orders_last_hour=approved_orders_last_hour,
        approved_orders_today=approved_orders_today,
        spread_basis_points=spread_basis_points,
        as_of=as_of,
        latest_bar_close_time=ANCHOR,
    )


def _risk_state(**overrides: object) -> PositionRiskState:
    defaults: dict[str, object] = {
        "symbol": SYMBOL,
        "stop": StopSpecification(kind=StopKind.HARD, trigger_price=_STOP_PRICE),
        "quantity": Decimal("0.1"),
        "initial_risk_amount": Decimal("100"),
        "current_risk_amount": Decimal("100"),
        "entry_price": Decimal("50000"),
        "opened_at": ANCHOR,
    }
    return PositionRiskState(**{**defaults, **overrides})  # type: ignore[arg-type]


def _bar(*, low: Decimal, close: Decimal | None = None) -> object:
    """A closed bar whose low is what a stop is judged against."""
    resolved = close if close is not None else low + Decimal(500)
    return make_bar(
        index=0, open_price=resolved, high=resolved + Decimal(100), low=low, close=resolved
    )


def _evaluate(**overrides: object) -> tuple[object, ...]:
    engine = make_risk_engine()
    defaults: dict[str, object] = {
        "positions": (make_position(quantity=Decimal("0.1")),),
        "position_risk": {SYMBOL: _risk_state()},
        "bar": _bar(low=Decimal("49500")),
        "require_protection": False,
    }
    merged = {**defaults, **overrides}
    return tuple(engine.evaluate_open_positions(**merged))  # type: ignore[arg-type]


# --- Trigger ------------------------------------------------------------------------------------


def test_a_position_above_its_stop_is_left_alone() -> None:
    assert _evaluate(bar=_bar(low=Decimal("49500"))) == ()


def test_a_bar_that_touches_the_stop_exactly_forces_an_exit() -> None:
    # A stop at the price is a stop reached. Requiring a breach would mean the one bar that
    # traded exactly at the level did not count, which is not what a stop means.
    actions = _evaluate(bar=_bar(low=_STOP_PRICE))

    assert len(actions) == 1
    assert actions[0].kind is RiskActionKind.CLOSE  # type: ignore[attr-defined]
    assert actions[0].symbol == SYMBOL  # type: ignore[attr-defined]


def test_a_bar_whose_low_crosses_the_stop_forces_an_exit() -> None:
    actions = _evaluate(bar=_bar(low=Decimal("48000")))

    assert len(actions) == 1
    assert actions[0].kind is RiskActionKind.CLOSE  # type: ignore[attr-defined]


def test_a_flat_book_produces_no_actions() -> None:
    assert _evaluate(positions=(), position_risk={}) == ()


def test_a_position_with_no_recorded_risk_is_left_alone_when_protection_is_optional() -> None:
    # V1: no stop was ever derived, so there is nothing to enforce and nothing to complain
    # about. This is the path every run the platform has ever completed took.
    assert _evaluate(position_risk={}, bar=_bar(low=Decimal("1"))) == ()


# --- Fail loudly --------------------------------------------------------------------------------


def test_a_protected_position_with_no_recorded_risk_fails_loudly() -> None:
    # The failure this replaces is silence: a position that ought to be protected, is not,
    # and nothing anywhere says so. Continuing would mean the account is exposed while the
    # system reports that it is covered.
    with pytest.raises(PositionRiskUnavailableError, match="no recorded risk"):
        _evaluate(position_risk={}, require_protection=True)


def test_the_refusal_names_the_position_it_could_not_account_for() -> None:
    with pytest.raises(PositionRiskUnavailableError) as caught:
        _evaluate(position_risk={}, require_protection=True)

    assert SYMBOL in str(caught.value.details)


def test_a_recorded_risk_without_an_absolute_level_fails_loudly_when_required() -> None:
    # A trailing stop that never armed carries no level to test a bar against. Treating that
    # as "not triggered" would report protection that cannot be evaluated.
    trailing = _risk_state(
        stop=StopSpecification(kind=StopKind.TRAILING, distance_bps=Decimal(200))
    )

    with pytest.raises(PositionRiskUnavailableError, match="no trigger price"):
        _evaluate(position_risk={SYMBOL: trailing}, require_protection=True)


# --- Authority: a forced exit outranks an administrative limit -----------------------------------


def test_an_exhausted_order_rate_cannot_block_a_protective_exit() -> None:
    # The limit exists to stop a strategy over-trading. Letting it also stop a stop-out would
    # invert its purpose exactly: the account would stay exposed *because* it had been
    # active, which is the opposite of what a rate limit is for.
    engine = make_risk_engine(max_orders_per_hour=1, max_orders_per_day=1)
    context = _holding_context(approved_orders_last_hour=5, approved_orders_today=5)
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(intent, context, forced_exit=True).decision

    assert decision.outcome is not RiskOutcome.REJECTED


def test_the_same_rate_limit_still_blocks_an_ordinary_order() -> None:
    # The exemption is for protective exits and nothing else. An ordinary intent under the
    # same exhausted budget must still be refused, or the limit means nothing.
    engine = make_risk_engine(max_orders_per_hour=1, max_orders_per_day=1)
    context = make_risk_context(approved_orders_last_hour=5, approved_orders_today=5)

    decision = engine.assess(make_intent(), context).decision

    assert decision.outcome is RiskOutcome.REJECTED


def test_a_rate_limit_is_recorded_as_advisory_rather_than_hidden_on_a_forced_exit() -> None:
    # Exempt is not the same as unrecorded. The check still runs and still reports what it
    # found; only its authority to veto is withdrawn, so the audit trail stays complete.
    engine = make_risk_engine(max_orders_per_hour=1, max_orders_per_day=1)
    context = _holding_context(approved_orders_last_hour=5, approved_orders_today=5)
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(intent, context, forced_exit=True).decision

    hourly = next(c for c in decision.checks if c.code.value == "max_hourly_orders")
    assert hourly.severity is RiskCheckSeverity.ADVISORY
    assert hourly.passed is False


def test_a_venue_rule_still_blocks_a_forced_exit() -> None:
    # Risk may overrule its own administrative limits. It may not fabricate an order the
    # venue would reject — an unexecutable order protects nothing, and pretending otherwise
    # would report an exit that never happened.
    engine = make_risk_engine()
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.0000000001"))

    decision = engine.assess(intent, _holding_context(), forced_exit=True).decision

    assert decision.outcome is RiskOutcome.REJECTED


# --- M7a: protection that must exist, and refusals that name what is missing --------------------


def test_a_risk_record_with_no_open_position_fails_loudly() -> None:
    # The orphan. M5b drops a record the moment its position goes flat, so one that outlives
    # its position means that cleanup did not run — and the next entry on the symbol would be
    # reconciled against a stop belonging to a position that no longer exists. Silence here
    # would let a stale level protect nothing while claiming to protect something.
    with pytest.raises(PositionRiskUnavailableError, match="no open position"):
        _evaluate(positions=(), require_protection=True)


def test_the_orphan_refusal_names_the_symbol_it_could_not_account_for() -> None:
    with pytest.raises(PositionRiskUnavailableError) as caught:
        _evaluate(positions=(), require_protection=True)

    assert SYMBOL in str(caught.value.details)


def test_a_risk_record_whose_quantity_has_drifted_fails_loudly() -> None:
    # M5b restates the record from the position after every fill, so a divergence means that
    # restatement failed. The recorded risk would then describe a size the account does not
    # hold: too small and the stop under-closes, leaving a residue; too large and it asks to
    # sell what is not there. Neither is a protected position.
    with pytest.raises(PositionRiskUnavailableError, match="quantity"):
        _evaluate(
            positions=(make_position(quantity=Decimal("0.2")),),
            position_risk={SYMBOL: _risk_state(quantity=Decimal("0.1"))},
            require_protection=True,
        )


def test_a_drifted_quantity_is_tolerated_when_protection_is_optional() -> None:
    # V1 never records risk at all, so this path is unreachable there. The gate stays on
    # `require_protection` regardless, so that a V1 run cannot begin raising on state a
    # future change happens to leave behind.
    actions = _evaluate(
        positions=(make_position(quantity=Decimal("0.2")),),
        position_risk={SYMBOL: _risk_state(quantity=Decimal("0.1"))},
        bar=_bar(low=Decimal("49500")),
    )

    assert actions == ()


# --- M7a: a market condition may refuse an entry, never a protective exit -----------------------


def test_an_excessive_spread_still_blocks_an_ordinary_entry() -> None:
    # The control. A wide spread is a real reason not to open a position, and must keep
    # being one, or the exemption below would be indistinguishable from removing the check.
    engine = make_risk_engine(max_spread_bps=Decimal(5))

    decision = engine.assess(
        make_intent(), make_risk_context(spread_basis_points=Decimal(500))
    ).decision

    assert decision.outcome is RiskOutcome.REJECTED


def test_an_excessive_spread_does_not_block_a_protective_exit() -> None:
    # A wide spread makes the exit expensive; it does not make it impossible. Refusing to
    # close would keep the account exposed precisely while the market is disorderly, which
    # inverts what the guard is for in the same way the rate limit did.
    engine = make_risk_engine(max_spread_bps=Decimal(5))
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(
        intent, _holding_context(spread_basis_points=Decimal(500)), forced_exit=True
    ).decision

    assert decision.outcome is not RiskOutcome.REJECTED


def test_the_spread_is_recorded_as_advisory_rather_than_hidden_on_a_forced_exit() -> None:
    engine = make_risk_engine(max_spread_bps=Decimal(5))
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(
        intent, _holding_context(spread_basis_points=Decimal(500)), forced_exit=True
    ).decision

    spread = next(c for c in decision.checks if c.code.value == "excessive_spread")
    assert spread.severity is RiskCheckSeverity.ADVISORY
    assert spread.passed is False


def test_stale_data_still_blocks_a_protective_exit() -> None:
    # Where the exemption stops, and the reason the authority order is real rather than
    # decorative. A market condition is a judgement about a price we can see; stale data
    # means we cannot see one. Exiting on it would act on a level that may not exist, and
    # data integrity outranks capital protection precisely so that never happens.
    engine = make_risk_engine()
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))
    stale = _holding_context(as_of=ANCHOR + timedelta(hours=1))

    decision = engine.assess(intent, stale, forced_exit=True).decision

    assert decision.outcome is RiskOutcome.REJECTED
