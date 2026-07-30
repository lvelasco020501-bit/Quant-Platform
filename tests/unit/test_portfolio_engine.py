"""Phase 3A: :class:`SpotPortfolioEngine` deterministic spot accounting."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from quantplatform.core.enums import MarketType, OrderSide
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
from quantplatform.core.events import PortfolioUpdated, PositionChanged
from quantplatform.core.interfaces import PortfolioEngine
from quantplatform.core.models.orders import Fill
from quantplatform.core.models.portfolio import Position
from quantplatform.portfolio.engine import SpotPortfolioEngine
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_balance,
    make_fill,
    make_portfolio_engine,
    make_position,
    make_symbol_rules,
)

_BTC = "BTC"
_USDT = "USDT"


def _payload(fill: Fill, **overrides: object) -> dict[str, object]:
    """Dump a fill to a re-validatable payload, dropping computed fields."""
    data = fill.model_dump()
    for computed in type(fill).model_computed_fields:
        data.pop(computed, None)
    return {**data, **overrides}


def _btc_balance(engine: SpotPortfolioEngine) -> Decimal:
    return next((b.free for b in engine.balances() if b.asset == _BTC), Decimal(0))


def _seed_consistent_position(
    engine: SpotPortfolioEngine,
    *,
    free: Decimal,
    locked: Decimal,
    avg_entry_price: Decimal,
) -> None:
    """White-box seed a base balance and a matching position directly into engine state.

    Used only to exercise the free/locked reconciliation formulas against a state the
    engine's flat-start constructor deliberately cannot produce on its own (Phase 3A has no
    seedable-position contract; see the seed-state tests below). The balance and position
    seeded here are mutually consistent (``quantity == free + locked``), unlike the
    inconsistent-seed tests, which deliberately seed a mismatch.
    """
    engine._balances[_BTC] = make_balance(asset=_BTC, free=free, locked=locked)
    engine._positions[SYMBOL] = make_position(
        quantity=free + locked, avg_entry_price=avg_entry_price
    )


# --- Buy accounting --------------------------------------------------------------------------


def test_first_buy_opens_position() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(100_000)),))
    fill = make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5))

    result = engine.apply(fill)

    position = result.snapshot.positions[0]
    assert position.quantity == Decimal("0.1")
    assert position.avg_entry_price == Decimal(50_050)
    assert position.realized_pnl == Decimal(0)
    assert position.fees_paid == Decimal(5)
    assert position.opened_at == ANCHOR
    assert position.updated_at == ANCHOR
    assert result.newly_applied is True


def test_repeated_buy_increases_position() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5)))
    second = make_fill(
        quantity=Decimal("0.1"),
        price=Decimal(51_000),
        fee=Decimal(5),
        executed_at=ANCHOR + timedelta(hours=1),
    )

    result = engine.apply(second)

    position = result.snapshot.positions[0]
    assert position.quantity == Decimal("0.2")
    assert position.avg_entry_price == Decimal(50_550)
    assert position.fees_paid == Decimal(10)
    assert position.realized_pnl == Decimal(0)
    assert position.opened_at == ANCHOR
    assert position.updated_at == ANCHOR + timedelta(hours=1)


def test_weighted_average_entry_includes_quote_fee() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(100))

    result = engine.apply(fill)

    position = result.snapshot.positions[0]
    assert position.avg_entry_price == Decimal(50_100)
    assert position.avg_entry_price != fill.price


def test_buy_fee_increases_cost_basis() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(100))

    result = engine.apply(fill)

    assert result.snapshot.positions[0].cost_basis == Decimal(50_100)


# --- Sell accounting -------------------------------------------------------------------------


def test_partial_sell_preserves_average_entry() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    sell = make_fill(
        quantity=Decimal("0.4"),
        price=Decimal(52_000),
        fee=Decimal(0),
        side=OrderSide.SELL,
        executed_at=ANCHOR + timedelta(hours=1),
    )

    result = engine.apply(sell)

    position = result.snapshot.positions[0]
    assert position.quantity == Decimal("0.6")
    assert position.avg_entry_price == Decimal(50_000)
    assert position.realized_pnl == Decimal(800)


def test_full_sell_closes_position() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    sell = make_fill(
        quantity=Decimal(1),
        price=Decimal(53_000),
        fee=Decimal(0),
        side=OrderSide.SELL,
        executed_at=ANCHOR + timedelta(hours=1),
    )

    result = engine.apply(sell)

    position = result.snapshot.positions[0]
    assert position.quantity == Decimal(0)
    assert position.avg_entry_price is None
    assert position.realized_pnl == Decimal(3_000)
    assert position.opened_at == ANCHOR
    assert position.updated_at == ANCHOR + timedelta(hours=1)
    assert _btc_balance(engine) == Decimal(0)


def test_realized_pnl_on_profitable_sell() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    result = engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(55_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    assert result.snapshot.positions[0].realized_pnl == Decimal(5_000)


def test_realized_pnl_on_losing_sell() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    result = engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(45_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    assert result.snapshot.positions[0].realized_pnl == Decimal(-5_000)


def test_sell_fee_reduces_realized_pnl() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    result = engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(55_000),
            fee=Decimal(50),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    assert result.snapshot.positions[0].realized_pnl == Decimal(4_950)


# --- Fee handling -----------------------------------------------------------------------------


def test_total_fees_accumulate() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(5)))
    result = engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(55_000),
            fee=Decimal(3),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    assert result.snapshot.total_fees == Decimal(8)
    assert result.snapshot.positions[0].fees_paid == Decimal(8)


def test_zero_fee_in_non_quote_asset_is_accepted() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0), fee_asset=_BTC)

    result = engine.apply(fill)

    assert result.newly_applied is True
    assert result.snapshot.total_fees == Decimal(0)


def test_unsupported_fee_asset_fails_atomically() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(1), fee_asset=_BTC)

    with pytest.raises(UnsupportedFeeAssetError):
        engine.apply(fill)

    assert engine.balances() == (make_balance(free=Decimal(1_000_000)),)
    assert engine.positions() == ()
    assert engine.has_applied(fill.fill_id) is False


# --- Lifecycle: reopening and closed-snapshot immutability --------------------------------


def test_account_realized_pnl_survives_position_closure() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(55_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    reopened = engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(50_000),
            fee=Decimal(0),
            executed_at=ANCHOR + timedelta(hours=2),
        )
    )

    assert reopened.snapshot.realized_pnl == Decimal(5_000)
    assert reopened.snapshot.positions[0].realized_pnl == Decimal(0)


def test_reopening_creates_a_new_lifecycle() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(5)))
    engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(55_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    reopen_time = ANCHOR + timedelta(hours=2)
    result = engine.apply(
        make_fill(
            quantity=Decimal(2), price=Decimal(48_000), fee=Decimal(7), executed_at=reopen_time
        )
    )

    position = result.snapshot.positions[0]
    assert position.opened_at == reopen_time
    assert position.realized_pnl == Decimal(0)
    assert position.fees_paid == Decimal(7)
    assert position.avg_entry_price == Decimal("48003.5")


def test_closed_historical_snapshot_remains_unchanged() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    close_result = engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(55_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    closed_position = close_result.snapshot.positions[0]

    engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(50_000),
            fee=Decimal(0),
            executed_at=ANCHOR + timedelta(hours=2),
        )
    )

    assert closed_position.quantity == Decimal(0)
    assert closed_position.realized_pnl == Decimal(5_000)
    assert engine.positions()[0] is not closed_position
    assert engine.positions()[0].quantity == Decimal(1)


# --- Reconciliation invariants ------------------------------------------------------------


def test_base_balance_reconciles_with_position_quantity() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fills = [
        make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)),
        make_fill(
            quantity=Decimal("0.3"),
            price=Decimal(52_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=1),
        ),
        make_fill(
            quantity=Decimal("0.7"),
            price=Decimal(53_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=2),
        ),
    ]
    for fill in fills:
        result = engine.apply(fill)
        assert result.snapshot.positions[0].quantity == _btc_balance(engine)


def test_cash_reconciles_with_quote_balance() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    result = engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(5)))
    quote_balance = next(b for b in result.snapshot.balances if b.asset == _USDT)
    assert result.snapshot.cash == quote_balance.total


def test_unrelated_balances_remain_unchanged() -> None:
    eth_balance = make_balance(asset="ETH", free=Decimal(10), locked=Decimal(2))
    engine = make_portfolio_engine(
        initial_balances=(make_balance(free=Decimal(1_000_000)), eth_balance)
    )
    engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(5)))
    stored_eth = next(b for b in engine.balances() if b.asset == "ETH")
    assert stored_eth == eth_balance


def test_locked_balances_remain_unchanged() -> None:
    engine = make_portfolio_engine(
        initial_balances=(make_balance(free=Decimal(1_000_000), locked=Decimal(5_000)),)
    )
    engine.apply(make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5)))
    quote_balance = next(b for b in engine.balances() if b.asset == _USDT)
    assert quote_balance.locked == Decimal(5_000)
    assert quote_balance.free == Decimal(1_000_000) - Decimal(5_005)


# --- Atomic failures ------------------------------------------------------------------------


def test_insufficient_quote_balance_fails_atomically() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(100)),))
    fill = make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0))

    with pytest.raises(InsufficientBalanceError):
        engine.apply(fill)

    assert engine.balances() == (make_balance(free=Decimal(100)),)
    assert engine.positions() == ()
    assert engine.has_applied(fill.fill_id) is False


def test_insufficient_base_balance_fails_atomically() -> None:
    # Simulates an inconsistent starting state: the internal position record claims exposure
    # that the base-asset balance was never seeded to back. Reachable in Phase 3A only via
    # direct state seeding since engine-only mutation always keeps the two in lockstep; it
    # exercises the defensive base-balance guard described in section 10 of the request.
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine._positions[SYMBOL] = make_position(quantity=Decimal(1), avg_entry_price=Decimal(50_000))
    fill = make_fill(
        quantity=Decimal("0.5"), price=Decimal(50_000), fee=Decimal(0), side=OrderSide.SELL
    )

    with pytest.raises(InsufficientBalanceError):
        engine.apply(fill)

    assert _btc_balance(engine) == Decimal(0)
    assert engine.has_applied(fill.fill_id) is False


def test_selling_above_position_quantity_fails_atomically() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(0)))
    positions_before = engine.positions()
    balances_before = engine.balances()
    fill = make_fill(
        quantity=Decimal("0.5"),
        price=Decimal(50_000),
        fee=Decimal(0),
        side=OrderSide.SELL,
        executed_at=ANCHOR + timedelta(hours=1),
    )

    with pytest.raises(InsufficientPositionError):
        engine.apply(fill)

    assert engine.positions() == positions_before
    assert engine.balances() == balances_before
    assert engine.has_applied(fill.fill_id) is False


def test_symbol_mismatch_for_unknown_symbol_fails_atomically() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = Fill.model_validate(
        _payload(
            make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)), symbol="ETH/USDT"
        )
    )

    with pytest.raises(SymbolMismatchError):
        engine.apply(fill)

    assert engine.positions() == ()
    assert engine.has_applied(fill.fill_id) is False


def test_symbol_mismatch_for_wrong_quote_asset_fails_atomically() -> None:
    engine = make_portfolio_engine(
        quote_asset="USDT",
        symbols={"BTC/BUSD": make_symbol_rules(symbol="BTC/BUSD", quote_asset="BUSD")},
        initial_balances=(make_balance(free=Decimal(1_000_000)),),
    )
    fill = Fill.model_validate(
        _payload(
            make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)),
            symbol="BTC/BUSD",
        )
    )

    with pytest.raises(SymbolMismatchError):
        engine.apply(fill)

    assert engine.positions() == ()


def test_unsupported_market_type_fails_atomically() -> None:
    engine = make_portfolio_engine(
        symbols={SYMBOL: make_symbol_rules(market_type=MarketType.FUTURES)},
        initial_balances=(make_balance(free=Decimal(1_000_000)),),
    )
    fill = make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0))

    with pytest.raises(UnsupportedMarketTypeError):
        engine.apply(fill)

    assert engine.positions() == ()


def test_invalid_fill_side_is_defensively_rejected() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0))
    object.__setattr__(fill, "side", cast(OrderSide, "hold"))

    with pytest.raises(InvalidFillSideError):
        engine.apply(fill)

    assert engine.has_applied(fill.fill_id) is False


def test_accounting_invariant_violation_from_inconsistent_runtime_state() -> None:
    # A stray base balance injected directly into engine state (bypassing the flat-start
    # constructor gate, which now rejects this at seed time — see the seed-state tests below)
    # breaks the quantity-reconciles-with-balance invariant on the very next buy.
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine._balances[_BTC] = make_balance(asset=_BTC, free=Decimal("0.05"))
    fill = make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(0))

    with pytest.raises(AccountingInvariantError):
        engine.apply(fill)

    assert engine.has_applied(fill.fill_id) is False


# --- Idempotency ------------------------------------------------------------------------------


def test_repeated_fill_is_idempotent() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5))

    first = engine.apply(fill)
    positions_after_first = engine.positions()
    balances_after_first = engine.balances()
    second = engine.apply(fill)

    assert second.newly_applied is False
    assert second.snapshot == first.snapshot
    assert engine.positions() == positions_after_first
    assert engine.balances() == balances_after_first


def test_repeated_fill_emits_no_events() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    fill = make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5))
    engine.apply(fill)

    second = engine.apply(fill)

    assert second.events == ()


# --- Determinism ------------------------------------------------------------------------------


def test_deterministic_balance_ordering() -> None:
    # BTC is the engine's only registered base asset, so its seed balance must be flat
    # (zero total); ETH is not registered to any symbol here and can be seeded freely.
    engine = make_portfolio_engine(
        initial_balances=(
            make_balance(asset="USDT", free=Decimal(1_000_000)),
            make_balance(asset="ETH", free=Decimal(10)),
            make_balance(asset="BTC", free=Decimal(0)),
        )
    )
    assert [balance.asset for balance in engine.balances()] == ["BTC", "ETH", "USDT"]


def test_deterministic_position_ordering() -> None:
    engine = make_portfolio_engine(
        symbols={
            SYMBOL: make_symbol_rules(),
            "ETH/USDT": make_symbol_rules(symbol="ETH/USDT", base_asset="ETH"),
        },
        initial_balances=(make_balance(free=Decimal(1_000_000)),),
    )
    engine.apply(
        make_fill(symbol="ETH/USDT", quantity=Decimal(1), price=Decimal(2_000), fee=Decimal(0))
    )
    engine.apply(
        make_fill(
            symbol=SYMBOL,
            quantity=Decimal(1),
            price=Decimal(50_000),
            fee=Decimal(0),
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    assert [position.symbol for position in engine.positions()] == ["BTC/USDT", "ETH/USDT"]


def test_deterministic_event_and_snapshot_output_across_engines() -> None:
    def _fresh() -> SpotPortfolioEngine:
        return make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))

    fill = make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5))
    result_a = _fresh().apply(fill)
    result_b = _fresh().apply(fill)

    assert result_a.events[0].event_id == result_b.events[0].event_id
    assert result_a.events[1].event_id == result_b.events[1].event_id
    assert result_a.snapshot == result_b.snapshot


# --- Precision and type safety ----------------------------------------------------------------


def test_decimal_precision_with_high_precision_prices_and_quantities() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(10**9)),))
    quantity = Decimal("0.25")
    price = Decimal("50000.123456789012345678")
    fee = Decimal("0.000000000000000001")
    fill = make_fill(quantity=quantity, price=price, fee=fee)

    result = engine.apply(fill)

    expected_notional = price * quantity
    expected_required = expected_notional + fee
    expected_avg_entry = expected_required / quantity
    position = result.snapshot.positions[0]
    assert position.avg_entry_price == expected_avg_entry
    assert position.fees_paid == fee


def test_no_float_enters_accounting_path() -> None:
    fill = make_fill()
    payload = _payload(fill, price=50_000.0)
    with pytest.raises(ValidationError):
        Fill.model_validate(payload)


# --- Events -------------------------------------------------------------------------------


def test_position_changed_covers_open_increase_reduce_close() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))

    opened = engine.apply(make_fill(quantity=Decimal(1), price=Decimal(50_000), fee=Decimal(0)))
    opened_event = next(e for e in opened.events if isinstance(e, PositionChanged))
    assert opened_event.previous_position is None
    assert opened_event.position.quantity == Decimal(1)

    increased = engine.apply(
        make_fill(
            quantity=Decimal(1),
            price=Decimal(51_000),
            fee=Decimal(0),
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )
    increased_event = next(e for e in increased.events if isinstance(e, PositionChanged))
    assert increased_event.previous_position is not None
    assert increased_event.previous_position.quantity == Decimal(1)
    assert increased_event.position.quantity == Decimal(2)

    reduced = engine.apply(
        make_fill(
            quantity=Decimal("0.5"),
            price=Decimal(52_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=2),
        )
    )
    reduced_event = next(e for e in reduced.events if isinstance(e, PositionChanged))
    assert reduced_event.position.quantity == Decimal("1.5")
    assert reduced_event.position.is_open

    closed = engine.apply(
        make_fill(
            quantity=Decimal("1.5"),
            price=Decimal(53_000),
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(hours=3),
        )
    )
    closed_event = next(e for e in closed.events if isinstance(e, PositionChanged))
    assert closed_event.previous_position is not None
    assert closed_event.previous_position.is_open
    assert not closed_event.position.is_open


def test_portfolio_updated_emitted_after_successful_fill() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    result = engine.apply(make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5)))

    updated_events = [e for e in result.events if isinstance(e, PortfolioUpdated)]
    assert len(updated_events) == 1
    assert updated_events[0].snapshot == result.snapshot


def test_apply_fill_satisfies_the_portfolio_engine_protocol() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    assert isinstance(engine, PortfolioEngine)
    fill = make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5))
    engine.apply_fill(fill)
    assert engine.has_applied(fill.fill_id) is True


# --- Property-based tests -----------------------------------------------------------------


@given(
    quantities=st.lists(
        st.decimals(min_value=Decimal("0.00000001"), max_value=Decimal(5), places=8),
        min_size=1,
        max_size=6,
    ),
    price=st.decimals(min_value=Decimal(1), max_value=Decimal(100_000), places=2),
)
def test_property_sequential_buys_keep_position_reconciled(
    quantities: list[Decimal], price: Decimal
) -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(10**13)),))
    total_quantity = Decimal(0)
    for index, quantity in enumerate(quantities):
        fill = make_fill(
            quantity=quantity,
            price=price,
            fee=Decimal(0),
            executed_at=ANCHOR + timedelta(seconds=index),
        )
        engine.apply(fill)
        total_quantity += quantity

    position = engine.positions()[0]
    assert position.quantity == total_quantity
    assert _btc_balance(engine) == total_quantity
    assert position.realized_pnl == Decimal(0)


@given(
    quantity=st.decimals(min_value=Decimal("0.00000001"), max_value=Decimal(1_000), places=8),
    price=st.decimals(min_value=Decimal(1), max_value=Decimal(100_000), places=2),
)
def test_property_buy_then_full_sell_at_same_price_nets_zero_realized_pnl(
    quantity: Decimal, price: Decimal
) -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(10**13)),))
    engine.apply(make_fill(quantity=quantity, price=price, fee=Decimal(0)))
    result = engine.apply(
        make_fill(
            quantity=quantity,
            price=price,
            fee=Decimal(0),
            side=OrderSide.SELL,
            executed_at=ANCHOR + timedelta(seconds=1),
        )
    )

    position = result.snapshot.positions[0]
    assert position.quantity == Decimal(0)
    assert position.avg_entry_price is None
    assert position.realized_pnl == Decimal(0)


# --- Base-asset reconciliation with locked balances (audit) -------------------------------


def test_seeded_position_quantity_equals_base_total_when_locked_present() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    _seed_consistent_position(
        engine, free=Decimal("0.6"), locked=Decimal("0.4"), avg_entry_price=Decimal(50_000)
    )

    base = next(b for b in engine.balances() if b.asset == _BTC)
    position = engine.positions()[0]
    assert base.free == Decimal("0.6")
    assert base.locked == Decimal("0.4")
    assert base.total == Decimal(1)
    assert position.quantity == base.total


def test_partial_sell_spends_only_free_base_and_preserves_locked() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    _seed_consistent_position(
        engine, free=Decimal("0.6"), locked=Decimal("0.4"), avg_entry_price=Decimal(50_000)
    )
    fill = make_fill(
        quantity=Decimal("0.5"), price=Decimal(52_000), fee=Decimal(0), side=OrderSide.SELL
    )

    result = engine.apply(fill)

    base = next(b for b in result.snapshot.balances if b.asset == _BTC)
    position = result.snapshot.positions[0]
    assert base.free == Decimal("0.1")
    assert base.locked == Decimal("0.4")
    assert base.total == Decimal("0.5")
    assert position.quantity == Decimal("0.5")
    assert position.quantity == base.total


def test_sell_exceeding_free_but_not_total_fails_atomically() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    _seed_consistent_position(
        engine, free=Decimal("0.3"), locked=Decimal("0.7"), avg_entry_price=Decimal(50_000)
    )
    # Requested quantity is within position.quantity and base.total, but exceeds base.free —
    # the sell must not be allowed to spend locked base asset to make up the difference.
    fill = make_fill(
        quantity=Decimal("0.5"), price=Decimal(52_000), fee=Decimal(0), side=OrderSide.SELL
    )

    with pytest.raises(InsufficientBalanceError):
        engine.apply(fill)

    base = next(b for b in engine.balances() if b.asset == _BTC)
    assert base.free == Decimal("0.3")
    assert base.locked == Decimal("0.7")
    assert engine.positions()[0].quantity == Decimal(1)
    assert engine.has_applied(fill.fill_id) is False


def test_buy_with_locked_base_preserves_locked_and_increases_total() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    _seed_consistent_position(
        engine, free=Decimal("0.6"), locked=Decimal("0.4"), avg_entry_price=Decimal(50_000)
    )
    fill = make_fill(quantity=Decimal("0.2"), price=Decimal(51_000), fee=Decimal(0))

    result = engine.apply(fill)

    base = next(b for b in result.snapshot.balances if b.asset == _BTC)
    position = result.snapshot.positions[0]
    assert base.free == Decimal("0.8")
    assert base.locked == Decimal("0.4")
    assert base.total == Decimal("1.2")
    assert position.quantity == Decimal("1.2")
    assert position.quantity == base.total


def test_unrelated_locked_base_balance_remains_unchanged_across_symbols() -> None:
    engine = make_portfolio_engine(
        symbols={
            SYMBOL: make_symbol_rules(),
            "ETH/USDT": make_symbol_rules(symbol="ETH/USDT", base_asset="ETH"),
        },
        initial_balances=(make_balance(free=Decimal(1_000_000)),),
    )
    eth_balance = make_balance(asset="ETH", free=Decimal(2), locked=Decimal(3))
    eth_position = Position(
        symbol="ETH/USDT",
        base_asset="ETH",
        quote_asset="USDT",
        quantity=Decimal(5),
        avg_entry_price=Decimal(2_000),
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=ANCHOR,
        updated_at=ANCHOR,
    )
    engine._balances["ETH"] = eth_balance
    engine._positions["ETH/USDT"] = eth_position

    engine.apply(make_fill(quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5)))

    assert next(b for b in engine.balances() if b.asset == "ETH") == eth_balance
    assert next(p for p in engine.positions() if p.symbol == "ETH/USDT") == eth_position


# --- Seed-state contract: flat-start (audit) -----------------------------------------------


def test_constructor_rejects_nonzero_free_base_balance_seed() -> None:
    with pytest.raises(InconsistentSeedStateError):
        make_portfolio_engine(
            initial_balances=(
                make_balance(free=Decimal(1_000_000)),
                make_balance(asset=_BTC, free=Decimal("0.1")),
            )
        )


def test_constructor_rejects_locked_only_base_balance_seed() -> None:
    with pytest.raises(InconsistentSeedStateError):
        make_portfolio_engine(
            initial_balances=(
                make_balance(free=Decimal(1_000_000)),
                make_balance(asset=_BTC, free=Decimal(0), locked=Decimal("0.1")),
            )
        )


def test_constructor_accepts_zero_total_base_balance_seed() -> None:
    engine = make_portfolio_engine(
        initial_balances=(
            make_balance(free=Decimal(1_000_000)),
            make_balance(asset=_BTC, free=Decimal(0), locked=Decimal(0)),
        )
    )
    assert engine.positions() == ()


def test_constructor_allows_nonzero_balance_for_asset_not_registered_as_any_base() -> None:
    engine = make_portfolio_engine(
        initial_balances=(
            make_balance(free=Decimal(1_000_000)),
            make_balance(asset="ADA", free=Decimal(500)),
        )
    )
    ada_balance = next(b for b in engine.balances() if b.asset == "ADA")
    assert ada_balance.free == Decimal(500)


def test_engine_starts_flat_with_empty_positions_and_zero_cumulative_totals() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    snapshot = engine.snapshot(as_of=ANCHOR, mark_prices={})
    assert engine.positions() == ()
    assert snapshot.realized_pnl == Decimal(0)
    assert snapshot.total_fees == Decimal(0)


# --- Fill chronology (audit) ----------------------------------------------------------------


def test_out_of_order_fill_fails_atomically() -> None:
    # Chronology failure raises before `_build_events` is ever called, so there is no return
    # value to inspect for events; atomicity is proven via unchanged engine state below.
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    engine.apply(
        make_fill(
            quantity=Decimal("0.1"),
            price=Decimal(50_000),
            fee=Decimal(0),
            executed_at=ANCHOR + timedelta(hours=2),
        )
    )
    positions_before = engine.positions()
    balances_before = engine.balances()
    earlier_fill = make_fill(
        quantity=Decimal("0.1"),
        price=Decimal(51_000),
        fee=Decimal(0),
        executed_at=ANCHOR + timedelta(hours=1),
    )

    with pytest.raises(OutOfOrderFillError):
        engine.apply(earlier_fill)

    assert engine.positions() == positions_before
    assert engine.balances() == balances_before
    assert engine.has_applied(earlier_fill.fill_id) is False


def test_repeated_older_fill_remains_idempotent_despite_being_out_of_order() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    first_fill = make_fill(
        quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(0), executed_at=ANCHOR
    )
    engine.apply(first_fill)
    engine.apply(
        make_fill(
            quantity=Decimal("0.1"),
            price=Decimal(51_000),
            fee=Decimal(0),
            executed_at=ANCHOR + timedelta(hours=1),
        )
    )

    # Replaying the first fill again: same fill_id, but its own timestamp is now earlier than
    # the engine's last applied fill. Idempotency is checked before chronology, so this must
    # still succeed as a no-op rather than raise OutOfOrderFillError.
    result = engine.apply(first_fill)

    assert result.newly_applied is False
    assert result.events == ()


def test_equal_timestamp_fills_are_both_accepted_deterministically() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    same_time = ANCHOR + timedelta(hours=1)
    first = make_fill(
        quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(0), executed_at=same_time
    )
    second = make_fill(
        quantity=Decimal("0.2"), price=Decimal(50_000), fee=Decimal(0), executed_at=same_time
    )

    result1 = engine.apply(first)
    result2 = engine.apply(second)

    assert result1.newly_applied is True
    assert result2.newly_applied is True
    assert result2.snapshot.positions[0].quantity == Decimal("0.3")


def test_timestamps_are_monotonic_after_a_valid_sequence() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    timestamps = [
        ANCHOR,
        ANCHOR + timedelta(hours=1),
        ANCHOR + timedelta(hours=1),
        ANCHOR + timedelta(hours=3),
    ]
    seen_taken_at = []
    result = None
    for ts in timestamps:
        result = engine.apply(
            make_fill(
                quantity=Decimal("0.01"), price=Decimal(50_000), fee=Decimal(0), executed_at=ts
            )
        )
        seen_taken_at.append(result.snapshot.taken_at)

    assert seen_taken_at == sorted(seen_taken_at)
    assert result is not None
    assert result.snapshot.positions[0].updated_at == timestamps[-1]


# --- Event and snapshot timestamp/id consistency (audit) ------------------------------------


def test_distinct_fills_at_equal_timestamp_produce_distinct_event_and_snapshot_ids() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    same_time = ANCHOR + timedelta(hours=1)
    first = make_fill(
        quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(0), executed_at=same_time
    )
    second = make_fill(
        quantity=Decimal("0.2"), price=Decimal(50_000), fee=Decimal(0), executed_at=same_time
    )

    result1 = engine.apply(first)
    result2 = engine.apply(second)

    assert result1.events[0].event_id != result2.events[0].event_id
    assert result1.events[1].event_id != result2.events[1].event_id
    assert result1.snapshot.snapshot_id != result2.snapshot.snapshot_id


def test_successful_fill_uses_executed_at_consistently_across_state_and_events() -> None:
    engine = make_portfolio_engine(initial_balances=(make_balance(free=Decimal(1_000_000)),))
    executed_at = ANCHOR + timedelta(hours=5)
    result = engine.apply(
        make_fill(
            quantity=Decimal("0.1"), price=Decimal(50_000), fee=Decimal(5), executed_at=executed_at
        )
    )

    assert result.snapshot.positions[0].updated_at == executed_at
    assert result.snapshot.taken_at == executed_at
    position_changed = next(e for e in result.events if isinstance(e, PositionChanged))
    portfolio_updated = next(e for e in result.events if isinstance(e, PortfolioUpdated))
    assert position_changed.occurred_at == executed_at
    assert portfolio_updated.occurred_at == executed_at
