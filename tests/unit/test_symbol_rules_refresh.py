"""Keeping the venue's rulebook current without ever going near a network.

Every provider here is a double. That is not a convenience: the failure modes that matter —
a throttled endpoint, a malformed document, an outage lasting past the freshness budget —
cannot be produced on demand against the real venue, and a test suite that reached the
internet to look for them would be slow, flaky and quietly dependent on Binance being up.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import OrderStatus, OrderType
from quantplatform.core.errors import ConfigurationError, DataProviderError
from quantplatform.core.interfaces import SymbolRulesMaintainer, SymbolRulesProvider
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.orders import Order
from quantplatform.core.symbol_rules import SymbolRulesStore
from quantplatform.orchestration.symbol_rules import SymbolRulesRefresher
from tests.factories import ANCHOR, SYMBOL, make_order, make_symbol_rules

_SIX_HOURS = 6 * 3600
_ONE_DAY = 24 * 3600


class _Provider:
    """Answers with rules stamped at whatever the clock says, and can be told to fail."""

    def __init__(self, clock: SimulatedClock, **overrides: Decimal) -> None:
        self.clock = clock
        self.overrides: dict[str, Any] = dict(overrides)
        self.calls = 0
        self.fail_with: Exception | None = None
        self.requested: list[tuple[str, ...]] = []

    def fetch(self, symbols: Sequence[str]) -> Mapping[str, SymbolRules]:
        self.calls += 1
        self.requested.append(tuple(symbols))
        if self.fail_with is not None:
            raise self.fail_with
        return {
            symbol: make_symbol_rules(symbol=symbol, updated_at=self.clock.now(), **self.overrides)
            for symbol in symbols
        }


def _refresher(
    *,
    clock: SimulatedClock | None = None,
    store: SymbolRulesStore | None = None,
    interval: float = _SIX_HOURS,
    stale_after: int = _ONE_DAY,
    open_orders: object = None,
) -> tuple[SymbolRulesRefresher, _Provider, SymbolRulesStore, SimulatedClock]:
    resolved_clock = clock if clock is not None else SimulatedClock(ANCHOR)
    resolved_store = (
        store
        if store is not None
        else SymbolRulesStore({SYMBOL: make_symbol_rules(updated_at=ANCHOR)})
    )
    provider = _Provider(resolved_clock)
    refresher = SymbolRulesRefresher(
        store=resolved_store,
        provider=provider,
        clock=resolved_clock,
        refresh_interval_seconds=interval,
        stale_after_seconds=stale_after,
        open_orders=open_orders,  # type: ignore[arg-type]
    )
    return refresher, provider, resolved_store, resolved_clock


# --- The contract -------------------------------------------------------------------------------


def test_the_provider_double_satisfies_the_port() -> None:
    assert isinstance(_Provider(SimulatedClock(ANCHOR)), SymbolRulesProvider)


def test_the_refresher_satisfies_the_maintainer_port() -> None:
    refresher, _, _, _ = _refresher()

    assert isinstance(refresher, SymbolRulesMaintainer)


def test_no_unit_test_here_opens_a_socket() -> None:
    # Stated as an assertion rather than a comment: the provider is a double, and the count
    # it keeps is the only evidence a fetch ever happened.
    refresher, provider, _, clock = _refresher()
    clock.advance(timedelta(hours=7))

    refresher.maintain()

    assert provider.calls == 1


# --- Schedule validation ------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [0, -1, -3600])
def test_a_non_positive_interval_is_refused(interval: float) -> None:
    with pytest.raises(ConfigurationError, match="strictly positive"):
        _refresher(interval=interval)


@pytest.mark.parametrize("interval", [_ONE_DAY, _ONE_DAY + 1, 7 * _ONE_DAY])
def test_an_interval_at_or_past_the_staleness_budget_is_refused(interval: float) -> None:
    # The configuration that guarantees the outage this whole mechanism exists to prevent:
    # the rules would expire before the next refresh was even due.
    with pytest.raises(ConfigurationError, match="strictly below the staleness"):
        _refresher(interval=interval)


def test_a_staleness_budget_of_zero_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="staleness budget must be strictly positive"):
        _refresher(interval=60, stale_after=0)


def test_the_documented_default_pair_is_accepted() -> None:
    refresher, _, _, _ = _refresher(interval=_SIX_HOURS, stale_after=_ONE_DAY)

    assert refresher.telemetry().stale_after_seconds == _ONE_DAY


# --- Scheduling ---------------------------------------------------------------------------------


def test_nothing_is_fetched_before_the_interval_elapses() -> None:
    refresher, provider, _, clock = _refresher()

    clock.advance(timedelta(hours=5, minutes=59))
    refresher.maintain()

    assert provider.calls == 0
    assert refresher.is_due() is False


def test_a_refresh_happens_once_the_interval_elapses() -> None:
    refresher, provider, store, clock = _refresher()

    clock.advance(timedelta(hours=6))
    refresher.maintain()

    assert provider.calls == 1
    assert store.age_seconds(clock.now()) == 0.0


def test_rules_never_reach_the_staleness_budget_under_a_working_refresh() -> None:
    # The blocker, stated as the thing that must not happen again.
    refresher, _, store, clock = _refresher()

    for _ in range(28):  # seven days at six-hour steps
        clock.advance(timedelta(hours=6))
        refresher.maintain()
        assert store.age_seconds(clock.now()) < _ONE_DAY


def test_the_schedule_reads_only_the_injected_clock() -> None:
    # Seven simulated days pass in microseconds. If anything here consulted the wall clock
    # the ages below would all be near zero and the refreshes would never come due.
    refresher, provider, _, clock = _refresher()

    for day in range(1, 8):
        clock.advance(timedelta(days=1))
        refresher.maintain()
        assert provider.calls == day

    assert clock.now() == ANCHOR + timedelta(days=7)


def test_symbols_default_to_whatever_the_store_holds() -> None:
    store = SymbolRulesStore(
        {
            SYMBOL: make_symbol_rules(updated_at=ANCHOR),
            "ETH/USDT": make_symbol_rules(symbol="ETH/USDT", base_asset="ETH", updated_at=ANCHOR),
        }
    )
    refresher, provider, _, clock = _refresher(store=store)

    clock.advance(timedelta(hours=7))
    refresher.maintain()

    assert provider.requested == [("BTC/USDT", "ETH/USDT")]


# --- Failure ------------------------------------------------------------------------------------


def test_a_failed_refresh_keeps_the_last_known_good_rules() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})
    refresher, provider, _, clock = _refresher(store=store)
    provider.fail_with = DataProviderError("venue unreachable")

    clock.advance(timedelta(hours=7))
    refresher.maintain()

    assert store.current(SYMBOL).price_tick == Decimal("0.01")


def test_a_failed_refresh_does_not_reset_the_age() -> None:
    # The failure mode that would hide everything else: marking rules fresh because an
    # attempt was made would leave the risk engine trading on rules nobody re-read.
    refresher, provider, store, clock = _refresher()
    provider.fail_with = DataProviderError("throttled")

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.age_seconds == pytest.approx(7 * 3600)
    assert store.age_seconds(clock.now()) == pytest.approx(7 * 3600)


def test_a_failed_refresh_does_not_move_the_last_refresh_timestamp() -> None:
    refresher, provider, _, clock = _refresher()
    clock.advance(timedelta(hours=7))
    refresher.maintain()
    succeeded_at = refresher.telemetry().last_refresh_at

    provider.fail_with = DataProviderError("gone")
    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.last_refresh_at == succeeded_at


def test_a_refresh_failure_never_escapes_maintain() -> None:
    # A briefly unreachable venue is an ordinary event, not a reason to stop a week-long run.
    refresher, provider, _, clock = _refresher()
    provider.fail_with = DataProviderError("venue unreachable")

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.refresh_failures == 1


@pytest.mark.parametrize(
    "failure",
    [
        DataProviderError("unreachable"),
        ValueError("malformed document"),
        OSError("connection reset"),
    ],
)
def test_every_kind_of_failure_is_contained_identically(failure: Exception) -> None:
    refresher, provider, store, clock = _refresher()
    provider.fail_with = failure

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.consecutive_failures == 1
    assert set(store) == {SYMBOL}


def test_repeated_failures_let_the_rules_go_stale_rather_than_pretending() -> None:
    # The honest outcome. Refresh cannot rescue an unreachable venue, and the risk engine
    # remains the thing that decides whether trading may continue.
    refresher, provider, store, clock = _refresher()
    provider.fail_with = DataProviderError("still unreachable")

    for _ in range(8):
        clock.advance(timedelta(hours=6))
        refresher.maintain()

    reading = refresher.telemetry()
    assert store.age_seconds(clock.now()) > _ONE_DAY
    assert reading.is_stale is True
    assert reading.consecutive_failures == 8


def test_a_recovery_clears_the_consecutive_count_but_not_the_total() -> None:
    refresher, provider, _, clock = _refresher()
    provider.fail_with = DataProviderError("transient")
    clock.advance(timedelta(hours=7))
    refresher.maintain()

    provider.fail_with = None
    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.consecutive_failures == 0
    assert reading.refresh_failures == 1
    assert reading.refresh_successes == 1
    assert reading.is_refreshing is True


def test_a_replacement_that_would_drop_a_symbol_is_treated_as_a_failure() -> None:
    store = SymbolRulesStore(
        {
            SYMBOL: make_symbol_rules(updated_at=ANCHOR),
            "ETH/USDT": make_symbol_rules(symbol="ETH/USDT", base_asset="ETH", updated_at=ANCHOR),
        }
    )
    refresher = SymbolRulesRefresher(
        store=store,
        provider=_Provider(SimulatedClock(ANCHOR)),
        clock=(clock := SimulatedClock(ANCHOR)),
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
        symbols=[SYMBOL],
    )

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.refresh_failures == 1
    assert set(store) == {SYMBOL, "ETH/USDT"}


# --- Telemetry ----------------------------------------------------------------------------------


def test_telemetry_starts_from_the_startup_fetch() -> None:
    refresher, _, _, _ = _refresher()

    reading = refresher.telemetry()

    assert reading.refresh_attempts == 0
    assert reading.last_refresh_at == ANCHOR
    assert reading.age_seconds == 0.0
    assert reading.is_refreshing is True
    assert reading.is_stale is False


def test_every_named_counter_is_reported() -> None:
    refresher, provider, _, clock = _refresher()
    clock.advance(timedelta(hours=7))
    refresher.maintain()
    provider.fail_with = DataProviderError("down")
    clock.advance(timedelta(hours=7))

    reading = refresher.maintain()

    assert reading.refresh_attempts == 2
    assert reading.refresh_successes == 1
    assert reading.refresh_failures == 1
    assert reading.consecutive_failures == 1
    assert reading.last_refresh_at == ANCHOR + timedelta(hours=7)
    assert reading.age_seconds == pytest.approx(7 * 3600)
    assert reading.stale_after_seconds == _ONE_DAY
    assert "DataProviderError" in str(reading.last_failure_reason)


def test_the_age_reported_is_the_age_after_any_refresh_this_call_performed() -> None:
    refresher, _, _, clock = _refresher()

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.age_seconds == 0.0


def test_a_venue_rule_change_is_counted() -> None:
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})
    clock = SimulatedClock(ANCHOR)
    provider = _Provider(clock, price_tick=Decimal("0.5"))
    refresher = SymbolRulesRefresher(
        store=store,
        provider=provider,
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
    )

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.rule_changes == 1
    assert store.current(SYMBOL).price_tick == Decimal("0.5")


def test_an_unchanged_refetch_is_not_counted_as_a_change() -> None:
    refresher, _, _, clock = _refresher()

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.rule_changes == 0
    assert reading.refresh_successes == 1


# --- Working orders ------------------------------------------------------------------------------


def _working_order(quantity: Decimal) -> Order:
    return make_order(quantity=quantity, order_type=OrderType.LIMIT, limit_price=Decimal(50_000))


def test_a_working_order_breaching_refreshed_rules_is_reported_and_left_alone() -> None:
    # Recorded, never repaired: rewriting a live order from a metadata refresh is an
    # execution decision nobody asked for and nobody could audit.
    order = _working_order(Decimal("0.00005"))
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(quantity_step=Decimal("0.00001"))})
    clock = SimulatedClock(ANCHOR)
    provider = _Provider(clock, quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"))
    refresher = SymbolRulesRefresher(
        store=store,
        provider=provider,
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
        open_orders=lambda: (order,),
    )

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.working_order_conflicts == 1
    assert order.quantity == Decimal("0.00005")
    assert order.status is OrderStatus.OPEN


def test_a_working_order_that_still_satisfies_the_new_rules_is_not_flagged() -> None:
    order = _working_order(Decimal("0.5"))
    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})
    clock = SimulatedClock(ANCHOR)
    provider = _Provider(clock, price_tick=Decimal("0.5"))
    refresher = SymbolRulesRefresher(
        store=store,
        provider=provider,
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
        open_orders=lambda: (order,),
    )

    clock.advance(timedelta(hours=7))

    assert refresher.maintain().working_order_conflicts == 0


def test_working_orders_are_not_consulted_when_nothing_changed() -> None:
    calls = 0

    def _orders() -> tuple[Order, ...]:
        nonlocal calls
        calls += 1
        return ()

    refresher, _, _, clock = _refresher(open_orders=_orders)
    clock.advance(timedelta(hours=7))
    refresher.maintain()

    assert calls == 0


def test_a_broken_order_source_cannot_break_a_refresh() -> None:
    def _explode() -> tuple[Order, ...]:
        raise RuntimeError("broker is busy")

    store = SymbolRulesStore({SYMBOL: make_symbol_rules(price_tick=Decimal("0.01"))})
    clock = SimulatedClock(ANCHOR)
    refresher = SymbolRulesRefresher(
        store=store,
        provider=_Provider(clock, price_tick=Decimal("0.5")),
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
        open_orders=_explode,
    )

    clock.advance(timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.refresh_successes == 1
    assert store.current(SYMBOL).price_tick == Decimal("0.5")
