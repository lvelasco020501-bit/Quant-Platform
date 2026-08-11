"""Seven days of trading against a rulebook that keeps being re-read.

The blocker this file exists for: venue rules were fetched once at startup, the risk engine
refuses anything sized against rules older than a day, and a week-long run therefore traded
for one day and refused everything for six. Nothing about that was visible in a report.

Every day here is simulated. A test that waited a week to find this out would be a test
nobody ran, so the clock is injected and 168 hours pass in milliseconds — which is also the
only way to assert the *negative*: that ``symbol_rules_freshness`` never once blocked a
decision across the whole run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from quantplatform.backtesting.results import BarOutcome
from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import (
    MarketType,
    PositionState,
    RiskCheckCode,
    RiskCheckStatus,
    SignalAction,
    Timeframe,
)
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.risk import RiskDecision
from quantplatform.core.models.signals import Signal, StrategyContext
from quantplatform.core.models.strategy import StrategyMetadata
from quantplatform.core.symbol_rules import SymbolRulesStore
from quantplatform.marketdata.symbol_rules import BinanceSpotSymbolRulesProvider
from quantplatform.orchestration.symbol_rules import SymbolRulesRefresher
from quantplatform.strategies.base import BaseStrategy
from tests.factories import ANCHOR, SYMBOL, make_backtest, make_bars, make_symbol_rules
from tests.unit.test_symbol_rules import _document as _exchange_info_document

_SIX_HOURS = 6 * 3600
_ONE_DAY = 24 * 3600
_WEEK_OF_HOURS = 24 * 7


class _Params(BaseModel):
    """This strategy takes no parameters."""


class _AlwaysTrading(BaseStrategy):
    """Buys once warm-up is over, sells whenever it is holding: constant order flow.

    Constant flow is the point. A strategy that traded twice would leave most of the week
    unobserved, and the whole question is whether an intent raised on day six is still
    allowed to reach the broker.
    """

    METADATA: ClassVar[StrategyMetadata] = StrategyMetadata(
        strategy_id="always_trading",
        version="1.0.0",
        name="always_trading",
        description="Deterministic strategy that keeps order flow alive for a week.",
        required_history=3,
        required_features=(),
        supported_timeframes=(Timeframe.H1,),
        supported_market_types=(MarketType.SPOT,),
        parameter_schema=_Params,
        operates_intrabar=False,
        allows_short=False,
    )

    def generate(self, context: StrategyContext) -> Sequence[Signal]:
        if context.history_length < 3:
            return ()
        action = (
            SignalAction.EXIT_LONG
            if context.position_state is PositionState.LONG
            else SignalAction.ENTER_LONG
        )
        return (
            self.build_signal(
                context=context,
                action=action,
                confidence=Decimal("0.9"),
                reason="keeping order flow alive",
            ),
        )


class _Venue:
    """A metadata endpoint that answers instantly and can be told to change or fail."""

    def __init__(self, clock: SimulatedClock) -> None:
        self.clock = clock
        self.calls = 0
        self.fail_with: Exception | None = None
        self.overrides: dict[str, Any] = {}

    def fetch(self, symbols: Sequence[str]) -> Mapping[str, SymbolRules]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return {
            symbol: make_symbol_rules(symbol=symbol, updated_at=self.clock.now(), **self.overrides)
            for symbol in symbols
        }


class _Week:
    """One week of hourly bars driven through the real pipeline, refresher and all."""

    def __init__(self, *, refresh: bool, hours: int = _WEEK_OF_HOURS) -> None:
        self.clock = SimulatedClock(ANCHOR)
        self.venue = _Venue(self.clock)
        self.store = SymbolRulesStore({SYMBOL: make_symbol_rules(updated_at=ANCHOR)})
        self.engine, self.broker, self.portfolio = make_backtest(
            strategy=_AlwaysTrading(_Params()),
            symbols=self.store,
            max_orders_per_day=10_000,
            max_orders_per_hour=1_000,
        )
        self.refresher = (
            SymbolRulesRefresher(
                store=self.store,
                provider=self.venue,
                clock=self.clock,
                refresh_interval_seconds=_SIX_HOURS,
                stale_after_seconds=_ONE_DAY,
                open_orders=self.broker.open_orders,
            )
            if refresh
            else None
        )
        self.bars = make_bars([Decimal(50_000)] * hours)
        self.outcomes: list[BarOutcome] = []

    def run(self) -> _Week:
        """Drive the pipeline exactly as the paper runner does: maintain, then submit."""
        state = self.engine.begin()
        for bar in self.bars:
            # The clock tracks the candle, which is what makes a week pass in milliseconds.
            self.clock.set_time(bar.close_time)
            if self.refresher is not None:
                self.refresher.maintain()
            self.outcomes.append(self.engine.advance(bar, state))
        return self

    @property
    def decisions(self) -> tuple[RiskDecision, ...]:
        return tuple(decision for outcome in self.outcomes for decision in outcome.decisions)

    def stale_rejections(self) -> tuple[RiskDecision, ...]:
        """Return every decision refused specifically for stale venue rules."""
        return tuple(
            decision
            for decision in self.decisions
            for check in decision.checks
            if check.code is RiskCheckCode.SYMBOL_RULES_FRESHNESS
            and check.status is RiskCheckStatus.FAILED
        )

    def fills_after(self, hour: int) -> int:
        return sum(
            len(outcome.fills)
            for outcome in self.outcomes
            if outcome.bar.close_time >= ANCHOR + timedelta(hours=hour)
        )


# --- The blocker, and its absence ----------------------------------------------------------------


def test_without_refresh_a_week_stops_trading_after_the_first_day() -> None:
    # The defect, pinned so it cannot come back quietly. Kept as the control case: the
    # assertions below mean nothing unless this one still fails the way it used to.
    week = _Week(refresh=False).run()

    assert week.decisions != ()
    assert week.stale_rejections() != ()
    # Nothing fills once the rules pass the budget. The boundary is not exactly hour 24 —
    # an order approved on the last good bar may still fill on the next one — so this asks
    # the question that matters: is the run dead well before day seven?
    assert week.fills_after(hour=30) == 0


def test_seven_days_of_refresh_never_block_a_decision_for_stale_rules() -> None:
    week = _Week(refresh=True).run()

    assert len(week.bars) == _WEEK_OF_HOURS
    assert week.decisions != ()
    assert week.stale_rejections() == ()


def test_trading_continues_past_the_first_day() -> None:
    # Not merely "no stale rejections": orders actually reach the broker on day seven.
    week = _Week(refresh=True).run()

    assert week.fills_after(hour=25) > 0
    assert week.fills_after(hour=_WEEK_OF_HOURS - 24) > 0


def test_the_rules_never_reach_the_freshness_budget_across_the_week() -> None:
    week = _Week(refresh=True).run()

    assert week.store.age_seconds(week.clock.now()) < _ONE_DAY


def test_a_week_costs_one_fetch_every_six_hours_and_no_more() -> None:
    # A refresh loop that hammered a public endpoint every bar would be throttled long
    # before day seven, so the cadence is part of the contract rather than an optimisation.
    week = _Week(refresh=True).run()

    assert week.venue.calls == _WEEK_OF_HOURS // 6


def test_the_whole_week_runs_on_the_injected_clock() -> None:
    week = _Week(refresh=True).run()

    assert week.clock.now() == ANCHOR + timedelta(hours=_WEEK_OF_HOURS)


# --- Consistency between components ---------------------------------------------------------------


def test_risk_and_broker_read_the_same_refreshed_snapshot() -> None:
    # The property that makes divergence unrepresentable. All three components were handed
    # the same store, so there is no copy to fall behind.
    week = _Week(refresh=True)
    week.venue.overrides = {"price_tick": Decimal("0.5")}

    week.clock.advance(timedelta(hours=7))
    week.refresher is not None and week.refresher.maintain()

    updated = week.store.current(SYMBOL)
    assert updated.price_tick == Decimal("0.5")
    assert week.broker._symbols.current(SYMBOL) is updated
    assert week.engine._symbols.current(SYMBOL) is updated
    assert week.portfolio._symbols.current(SYMBOL) is updated


def test_an_updated_price_tick_reaches_later_risk_decisions() -> None:
    week = _Week(refresh=True, hours=12)
    week.venue.overrides = {"price_tick": Decimal("0.5")}

    week.run()

    assert week.store.current(SYMBOL).price_tick == Decimal("0.5")
    assert week.decisions != ()


def test_an_updated_quantity_step_reaches_later_sizing() -> None:
    # Sizing rounds to the step in force at the moment of the decision, so a coarser step
    # arriving mid-run must show up in the quantities approved after it — and only after it.
    week = _Week(refresh=True, hours=30)
    week.venue.overrides = {
        "quantity_step": Decimal("0.01"),
        "min_quantity": Decimal("0.01"),
    }

    week.run()

    changed_at = ANCHOR + timedelta(hours=6)
    before = _approved_quantities(week, until=changed_at)
    after = _approved_quantities(week, since=changed_at)

    assert before != []
    assert after != []
    assert any(quantity % Decimal("0.01") != 0 for quantity in before)
    assert all(quantity % Decimal("0.01") == 0 for quantity in after)


def test_orders_approved_before_a_rule_change_are_not_rewritten_afterwards() -> None:
    # The venue's new limits are authoritative for what happens next, never backwards. An
    # order already approved under the old step keeps the quantity it was approved with,
    # because a record that quietly re-rounds itself is a record nobody can audit.
    week = _Week(refresh=True, hours=30)
    week.venue.overrides = {
        "quantity_step": Decimal("0.01"),
        "min_quantity": Decimal("0.01"),
    }

    week.run()

    historic = _approved_quantities(week, until=ANCHOR + timedelta(hours=6))

    assert historic != []
    assert any(quantity % Decimal("0.01") != 0 for quantity in historic)


def _approved_quantities(
    week: _Week, *, since: datetime | None = None, until: datetime | None = None
) -> list[Decimal]:
    """Return the quantities risk approved inside a window."""
    return [
        decision.approved_order.quantity
        for decision in week.decisions
        if decision.approved_order is not None
        and (since is None or decision.decided_at >= since)
        and (until is None or decision.decided_at < until)
    ]


# --- Failure across a week --------------------------------------------------------------------


def test_an_outage_lasting_past_the_budget_stops_trading_exactly_as_before() -> None:
    # Refresh cannot rescue an unreachable venue and does not pretend to. The risk engine
    # remains the only thing deciding whether trading may continue.
    week = _Week(refresh=True)
    assert week.refresher is not None
    week.venue.fail_with = OSError("venue unreachable for the whole week")

    week.run()

    assert week.stale_rejections() != ()
    assert week.refresher.telemetry().is_stale is True
    assert week.refresher.telemetry().consecutive_failures > 0


def test_a_recovered_outage_resumes_trading() -> None:
    week = _Week(refresh=True, hours=48)
    assert week.refresher is not None
    state = week.engine.begin()
    for bar in week.bars:
        week.clock.set_time(bar.close_time)
        # Down for the first eighteen hours, back before the budget expires.
        week.venue.fail_with = (
            OSError("temporarily unreachable")
            if bar.close_time < ANCHOR + timedelta(hours=18)
            else None
        )
        week.refresher.maintain()
        week.outcomes.append(week.engine.advance(bar, state))

    telemetry = week.refresher.telemetry()
    assert telemetry.refresh_failures > 0
    assert telemetry.consecutive_failures == 0
    assert week.stale_rejections() == ()
    assert week.fills_after(hour=30) > 0


@pytest.mark.parametrize("hours", [24, 72, _WEEK_OF_HOURS])
def test_no_run_length_reintroduces_the_stale_rejection(hours: int) -> None:
    week = _Week(refresh=True, hours=hours).run()

    assert week.stale_rejections() == ()


# --- The real provider inside the refresh loop --------------------------------------------------
#
# Everything above drives the refresher with a venue double. That is what let the original
# defect through: the double always answered with freshly stamped rules, so the loop looked
# correct while the real provider was memoising its first answer for the life of the process.
# These tests close that gap by wiring the production provider itself.


class _CountingTransport:
    """The real provider's transport, counting how often the venue is actually read."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def fetch(self, url: str) -> str:
        self.calls += 1
        _ = url
        return self.payload


def _real_provider(
    clock: SimulatedClock,
) -> tuple[BinanceSpotSymbolRulesProvider, _CountingTransport]:
    transport = _CountingTransport(_exchange_info_document())
    return BinanceSpotSymbolRulesProvider(clock=clock, transport=transport), transport


def test_the_real_provider_reads_the_venue_on_every_refresh() -> None:
    # The exact scenario that failed in production: one HTTP request in two days, eight
    # "successful" refreshes, and rules that never got any younger.
    clock = SimulatedClock(ANCHOR)
    provider, transport = _real_provider(clock)
    store = SymbolRulesStore(provider.fetch([SYMBOL]))
    refresher = SymbolRulesRefresher(
        store=store,
        provider=provider,
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
    )

    for hour in range(6, 49, 6):
        clock.set_time(ANCHOR + timedelta(hours=hour))
        refresher.maintain()

    assert transport.calls == 1 + 8


def test_a_successful_refresh_moves_updated_at_forward() -> None:
    clock = SimulatedClock(ANCHOR)
    provider, _ = _real_provider(clock)
    store = SymbolRulesStore(provider.fetch([SYMBOL]))
    refresher = SymbolRulesRefresher(
        store=store,
        provider=provider,
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
    )
    before = store.current(SYMBOL).updated_at

    clock.set_time(ANCHOR + timedelta(hours=7))
    refresher.maintain()

    assert store.current(SYMBOL).updated_at == ANCHOR + timedelta(hours=7)
    assert store.current(SYMBOL).updated_at > before


def test_a_successful_refresh_returns_the_age_to_zero() -> None:
    # The measurement that was climbing without limit in the aborted run.
    clock = SimulatedClock(ANCHOR)
    provider, _ = _real_provider(clock)
    store = SymbolRulesStore(provider.fetch([SYMBOL]))
    refresher = SymbolRulesRefresher(
        store=store,
        provider=provider,
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
    )

    clock.set_time(ANCHOR + timedelta(hours=7))
    reading = refresher.maintain()

    assert reading.age_seconds == 0.0
    assert store.age_seconds(clock.now()) == 0.0


def test_the_age_never_reaches_the_budget_across_seven_real_days() -> None:
    clock = SimulatedClock(ANCHOR)
    provider, _ = _real_provider(clock)
    store = SymbolRulesStore(provider.fetch([SYMBOL]))
    refresher = SymbolRulesRefresher(
        store=store,
        provider=provider,
        clock=clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
    )

    worst = 0.0
    for hour in range(1, _WEEK_OF_HOURS + 1):
        clock.set_time(ANCHOR + timedelta(hours=hour))
        reading = refresher.maintain()
        worst = max(worst, reading.age_seconds)
        assert reading.is_stale is False

    # Bars arrive hourly and the interval is six hours, so the rules are never older than
    # one bar past the interval. Well inside the twenty-four hour budget.
    assert worst <= _SIX_HOURS + 3600


def test_risk_never_refuses_for_stale_rules_across_a_week_on_the_real_provider() -> None:
    # The end-to-end statement: production provider, production refresher, production risk
    # engine, seven days of bars, and not one refusal for symbol_rules_freshness.
    week = _Week(refresh=False)
    provider, _ = _real_provider(week.clock)
    week.store.replace(provider.fetch([SYMBOL]))
    week.refresher = SymbolRulesRefresher(
        store=week.store,
        provider=provider,
        clock=week.clock,
        refresh_interval_seconds=_SIX_HOURS,
        stale_after_seconds=_ONE_DAY,
        open_orders=week.broker.open_orders,
    )

    week.run()

    assert week.decisions != ()
    assert week.stale_rejections() == ()
    assert week.fills_after(hour=_WEEK_OF_HOURS - 24) > 0
