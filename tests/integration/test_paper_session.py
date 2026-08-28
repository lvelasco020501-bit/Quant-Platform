"""Phase 6: a paper session driving the real pipeline against a simulated live feed.

Every component below the session is the production class — features, strategy, risk, broker,
portfolio. The feed is a double because the point of a paper session is that it cannot tell a
real feed from a replayed one; if it could, the mode would prove nothing about live behaviour.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import StopKind
from quantplatform.core.errors import DataIntegrityError, PaperSessionStateError
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.portfolio import Balance
from quantplatform.core.models.risk import PositionRiskState, StopSpecification
from quantplatform.core.models.telemetry import SymbolRulesTelemetry
from quantplatform.paper import (
    InMemoryPaperStateRepository,
    PaperTradingRunner,
    PaperTradingSession,
)
from quantplatform.storage.paper_state import _COMPUTED_FIELD_EXCLUSIONS
from tests.factories import ANCHOR, SYMBOL, make_backtest, make_bar, make_bars
from tests.integration.test_backtest_engine import (
    _WARMUP_BARS,
    BuyOnce,
    BuyThenSell,
    Silent,
    _flat_bars,
    _Params,
)


class _ReplayFeed:
    """Yields prepared bars as though a venue had just published them.

    Advances the injected clock to each bar's close before yielding, which is what a real feed
    does implicitly by taking wall-clock time to deliver one.
    """

    def __init__(self, bars: tuple[MarketBar, ...], clock: SimulatedClock) -> None:
        self._bars = bars
        self._clock = clock
        self.closed = False

    @property
    def symbols(self) -> tuple[str, ...]:
        return (SYMBOL,)

    def closed_bars(self) -> Iterator[MarketBar]:
        for bar in self._bars:
            self._clock.set_time(bar.close_time)
            yield bar

    def close(self) -> None:
        self.closed = True


def _session(
    *,
    strategy: object | None = None,
    clock: SimulatedClock | None = None,
    repository: InMemoryPaperStateRepository | None = None,
    **backtest_kwargs: object,
) -> tuple[PaperTradingSession, SimulatedClock, InMemoryPaperStateRepository, object]:
    """Wire a session over the real pipeline."""
    resolved_clock = clock if clock is not None else SimulatedClock(ANCHOR)
    resolved_repository = repository if repository is not None else InMemoryPaperStateRepository()
    engine, broker, portfolio = make_backtest(
        strategy=strategy if strategy is not None else BuyOnce(_Params()),  # type: ignore[arg-type]
        **backtest_kwargs,  # type: ignore[arg-type]
    )
    session = PaperTradingSession(
        session_id="paper-1",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=resolved_clock,
        state_repository=resolved_repository,
    )
    return session, resolved_clock, resolved_repository, portfolio


def _feed_bars(
    session: PaperTradingSession, clock: SimulatedClock, bars: tuple[MarketBar, ...]
) -> None:
    """Deliver bars as a live feed would, advancing the clock past each close."""
    for bar in bars:
        clock.set_time(bar.close_time)
        session.submit_bar(bar)


# --- Lifecycle -------------------------------------------------------------------------------


def test_a_session_starts_stops_and_reports_its_status() -> None:
    session, _, _, _ = _session(strategy=Silent(_Params()))

    started = session.start()
    assert started.running is True
    assert started.started_at == ANCHOR
    assert session.is_running is True

    stopped = session.stop()
    assert stopped.running is False
    assert stopped.stopped_at is not None
    assert session.is_running is False


def test_starting_twice_is_refused() -> None:
    session, _, _, _ = _session(strategy=Silent(_Params()))
    session.start()

    with pytest.raises(PaperSessionStateError, match="already running"):
        session.start()


def test_stopping_a_stopped_session_is_not_an_error() -> None:
    session, _, _, _ = _session(strategy=Silent(_Params()))
    session.start()
    session.stop()

    assert session.stop().running is False


def test_a_bar_offered_to_a_stopped_session_is_refused() -> None:
    session, clock, _, _ = _session(strategy=Silent(_Params()))
    bar = make_bar(index=0)
    clock.set_time(bar.close_time)

    with pytest.raises(PaperSessionStateError, match="not running"):
        session.submit_bar(bar)


# --- Closed-candle discipline -----------------------------------------------------------------


def test_a_bar_that_has_not_closed_yet_is_refused_without_stopping_the_session() -> None:
    session, _, _, _ = _session(strategy=Silent(_Params()))
    session.start()
    bar = make_bar(index=0)
    # The clock has not reached the bar's close, so the candle is still forming.

    assert session.submit_bar(bar) is None
    metrics = session.runtime_metrics()
    assert metrics.bars_received == 1
    assert metrics.bars_processed == 0
    assert metrics.bars_rejected == 1
    assert session.is_running is True


def test_an_explicitly_open_bar_is_refused() -> None:
    session, clock, _, _ = _session(strategy=Silent(_Params()))
    session.start()
    bar = make_bar(index=0, is_closed=False)
    clock.set_time(bar.close_time + timedelta(hours=1))

    assert session.submit_bar(bar) is None
    assert session.runtime_metrics().bars_processed == 0


def test_a_repeated_bar_is_refused_rather_than_traded_twice() -> None:
    session, clock, _, _ = _session(strategy=Silent(_Params()))
    session.start()
    bar = make_bar(index=0)
    clock.set_time(bar.close_time)

    assert session.submit_bar(bar) is not None
    assert session.submit_bar(bar) is None
    assert session.runtime_metrics().bars_processed == 1


def test_an_unknown_symbol_is_a_configuration_error_not_a_feed_hiccup() -> None:
    session, clock, _, _ = _session(strategy=Silent(_Params()))
    session.start()
    bar = make_bar(index=0, symbol="ETH/USDT")
    clock.set_time(bar.close_time)

    with pytest.raises(DataIntegrityError, match="venue rules"):
        session.submit_bar(bar)


# --- The pipeline runs unchanged -----------------------------------------------------------------


def test_a_paper_session_runs_the_full_chain_and_settles_virtually() -> None:
    session, clock, _, portfolio = _session()
    session.start()

    _feed_bars(session, clock, _flat_bars(5))

    metrics = session.runtime_metrics()
    assert metrics.bars_processed == 5
    assert metrics.signals_generated == 1
    assert metrics.orders_submitted == 1
    assert metrics.fills_received == 1
    assert portfolio.positions()[0].quantity > Decimal(0)


def test_execution_remains_next_bar_in_paper_mode() -> None:
    # The same guarantee the backtester makes: a decision from one close settles at the next
    # bar's open, never at a price that printed before the strategy saw it.
    session, clock, _, _ = _session()
    session.start()

    _feed_bars(session, clock, _flat_bars(5))

    result = session.result()
    assert result.detail is not None
    decided = _WARMUP_BARS - 1
    assert result.detail.bars[decided].signals != ()
    assert result.detail.bars[decided].fills == ()
    assert result.detail.bars[decided + 1].fills != ()


def test_a_paper_run_matches_the_backtest_over_the_same_bars() -> None:
    # The strongest statement Phase 6 can make: identical bars through the identical chain
    # produce identical trading, whether they arrive all at once or one at a time.
    bars = _flat_bars(6)
    engine, _, _ = make_backtest(strategy=BuyThenSell(_Params()))
    expected = engine.run(bars)

    session, clock, _, _ = _session(strategy=BuyThenSell(_Params()))
    session.start()
    _feed_bars(session, clock, bars)
    actual = session.result()

    assert actual.detail is not None
    assert [fill.fill_id for fill in actual.fills] == [fill.fill_id for fill in expected.fills]
    assert actual.detail.equity_curve == expected.equity_curve
    assert actual.performance == expected.performance


def test_the_session_never_reaches_a_real_venue() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "quantplatform" / "paper"
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in ("requests", "httpx", "websocket", "aiohttp", "ccxt", "urllib"):
            assert token not in source, f"{path.name} references {token}"


# --- Snapshots and reporting ----------------------------------------------------------------------


def test_a_snapshot_reports_the_account_and_the_runtime() -> None:
    session, clock, _, _ = _session()
    session.start()
    _feed_bars(session, clock, _flat_bars(5))

    snapshot = session.snapshot()

    assert snapshot.status.running is True
    assert snapshot.equity > Decimal(0)
    assert snapshot.last_bar is not None
    assert snapshot.runtime.bars_processed == 5
    assert snapshot.open_position_count == 1


def test_a_result_can_be_read_while_the_session_is_still_running() -> None:
    session, clock, _, _ = _session()
    session.start()
    _feed_bars(session, clock, _flat_bars(4))

    mid = session.result()
    _feed_bars(session, clock, make_bars([Decimal(50_000)] * 2)[:0])

    assert mid.status.running is True
    assert mid.runtime.bars_processed == 4
    assert mid.performance is not None


def test_a_session_with_no_bars_yet_reports_an_untouched_account() -> None:
    # Consistent with an empty backtest: the summary exists and says nothing happened, rather
    # than being absent and forcing every caller to special-case a just-started session.
    session, _, _, _ = _session(strategy=Silent(_Params()))
    session.start()

    result = session.result()

    assert result.performance is not None
    assert result.performance.bars_processed == 0
    assert result.performance.final_equity == result.snapshot.equity
    assert result.snapshot.last_bar is None
    assert result.runtime.bars_processed == 0


# --- Persistence, restart and resume ----------------------------------------------------------


def test_state_is_persisted_after_every_processed_bar() -> None:
    session, clock, repository, _ = _session(strategy=Silent(_Params()))
    session.start()

    _feed_bars(session, clock, _flat_bars(3))

    stored = repository.load("paper-1")
    assert stored is not None
    assert stored.bars_processed == 3
    assert stored.last_bar is not None
    assert session.runtime_metrics().state_saves >= 3


def test_a_session_resumes_from_its_stored_snapshot() -> None:
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()

    # A new process: fresh components, same repository and session id.
    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    status = second.resume()

    assert status.running is True
    assert status.started_at == ANCHOR
    assert status.restarts == 1
    assert second.runtime_metrics().bars_processed == 3


def test_a_resumed_session_refuses_bars_it_already_lived_through() -> None:
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    bars = _flat_bars(5)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, bars[:3])
    first.stop()

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    second.resume()

    # Replaying the already-processed bars must not re-trade them.
    for bar in bars[:3]:
        assert second.submit_bar(bar) is None
    clock.set_time(bars[3].close_time)
    assert second.submit_bar(bars[3]) is not None
    assert second.runtime_metrics().bars_processed == 4


def test_resuming_without_a_repository_is_refused() -> None:
    engine, broker, portfolio = make_backtest(strategy=Silent(_Params()))
    session = PaperTradingSession(
        session_id="paper-1",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=SimulatedClock(ANCHOR),
        state_repository=None,
    )

    with pytest.raises(PaperSessionStateError, match="without a state repository"):
        session.resume()


def test_resuming_an_unknown_session_is_refused() -> None:
    session, _, _, _ = _session(strategy=Silent(_Params()))

    with pytest.raises(PaperSessionStateError, match="no stored state"):
        session.resume()


def test_resuming_a_running_session_is_refused() -> None:
    session, _, _, _ = _session(strategy=Silent(_Params()))
    session.start()

    with pytest.raises(PaperSessionStateError, match="cannot resume a running"):
        session.resume()


def test_resuming_a_session_holding_an_open_position_is_refused_loudly() -> None:
    # A known limitation, surfaced rather than papered over: the portfolio engine is
    # flat-start, so an open position cannot be restored without bypassing its invariant.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(5))
    first.stop()

    second, _, _, _ = _session(clock=clock, repository=repository)

    with pytest.raises(PaperSessionStateError, match="flat-start"):
        second.resume()


# --- Runner ---------------------------------------------------------------------------------------


def test_the_runner_drives_a_session_from_a_feed_and_closes_it() -> None:
    clock = SimulatedClock(ANCHOR)
    session, _, _, portfolio = _session(clock=clock)
    feed = _ReplayFeed(_flat_bars(5), clock)
    runner = PaperTradingRunner(session=session, feed=feed)

    result = runner.run()

    assert result.runtime.bars_processed == 5
    assert result.status.running is False
    assert feed.closed is True
    assert portfolio.positions()[0].quantity > Decimal(0)


def test_the_runner_honours_a_bar_limit() -> None:
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    runner = PaperTradingRunner(
        session=session, feed=_ReplayFeed(_flat_bars(10), clock), max_bars=4
    )

    result = runner.run()

    assert result.runtime.bars_received == 4


def test_the_runner_stops_cooperatively_after_the_current_bar() -> None:
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    runner = PaperTradingRunner(session=session, feed=_ReplayFeed(_flat_bars(10), clock))
    seen: list[MarketBar] = []

    def observe(bar: MarketBar) -> None:
        seen.append(bar)
        if len(seen) == 3:
            runner.request_stop()

    runner._on_bar = observe
    result = runner.run()

    assert len(seen) == 3
    assert result.runtime.bars_processed == 3


def test_the_runner_closes_the_feed_even_when_a_bar_fails() -> None:
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    bad = make_bar(index=0, symbol="ETH/USDT")
    feed = _ReplayFeed((bad,), clock)
    runner = PaperTradingRunner(session=session, feed=feed)

    with pytest.raises(DataIntegrityError):
        runner.run()

    assert feed.closed is True
    assert session.is_running is False


def test_the_runner_can_resume_an_existing_session() -> None:
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    bars = _flat_bars(6)

    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    PaperTradingRunner(session=first, feed=_ReplayFeed(bars[:3], clock)).run()

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    result = PaperTradingRunner(session=second, feed=_ReplayFeed(bars[3:], clock)).run(resume=True)

    assert result.status.restarts == 1
    assert result.runtime.bars_processed == 6


def test_run_once_offers_a_single_bar_without_owning_the_loop() -> None:
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    runner = PaperTradingRunner(session=session, feed=_ReplayFeed((), clock))
    session.start()
    bar = make_bar(index=0)
    clock.set_time(bar.close_time)

    runner.run_once(bar)

    assert session.runtime_metrics().bars_processed == 1


def test_run_once_refuses_when_the_session_is_not_running() -> None:
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    runner = PaperTradingRunner(session=session, feed=_ReplayFeed((), clock))

    with pytest.raises(PaperSessionStateError, match="not running"):
        runner.run_once(make_bar(index=0))


# --- Venue rules maintenance ------------------------------------------------------------------


class _Maintainer:
    """Records that it was asked, and answers with whatever reading it was given."""

    def __init__(self, reading: SymbolRulesTelemetry | None = None) -> None:
        self.calls = 0
        self.reading = reading if reading is not None else SymbolRulesTelemetry()

    def maintain(self) -> SymbolRulesTelemetry:
        self.calls += 1
        return self.reading


def test_the_runner_maintains_the_venue_rules_once_per_bar() -> None:
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    maintainer = _Maintainer()
    runner = PaperTradingRunner(
        session=session, feed=_ReplayFeed(_flat_bars(5), clock), symbol_rules=maintainer
    )

    runner.run()

    assert maintainer.calls == 5


def test_the_reading_reaches_the_session_and_its_result() -> None:
    # Carried, never consulted. It exists so a daily report can say whether the rulebook is
    # still being re-read; the session itself must not act on it.
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    reading = SymbolRulesTelemetry(
        refresh_attempts=3,
        refresh_successes=3,
        last_refresh_at=ANCHOR,
        age_seconds=1800.0,
        stale_after_seconds=86_400,
    )
    runner = PaperTradingRunner(
        session=session,
        feed=_ReplayFeed(_flat_bars(3), clock),
        symbol_rules=_Maintainer(reading),
    )

    result = runner.run()

    assert session.symbol_rules_telemetry == reading
    assert result.symbol_rules == reading


def test_a_session_without_maintenance_reports_nothing_rather_than_zeros() -> None:
    # The distinction reporting depends on: nobody refreshing is not the same as refreshing
    # successfully, and a run with no loop will stop trading once the budget expires.
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)
    runner = PaperTradingRunner(session=session, feed=_ReplayFeed(_flat_bars(3), clock))

    result = runner.run()

    assert result.symbol_rules is None


def test_maintenance_happens_before_the_bar_is_submitted() -> None:
    # A refresh landing after the bar would size that bar's order against the rules the
    # previous candle happened to see, and a day rollover would report the new day's reading
    # in the old day's closing report.
    order: list[str] = []
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock)

    class _Recording(_Maintainer):
        def maintain(self) -> SymbolRulesTelemetry:
            order.append("maintain")
            return super().maintain()

    runner = PaperTradingRunner(
        session=session,
        feed=_ReplayFeed(_flat_bars(2), clock),
        on_bar=lambda _: order.append("bar"),
        symbol_rules=_Recording(),
    )

    runner.run()

    assert order == ["maintain", "bar", "maintain", "bar"]


# --- Resume: financial continuity (SEV-1, incident of 2026-08-20) ------------------------------
#
# The contract these pin down was stated at the very bottom of the stack and never fully
# enforced at the top of it. SpotPortfolioEngine's docstring says seeding anything other than
# flat "is out of scope ... this engine does not invent one", and the README repeats it for the
# open-position case. But the engine is rebuilt from configured starting capital on every
# resume, so *every* financial figure that engine owns — balances, reservations, realised PnL,
# fees — is silently reset, while the session's own counters (bars_processed, restarts,
# last_bar) are faithfully restored and go on claiming this is the same run.
#
# The incident: a session was interrupted holding 9509.13 USDT reserved against a working GTC
# buy that had been approved but never filled. Nothing about that state is recoverable — the
# order lived only in the broker's memory — and nothing refused to resume from it either.
#
# Each test below asserts the *only* two acceptable outcomes: exact restoration, or a loud
# refusal before a single bar is processed. "Resumes, but quietly loses money" is the third
# option that must not exist.


def _stored_with(
    repository: InMemoryPaperStateRepository,
    session_id: str = "paper-1",
    **overrides: object,
) -> None:
    """Rewrite the stored snapshot with a hand-built financial state.

    Building the state directly is what lets a test describe an interruption that is
    genuinely hard to reach through the pipeline — a crash between an order being reserved
    and its fill — without staging an elaborate scenario whose own machinery could be what
    the assertion ends up proving.
    """
    stored = repository.load(session_id)
    assert stored is not None
    repository.save(stored.model_copy(update=overrides))


def _quote_balance(free: str, locked: str, at: datetime | None = None) -> Balance:
    return Balance(
        asset="USDT",
        free=Decimal(free),
        locked=Decimal(locked),
        updated_at=at if at is not None else ANCHOR,
    )


def test_resuming_a_flat_untouched_account_is_allowed() -> None:
    # The one case the flat-start engine can rebuild exactly: nothing financial has happened,
    # so a fresh engine and the stored snapshot describe the same account.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    status = second.resume()

    assert status.running is True
    assert second.runtime_metrics().bars_processed == 3


def test_resuming_after_realised_pnl_is_refused() -> None:
    # A closed round trip leaves realised PnL the rebuilt engine starts at zero. Resuming
    # would report a fresh account while the counters insist it is the same week-old session.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()
    _stored_with(repository, realized_pnl=Decimal("125.50"))

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)

    with pytest.raises(PaperSessionStateError, match="realised pnl"):
        second.resume()


def test_resuming_after_fees_is_refused() -> None:
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()
    _stored_with(repository, total_fees=Decimal("3.75"))

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)

    with pytest.raises(PaperSessionStateError, match="fees"):
        second.resume()


def test_resuming_with_reserved_balance_is_refused() -> None:
    # The incident's exact shape: funds held against a working order the broker no longer
    # remembers, because a broker's order book is process memory and nothing persists it.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()
    _stored_with(repository, balances=(_quote_balance("490.8673953283", "9509.1326046717"),))

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)

    with pytest.raises(PaperSessionStateError, match="reserved"):
        second.resume()


def test_the_week4_incident_state_is_refused_rather_than_resumed() -> None:
    # Regression, from the persisted state of paper-7c-week4 at 2026-08-20T16:14:46Z:
    # 490.8673953283 free, 9509.1326046717 locked against an approved GTC buy that never
    # filled, no open position, no realised PnL, no fees. Before this guard the session
    # resumed cleanly and silently traded a rebuilt 10 000 USDT account instead.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()
    _stored_with(
        repository,
        balances=(_quote_balance("490.8673953283", "9509.1326046717"),),
        positions=(),
        realized_pnl=Decimal(0),
        total_fees=Decimal(0),
    )

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)

    with pytest.raises(PaperSessionStateError) as caught:
        second.resume()

    # The refusal must name what it found, so an operator is not left guessing which of the
    # several irrecoverable conditions applies to their session.
    assert "9509.1326046717" in str(caught.value.details)
    assert second.runtime_metrics().bars_processed == 0


def test_a_refused_resume_processes_no_bars_at_all() -> None:
    # Fail-closed means closed before anything happens, not after the first bar reveals it.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    bars = _flat_bars(5)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, bars[:3])
    first.stop()
    _stored_with(repository, realized_pnl=Decimal("10"))

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)

    with pytest.raises(PaperSessionStateError):
        second.resume()

    assert second.is_running is False
    with pytest.raises(PaperSessionStateError, match="session is not running"):
        second.submit_bar(bars[3])


def test_a_refusal_leaves_the_stored_state_untouched() -> None:
    # The snapshot is the only surviving record of the interrupted session. A refusal that
    # damaged it would destroy the evidence an operator needs to decide what to do next.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()
    _stored_with(repository, balances=(_quote_balance("100", "900"),))
    before = repository.load("paper-1")

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    with pytest.raises(PaperSessionStateError):
        second.resume()

    assert repository.load("paper-1") == before


# --- M3: stop/risk state persistence ------------------------------------------------------------
#
# The state file gains somewhere to record what a position was protected by. Nothing writes
# it yet — sizing lands in M4, enforcement in M5 — so these tests hold two properties: a V1
# session is byte-identical to what it was, and a risk state that *does* exist survives the
# round trip and keeps resume closed.


def _risk_state(symbol: str = SYMBOL, **overrides: object) -> PositionRiskState:
    defaults: dict[str, object] = {
        "symbol": symbol,
        "stop": StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("70000")),
        "risk_amount": Decimal("100"),
        "entry_price": Decimal("72000"),
        "opened_at": ANCHOR,
    }
    return PositionRiskState(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_a_v1_session_persists_an_empty_risk_state() -> None:
    # No strategy sets a stop, so nothing populates this. An empty tuple rather than a
    # record full of Nones: a PositionRiskState that cannot say what was risked would be
    # the "protection that was never applied" failure this whole layer exists to prevent.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    session.start()
    _feed_bars(session, clock, _flat_bars(3))

    stored = repository.load("paper-1")

    assert stored is not None
    assert stored.position_risk == ()


def test_a_v1_snapshot_serialises_exactly_as_it_did_before() -> None:
    # The equivalence guarantee at the wire level. A new field that defaults to empty must
    # not change what an existing session writes to disk, and must survive the exact
    # serialisation path production uses — computed-field exclusions included, since a raw
    # round trip has never worked and asserting on one would test a path nobody runs.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    session.start()
    _feed_bars(session, clock, _flat_bars(2))
    stored = repository.load("paper-1")
    assert stored is not None

    payload = stored.model_dump_json(exclude=_COMPUTED_FIELD_EXCLUSIONS)

    assert json.loads(payload)["position_risk"] == []
    assert PaperSessionState.model_validate_json(payload) == stored


def test_a_persisted_risk_state_round_trips_through_json() -> None:
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    session, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    session.start()
    _feed_bars(session, clock, _flat_bars(2))
    stored = repository.load("paper-1")
    assert stored is not None
    with_risk = stored.model_copy(update={"position_risk": (_risk_state(),)})

    restored = PaperSessionState.model_validate_json(
        with_risk.model_dump_json(exclude=_COMPUTED_FIELD_EXCLUSIONS)
    )

    assert restored.position_risk == (_risk_state(),)
    assert restored.position_risk[0].risk_amount == Decimal("100")
    assert restored.position_risk[0].stop.trigger_price == Decimal("70000")


def test_resume_is_refused_while_a_position_carries_recorded_risk() -> None:
    # An orphan by construction: risk state without the position it describes. The guard
    # must not rely on `positions` being non-empty to catch this — that implication holds
    # today and a later change could break it silently.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()
    _stored_with(repository, position_risk=(_risk_state(),))

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)

    with pytest.raises(PaperSessionStateError, match="recorded position risk"):
        second.resume()


def test_a_refusal_over_risk_state_names_the_symbol_it_found() -> None:
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()
    _stored_with(repository, position_risk=(_risk_state(),))

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    with pytest.raises(PaperSessionStateError) as caught:
        second.resume()

    assert SYMBOL in str(caught.value.details)


def test_an_empty_risk_state_does_not_by_itself_block_resume() -> None:
    # The V1 path must stay open: an untouched session resumes exactly as it did before M3.
    repository = InMemoryPaperStateRepository()
    clock = SimulatedClock(ANCHOR)
    first, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    first.start()
    _feed_bars(first, clock, _flat_bars(3))
    first.stop()

    second, _, _, _ = _session(strategy=Silent(_Params()), clock=clock, repository=repository)
    status = second.resume()

    assert status.running is True
    assert second.runtime_metrics().bars_processed == 3
