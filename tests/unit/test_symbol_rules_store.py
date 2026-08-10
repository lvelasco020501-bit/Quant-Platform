"""The one shared view of the venue's trading rules.

Every property asserted here exists to stop the same class of bug: two components trading
against different beliefs about what the venue will accept. The store is the reason that
cannot happen, so its behaviour under replacement is worth pinning down precisely.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from quantplatform.core.errors import ConfigurationError
from quantplatform.core.symbol_rules import SymbolRulesStore, as_symbol_rules_store
from tests.factories import ANCHOR, SYMBOL, make_symbol_rules

_OTHER = "ETH/USDT"


# --- Reading ------------------------------------------------------------------------------------


def test_a_store_reads_as_an_ordinary_mapping() -> None:
    # The property that lets it drop into every component that already takes a mapping.
    rules = make_symbol_rules()
    store = SymbolRulesStore({SYMBOL: rules})

    assert store[SYMBOL] == rules
    assert dict(store) == {SYMBOL: rules}
    assert list(store) == [SYMBOL]
    assert len(store) == 1
    assert SYMBOL in store


def test_current_returns_what_is_in_force_now() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})

    store.replace({SYMBOL: make_symbol_rules(price_tick=Decimal("0.5"))})

    assert store.current(SYMBOL).price_tick == Decimal("0.5")


def test_an_unregistered_symbol_is_refused_rather_than_returning_nothing() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules()})

    with pytest.raises(ConfigurationError, match="no venue trading rules"):
        store.current("DOGE/USDT")


def test_the_store_copies_what_it_is_given() -> None:
    # Exactly the protection the old `dict(symbols)` copy gave: a caller that keeps its own
    # dictionary and edits it later must not be able to change what the platform trades on.
    seed = {SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))}
    store = SymbolRulesStore(seed)

    seed[SYMBOL] = make_symbol_rules(price_tick=Decimal("999"))

    assert store.current(SYMBOL).price_tick == Decimal("0.01")


def test_a_snapshot_does_not_move_under_a_later_refresh() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})
    taken = store.snapshot()

    store.replace({SYMBOL: make_symbol_rules(price_tick=Decimal("0.5"))})

    assert taken[SYMBOL].price_tick == Decimal("0.01")
    assert store.current(SYMBOL).price_tick == Decimal("0.5")


# --- Age ----------------------------------------------------------------------------------------


def test_age_is_measured_from_the_oldest_symbol() -> None:
    # The worst symbol decides: the risk engine judges each intent against its own symbol's
    # rules, so a store is only as fresh as the stalest thing in it.
    store = SymbolRulesStore(
        {
            SYMBOL: make_symbol_rules(updated_at=ANCHOR),
            _OTHER: make_symbol_rules(
                symbol=_OTHER, base_asset="ETH", updated_at=ANCHOR + timedelta(hours=5)
            ),
        }
    )

    assert store.oldest_updated_at == ANCHOR
    assert store.age_seconds(ANCHOR + timedelta(hours=6)) == pytest.approx(6 * 3600)


def test_an_empty_store_has_no_age_to_report() -> None:
    store = SymbolRulesStore({})

    assert store.oldest_updated_at is None
    assert store.age_seconds(ANCHOR + timedelta(days=400)) == 0.0


def test_a_refresh_resets_the_age() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(updated_at=ANCHOR)})
    later = ANCHOR + timedelta(hours=6)

    store.replace({SYMBOL: make_symbol_rules(updated_at=later)})

    assert store.age_seconds(later) == 0.0


# --- Replacement --------------------------------------------------------------------------------


def test_replacement_reports_which_limits_actually_changed() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})

    changed = store.replace({SYMBOL: make_symbol_rules(price_tick=Decimal("0.5"))})

    assert changed == (SYMBOL,)


def test_refetching_identical_limits_is_not_a_change() -> None:
    # A re-fetch every six hours would otherwise report a venue rule change every six hours,
    # and an operator would learn to ignore the one time it meant something.
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(updated_at=ANCHOR)})

    changed = store.replace(
        {SYMBOL: make_symbol_rules(updated_at=ANCHOR + timedelta(hours=6), source="refetched")}
    )

    assert changed == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_tick", Decimal("0.5")),
        ("quantity_step", Decimal("0.01")),
        ("min_quantity", Decimal("0.002")),
        ("max_quantity", Decimal(500)),
        ("min_notional", Decimal(25)),
        ("max_notional", Decimal(900_000)),
    ],
)
def test_every_limit_that_binds_an_order_counts_as_a_change(field: str, value: Decimal) -> None:
    baseline: dict[str, Any] = {"quantity_step": Decimal("0.001")}
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(**baseline)})

    changed = store.replace({SYMBOL: make_symbol_rules(**(baseline | {field: value}))})

    assert changed == (SYMBOL,)


def test_a_replacement_that_drops_a_traded_symbol_is_refused() -> None:
    # Losing a symbol mid-run leaves the broker unable to price it, which turns a refresh
    # into an outage. The previous rules are kept instead.
    store = SymbolRulesStore(
        {SYMBOL: make_symbol_rules(), _OTHER: make_symbol_rules(symbol=_OTHER, base_asset="ETH")}
    )

    with pytest.raises(ConfigurationError, match="every symbol already in use"):
        store.replace({SYMBOL: make_symbol_rules()})

    assert set(store) == {SYMBOL, _OTHER}


def test_a_refused_replacement_leaves_nothing_half_applied() -> None:
    store = SymbolRulesStore(
        {
            SYMBOL: make_symbol_rules(price_tick=Decimal("0.01")),
            _OTHER: make_symbol_rules(symbol=_OTHER, base_asset="ETH"),
        }
    )

    with pytest.raises(ConfigurationError):
        store.replace({SYMBOL: make_symbol_rules(price_tick=Decimal("0.5"))})

    assert store.current(SYMBOL).price_tick == Decimal("0.01")


def test_a_replacement_may_add_a_symbol() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules()})

    store.replace(
        {SYMBOL: make_symbol_rules(), _OTHER: make_symbol_rules(symbol=_OTHER, base_asset="ETH")}
    )

    assert set(store) == {SYMBOL, _OTHER}


def test_stored_rules_are_never_mutated_in_place() -> None:
    original = make_symbol_rules(price_tick=Decimal("0.01"))
    store = SymbolRulesStore({SYMBOL: original})

    store.replace({SYMBOL: make_symbol_rules(price_tick=Decimal("0.5"))})

    assert original.price_tick == Decimal("0.01")
    assert original.model_config["frozen"] is True


# --- Adoption -----------------------------------------------------------------------------------


def test_a_store_is_adopted_by_reference_so_refreshes_reach_its_holder() -> None:
    # The whole mechanism in one assertion: a component that normalised its constructor
    # argument through this helper sees a later refresh.
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})

    held = as_symbol_rules_store(store)
    store.replace({SYMBOL: make_symbol_rules(price_tick=Decimal("0.5"))})

    assert held is store
    assert held.current(SYMBOL).price_tick == Decimal("0.5")


def test_a_plain_mapping_is_frozen_into_a_store() -> None:
    seed = {SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))}

    held = as_symbol_rules_store(seed)
    seed[SYMBOL] = make_symbol_rules(price_tick=Decimal("999"))

    assert isinstance(held, SymbolRulesStore)
    assert held.current(SYMBOL).price_tick == Decimal("0.01")


def test_the_representation_names_symbols_rather_than_dumping_rules() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules()})

    assert repr(store) == "SymbolRulesStore(symbols=['BTC/USDT'])"
