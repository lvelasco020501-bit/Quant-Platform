"""Phase 7A: the live feed, driven through a scripted transport.

Every failure a real stream produces — a dropped socket, a silent connection, a replayed
candle, a hole in the series — is an ordinary test input here, because the feed talks to a
:class:`~quantplatform.core.interfaces.CandleStreamTransport` rather than to a socket. That
is the whole reason the port exists.

The final section is the one that matters most: the same feed, wired into the Phase 6 paper
session with nothing in between, driving the real strategy, risk engine, simulated broker
and portfolio. Nothing below the feed is told where its bars came from.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import MarketDataFeedState
from quantplatform.core.errors import (
    DataGapError,
    DataIntegrityError,
    DataProviderError,
    MarketDataConnectionError,
    MarketDataSubscriptionError,
    OutOfOrderDataError,
)
from quantplatform.core.interfaces import (
    CandleStreamTransport,
    PaperMarketDataFeed,
    StreamingMarketDataProvider,
)
from quantplatform.core.models.market import MarketBar
from quantplatform.marketdata.config import MarketDataConfiguration
from quantplatform.marketdata.feed import BinanceSpotMarketDataFeed
from quantplatform.marketdata.reconnect import BackoffSchedule
from quantplatform.paper import (
    InMemoryPaperStateRepository,
    PaperTradingRunner,
    PaperTradingSession,
)
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_backtest,
    make_bar,
    make_kline_frame,
    make_subscription_ack,
)
from tests.integration.test_backtest_engine import (
    _WARMUP_BARS,
    BuyOnce,
    Silent,
    _flat_bars,
    _Params,
)


class _ScriptExhaustedError(RuntimeError):
    """Raised when a test consumes more from a transport than it scripted.

    Fails loudly rather than hanging: a feed loop that never terminates is the one failure
    mode a test suite must not have.
    """


@dataclass(frozen=True, slots=True)
class _Step:
    """One thing the scripted transport does when the feed reads from it."""

    frame: str | None = None
    """Text to return, or ``None`` to report a read timeout."""

    error: Exception | None = None
    """Raised instead of returning, to simulate a transport failure."""

    advance_seconds: float = 0.0
    """Simulated seconds to burn before answering, for heartbeat tests."""

    jump_to: datetime | None = None
    """Instant to move the clock to, so a candle counts as closed when it arrives."""

    then_refuse_connects: int = 0
    """Connection attempts to refuse from this step onward, for backoff tests."""


def _frame(text: str, *, at: datetime | None = None) -> _Step:
    return _Step(frame=text, jump_to=at)


def _timeout(advance_seconds: float = 0.0) -> _Step:
    return _Step(advance_seconds=advance_seconds)


def _fail(*, then_refuse_connects: int = 0, error: Exception | None = None) -> _Step:
    """Drop the socket, optionally refusing the next few reconnection attempts."""
    return _Step(
        error=error if error is not None else MarketDataConnectionError("socket died"),
        then_refuse_connects=then_refuse_connects,
    )


@dataclass
class _ScriptedTransport:
    """A transport that replays a fixed script and records what was sent to it."""

    clock: SimulatedClock
    steps: list[_Step] = field(default_factory=list)
    connect_failures: int = 0
    """How many of the next connection attempts should fail."""

    fail_sends: bool = False
    """Report an open socket but refuse every send, as a half-dead connection does."""

    sent: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    closes: int = 0
    _open: bool = False
    _cursor: int = 0

    @property
    def is_connected(self) -> bool:
        return self._open

    def connect(self, url: str) -> None:
        self.urls.append(url)
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise MarketDataConnectionError("refused")
        self._open = True

    def send(self, payload: str) -> None:
        if not self._open or self.fail_sends:
            raise MarketDataConnectionError("not connected")
        self.sent.append(payload)

    def receive(self, timeout_seconds: float) -> str | None:
        if not self._open:
            raise MarketDataConnectionError("not connected")
        if self._cursor >= len(self.steps):
            raise _ScriptExhaustedError("the transport script ran out")
        step = self.steps[self._cursor]
        self._cursor += 1
        if step.jump_to is not None:
            self.clock.set_time(step.jump_to)
        if step.advance_seconds:
            self.clock.advance(timedelta(seconds=step.advance_seconds))
        if step.then_refuse_connects:
            self.connect_failures = step.then_refuse_connects
        if step.error is not None:
            self._open = False
            raise step.error
        return step.frame

    def close(self) -> None:
        if self._open:
            self.closes += 1
        self._open = False


def _config(**overrides: object) -> MarketDataConfiguration:
    defaults: dict[str, object] = {
        "heartbeat_timeout_seconds": 60.0,
        "receive_timeout_seconds": 5.0,
        "close_grace_seconds": 0.0,
        "max_reconnect_attempts": 3,
    }
    return MarketDataConfiguration(**{**defaults, **overrides})  # type: ignore[arg-type]


def _feed(
    steps: Sequence[_Step],
    *,
    clock: SimulatedClock | None = None,
    config: MarketDataConfiguration | None = None,
    connect_failures: int = 0,
    schedule: BackoffSchedule | None = None,
) -> tuple[BinanceSpotMarketDataFeed, _ScriptedTransport, list[float]]:
    """Wire a feed over a scripted transport, recording every backoff delay it waits out."""
    resolved_clock = clock if clock is not None else SimulatedClock(ANCHOR)
    transport = _ScriptedTransport(
        clock=resolved_clock, steps=list(steps), connect_failures=connect_failures
    )
    delays: list[float] = []
    feed = BinanceSpotMarketDataFeed(
        config=config if config is not None else _config(),
        clock=resolved_clock,
        transport=transport,
        schedule=schedule,
        sleep=delays.append,
    )
    return feed, transport, delays


def _bar_steps(bars: Sequence[MarketBar]) -> list[_Step]:
    """Script one closed-candle frame per bar, moving the clock to each candle's close."""
    return [_frame(make_kline_frame(bar), at=bar.close_time) for bar in bars]


def _take(feed: BinanceSpotMarketDataFeed, count: int) -> list[MarketBar]:
    """Consume exactly ``count`` bars, leaving the rest of the script untouched."""
    return list(itertools.islice(feed.closed_bars(), count))


# --- Ports ------------------------------------------------------------------------------------


def test_the_feed_satisfies_both_the_streaming_and_paper_ports() -> None:
    # The same object is a live provider and a paper feed. That is what lets a real stream
    # drop into a Phase 6 session with no adapter and no change to the session.
    feed, _, _ = _feed([])

    assert isinstance(feed, StreamingMarketDataProvider)
    assert isinstance(feed, PaperMarketDataFeed)


def test_the_scripted_transport_satisfies_the_transport_port() -> None:
    assert isinstance(_ScriptedTransport(clock=SimulatedClock(ANCHOR)), CandleStreamTransport)


# --- Connection and subscription ----------------------------------------------------------------


def test_connecting_subscribes_to_the_configured_stream() -> None:
    config = _config()
    feed, transport, _ = _feed([], config=config)

    feed.connect()

    assert transport.urls == [config.websocket_url]
    assert transport.sent == ['{"method": "SUBSCRIBE", "params": ["btcusdt@kline_1h"], "id": 1}']
    assert feed.state is MarketDataFeedState.CONNECTED


def test_connecting_twice_does_not_reopen_the_socket() -> None:
    feed, transport, _ = _feed([])

    feed.connect()
    feed.connect()

    assert len(transport.urls) == 1


def test_a_failed_connection_is_reported_rather_than_retried_silently() -> None:
    feed, _, _ = _feed([], connect_failures=1)

    with pytest.raises(MarketDataConnectionError, match="refused"):
        feed.connect()


def test_subscribing_adds_an_instrument_and_tells_the_venue() -> None:
    feed, transport, _ = _feed([])
    feed.connect()

    feed.subscribe(["ETH/USDT"])

    assert feed.symbols == ("BTC/USDT", "ETH/USDT")
    assert '"ethusdt@kline_1h"' in transport.sent[-1]
    assert '"method": "SUBSCRIBE"' in transport.sent[-1]


def test_subscribing_to_an_already_subscribed_instrument_is_a_no_op() -> None:
    feed, transport, _ = _feed([])
    feed.connect()
    before = len(transport.sent)

    feed.subscribe([SYMBOL])

    assert len(transport.sent) == before


def test_unsubscribing_removes_an_instrument_and_tells_the_venue() -> None:
    feed, transport, _ = _feed([])
    feed.connect()
    feed.subscribe(["ETH/USDT"])

    feed.unsubscribe(["ETH/USDT"])

    assert feed.symbols == (SYMBOL,)
    assert '"method": "UNSUBSCRIBE"' in transport.sent[-1]


def test_unsubscribing_an_unknown_instrument_is_harmless() -> None:
    feed, transport, _ = _feed([])
    feed.connect()
    before = len(transport.sent)

    feed.unsubscribe(["DOGE/USDT"])

    assert len(transport.sent) == before


def test_a_symbol_that_is_not_canonical_is_refused() -> None:
    feed, _, _ = _feed([])

    with pytest.raises(MarketDataSubscriptionError, match="BASE/QUOTE"):
        feed.subscribe(["btcusdt"])


def test_subscribing_before_connecting_defers_the_frame_until_the_socket_opens() -> None:
    feed, transport, _ = _feed([])

    feed.subscribe(["ETH/USDT"])
    assert transport.sent == []

    feed.connect()

    assert '"ethusdt@kline_1h"' in transport.sent[0]
    assert '"btcusdt@kline_1h"' in transport.sent[0]


def test_a_subscription_that_cannot_be_sent_is_a_subscription_error() -> None:
    # A half-dead socket: still reporting itself open, but refusing traffic. The transport
    # failure is translated, because "the subscription did not happen" is what the caller
    # has to act on, not which layer noticed.
    feed, transport, _ = _feed([])
    feed.connect()
    transport.fail_sends = True

    with pytest.raises(MarketDataSubscriptionError, match="could not send"):
        feed.subscribe(["ETH/USDT"])


def test_disconnecting_releases_the_socket_but_keeps_the_subscription() -> None:
    feed, transport, _ = _feed([])
    feed.connect()

    feed.disconnect()

    assert transport.is_connected is False
    assert feed.state is MarketDataFeedState.DISCONNECTED
    assert feed.symbols == (SYMBOL,)


def test_closing_is_terminal_and_idempotent() -> None:
    feed, transport, _ = _feed([])
    feed.connect()

    feed.close()
    feed.close()

    assert feed.state is MarketDataFeedState.STOPPED
    assert transport.is_connected is False
    with pytest.raises(MarketDataConnectionError, match="has been stopped"):
        feed.connect()
    with pytest.raises(MarketDataConnectionError, match="has been stopped"):
        _take(feed, 1)


# --- Delivery ---------------------------------------------------------------------------------


def test_closed_candles_are_delivered_in_order() -> None:
    bars = _flat_bars(3)
    feed, _, _ = _feed(_bar_steps(bars))

    delivered = _take(feed, 3)

    assert [bar.open_time for bar in delivered] == [bar.open_time for bar in bars]
    assert all(bar.is_closed for bar in delivered)
    assert feed.state is MarketDataFeedState.STREAMING
    assert feed.metrics.bars_emitted == 3
    assert feed.metrics.is_clean is True


def test_the_same_script_produces_the_same_bars_every_time() -> None:
    # Determinism is not a nicety here: it is what makes a paper run reproducible and a
    # regression in the feed detectable at all.
    bars = _flat_bars(4)

    first, _, _ = _feed(_bar_steps(bars))
    second, _, _ = _feed(_bar_steps(bars))

    assert _take(first, 4) == _take(second, 4)


def test_a_forming_candle_never_reaches_the_pipeline() -> None:
    bar = make_bar(index=0)
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(bar, is_closed=False), at=bar.close_time),
            _frame(make_kline_frame(bar), at=bar.close_time),
        ]
    )

    delivered = _take(feed, 1)

    assert [delivered_bar.open_time for delivered_bar in delivered] == [bar.open_time]
    assert feed.metrics.forming_suppressed == 1
    assert feed.metrics.candles_parsed == 2


def test_a_republished_candle_is_suppressed_rather_than_delivered_twice() -> None:
    bars = _flat_bars(2)
    steps = _bar_steps(bars[:1]) + _bar_steps(bars[:1]) + _bar_steps(bars[1:])
    feed, _, _ = _feed(steps)

    delivered = _take(feed, 2)

    assert [bar.open_time for bar in delivered] == [bars[0].open_time, bars[1].open_time]
    assert feed.metrics.duplicates_suppressed == 1


def test_a_subscription_acknowledgement_is_counted_but_carries_no_candle() -> None:
    bars = _flat_bars(1)
    feed, _, _ = _feed([_frame(make_subscription_ack()), *_bar_steps(bars)])

    _take(feed, 1)

    assert feed.metrics.control_frames == 1
    assert feed.metrics.frames_received == 2


def test_a_malformed_frame_stops_the_feed_rather_than_being_skipped() -> None:
    feed, _, _ = _feed([_frame("this is not json")])

    with pytest.raises(DataProviderError, match="not valid JSON"):
        _take(feed, 1)


def test_a_revised_candle_stops_the_feed() -> None:
    original = make_bar(index=0, close=Decimal(50_000))
    revised = make_bar(index=0, close=Decimal(51_000))
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(original), at=original.close_time),
            _frame(make_kline_frame(revised), at=revised.close_time),
        ]
    )

    with pytest.raises(DataIntegrityError, match="revised a candle"):
        _take(feed, 2)


def test_a_candle_that_goes_backwards_stops_the_feed() -> None:
    first = make_bar(index=1)
    second = make_bar(index=0)
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(first), at=first.close_time),
            _frame(make_kline_frame(second), at=first.close_time),
        ]
    )

    with pytest.raises(OutOfOrderDataError, match="predates"):
        _take(feed, 2)


# --- Gaps -------------------------------------------------------------------------------------


def test_a_missing_candle_pauses_the_feed_instead_of_being_traded_through() -> None:
    first = make_bar(index=0)
    later = make_bar(index=3)
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(first), at=first.close_time),
            _frame(make_kline_frame(later), at=later.close_time),
        ]
    )

    with pytest.raises(DataGapError, match="2 missing 1h candle"):
        _take(feed, 2)

    assert feed.state is MarketDataFeedState.PAUSED
    gap = feed.pending_gap
    assert gap is not None
    assert gap.missing_bars == 2
    assert feed.metrics.gaps_detected == 1
    assert feed.metrics.bars_emitted == 1


def test_a_paused_feed_refuses_to_stream_until_recovery_is_acknowledged() -> None:
    first = make_bar(index=0)
    later = make_bar(index=3)
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(first), at=first.close_time),
            _frame(make_kline_frame(later), at=later.close_time),
        ]
    )
    with pytest.raises(DataGapError):
        _take(feed, 2)

    with pytest.raises(DataGapError, match="resynchronize before continuing"):
        _take(feed, 1)


def test_resynchronising_lets_delivery_resume_after_a_gap() -> None:
    first = make_bar(index=0)
    later = make_bar(index=3)
    after = make_bar(index=4)
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(first), at=first.close_time),
            _frame(make_kline_frame(later), at=later.close_time),
            _frame(make_kline_frame(after), at=after.close_time),
        ]
    )
    with pytest.raises(DataGapError):
        _take(feed, 2)

    feed.resynchronize()

    assert feed.pending_gap is None
    assert feed.state is MarketDataFeedState.CONNECTED
    assert [bar.open_time for bar in _take(feed, 1)] == [after.open_time]


# --- Reconnection -------------------------------------------------------------------------------


def test_a_dropped_socket_is_reconnected_and_the_stream_continues() -> None:
    bars = _flat_bars(2)
    feed, transport, delays = _feed(
        [
            *_bar_steps(bars[:1]),
            _fail(),
            *_bar_steps(bars[1:]),
        ]
    )

    delivered = _take(feed, 2)

    assert [bar.open_time for bar in delivered] == [bars[0].open_time, bars[1].open_time]
    assert feed.metrics.reconnects == 1
    assert delays == [1.0]
    assert len(transport.urls) == 2


def test_a_reconnection_resubscribes_to_every_instrument() -> None:
    bars = _flat_bars(2)
    feed, transport, _ = _feed([*_bar_steps(bars[:1]), _fail(), *_bar_steps(bars[1:])])

    _take(feed, 2)

    assert len(transport.sent) == 2
    assert all("SUBSCRIBE" in payload for payload in transport.sent)


def test_a_silent_connection_is_reconnected_when_the_heartbeat_expires() -> None:
    bars = _flat_bars(2)
    feed, transport, _ = _feed(
        [
            *_bar_steps(bars[:1]),
            _timeout(advance_seconds=30.0),
            _timeout(advance_seconds=40.0),
            *_bar_steps(bars[1:]),
        ]
    )

    _take(feed, 2)

    assert feed.metrics.heartbeat_timeouts == 1
    assert feed.metrics.reconnects == 1
    assert len(transport.urls) == 2


def test_an_ordinary_read_timeout_is_not_treated_as_a_dead_socket() -> None:
    bars = _flat_bars(2)
    feed, transport, _ = _feed(
        [*_bar_steps(bars[:1]), _timeout(advance_seconds=5.0), *_bar_steps(bars[1:])]
    )

    _take(feed, 2)

    assert feed.metrics.heartbeat_timeouts == 0
    assert len(transport.urls) == 1


def test_reconnection_delays_follow_the_exponential_schedule() -> None:
    # Three reconnection attempts are refused before the fourth succeeds, so the delays
    # show the curve growing and then flattening at the ceiling.
    bars = _flat_bars(2)
    feed, _, delays = _feed(
        [*_bar_steps(bars[:1]), _fail(then_refuse_connects=3), *_bar_steps(bars[1:])],
        schedule=BackoffSchedule(
            initial_delay_seconds=1.0, max_delay_seconds=4.0, multiplier=2.0, max_attempts=5
        ),
    )

    _take(feed, 2)

    assert delays == [1.0, 2.0, 4.0, 4.0]
    assert feed.metrics.reconnects == 1


def test_the_feed_keeps_retrying_past_the_nominal_budget_instead_of_giving_up() -> None:
    # SEV-1 2026-08-20: a transport outage that took six consecutive connection attempts
    # to clear ended paper-7c-week4 in 31 seconds, on infrastructure that had run cleanly
    # for 50 hours. Continuity now survives more failed attempts than the nominal budget —
    # DataGapError, not an attempt counter, is what may end a session over market data.
    bars = _flat_bars(2)
    feed, _, delays = _feed(
        [*_bar_steps(bars[:1]), _fail(then_refuse_connects=6), *_bar_steps(bars[1:])],
        schedule=BackoffSchedule(max_attempts=2, max_delay_seconds=4.0),
    )

    delivered = _take(feed, 2)

    assert [bar.open_time for bar in delivered] == [bars[0].open_time, bars[1].open_time]
    assert len(delays) == 7
    assert feed.metrics.reconnects == 1


def test_a_prolonged_outage_leaves_the_session_alive_not_terminated() -> None:
    # Fifty consecutive failures, far past any nominal budget this platform has ever
    # configured — the feed must still be standing, and delivering, on the other side.
    bars = _flat_bars(2)
    feed, _, delays = _feed(
        [*_bar_steps(bars[:1]), _fail(then_refuse_connects=50), *_bar_steps(bars[1:])],
        schedule=BackoffSchedule(max_attempts=5, max_delay_seconds=8.0),
    )

    delivered = _take(feed, 2)

    assert [bar.open_time for bar in delivered] == [bars[0].open_time, bars[1].open_time]
    assert len(delays) == 51
    assert delays[-1] == 8.0
    assert feed.state is not MarketDataFeedState.STOPPED


def test_a_long_outage_that_resumes_ahead_is_still_caught_as_a_gap() -> None:
    # Composing the two changes: retrying past the old budget must not weaken the one
    # thing that is still allowed to end a session over market data.
    first = make_bar(index=0)
    resumed = make_bar(index=5)
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(first), at=first.close_time),
            _fail(then_refuse_connects=8),
            _frame(make_kline_frame(resumed), at=resumed.close_time),
        ],
        schedule=BackoffSchedule(max_attempts=2, max_delay_seconds=2.0),
    )

    with pytest.raises(DataGapError, match="4 missing"):
        _take(feed, 2)

    assert feed.state is MarketDataFeedState.PAUSED


def test_a_stop_request_during_reconnection_is_honoured_between_attempts() -> None:
    # No real signal is raised here — request_stop() is exactly what a caught SIGTERM
    # calls, and this asserts the one thing this change owns: the retry loop notices the
    # flag between attempts and gives up cleanly, rather than retrying forever regardless.
    clock = SimulatedClock(ANCHOR)
    bars = _flat_bars(1)
    transport = _ScriptedTransport(
        clock=clock, steps=[*_bar_steps(bars), _fail(then_refuse_connects=99)]
    )
    delays: list[float] = []
    stop_after = 3

    def _sleep_then_stop_on_the_third_wait(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= stop_after:
            feed.request_stop()

    feed = BinanceSpotMarketDataFeed(
        config=_config(max_reconnect_attempts=2),
        clock=clock,
        transport=transport,
        schedule=BackoffSchedule(max_attempts=2, max_delay_seconds=2.0),
        sleep=_sleep_then_stop_on_the_third_wait,
    )

    feed_iter = feed.closed_bars()
    next(feed_iter)  # the one bar delivered before the outage starts

    delivered = list(feed_iter)

    assert delivered == []
    assert len(delays) == stop_after
    assert feed.state is not MarketDataFeedState.STOPPED  # request_stop, not close()


def test_a_candle_replayed_after_a_reconnect_is_suppressed() -> None:
    # Venues resend the most recent candle when a stream comes back. Continuity state
    # survives the reconnect precisely so this is a duplicate rather than a second trade.
    bars = _flat_bars(2)
    feed, _, _ = _feed(
        [
            *_bar_steps(bars[:1]),
            _fail(),
            *_bar_steps(bars[:1]),
            *_bar_steps(bars[1:]),
        ]
    )

    delivered = _take(feed, 2)

    assert [bar.open_time for bar in delivered] == [bars[0].open_time, bars[1].open_time]
    assert feed.metrics.duplicates_suppressed == 1


def test_a_stream_that_resumes_further_ahead_after_a_reconnect_is_caught_as_a_gap() -> None:
    first = make_bar(index=0)
    resumed = make_bar(index=5)
    feed, _, _ = _feed(
        [
            _frame(make_kline_frame(first), at=first.close_time),
            _fail(),
            _frame(make_kline_frame(resumed), at=resumed.close_time),
        ]
    )

    with pytest.raises(DataGapError, match="4 missing"):
        _take(feed, 2)

    assert feed.state is MarketDataFeedState.PAUSED


def test_a_stopped_feed_leaves_the_delivery_loop() -> None:
    bars = _flat_bars(3)
    feed, _, _ = _feed(_bar_steps(bars))

    delivered: list[MarketBar] = []
    for bar in feed.closed_bars():
        delivered.append(bar)
        feed.request_stop()

    assert len(delivered) == 1


# --- The pipeline, unaware of where its bars came from -------------------------------------------


def _session(
    feed: BinanceSpotMarketDataFeed, clock: SimulatedClock
) -> tuple[PaperTradingSession, object]:
    engine, broker, portfolio = make_backtest(strategy=BuyOnce(_Params()))
    session = PaperTradingSession(
        session_id="live-paper-1",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=InMemoryPaperStateRepository(),
    )
    assert isinstance(feed, PaperMarketDataFeed)
    return session, portfolio


def test_a_live_feed_drives_the_whole_chain_and_settles_virtually() -> None:
    # Feed -> runner -> session -> BacktestEngine.advance -> features -> strategy -> risk ->
    # simulated broker -> portfolio. The only real thing in the chain is the market data.
    bars = _flat_bars(5)
    clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed(_bar_steps(bars), clock=clock)
    session, portfolio = _session(feed, clock)

    runner = PaperTradingRunner(session=session, feed=feed, feed_metrics=feed, max_bars=5)
    result = runner.run()

    assert result.runtime.bars_processed == 5
    assert result.runtime.orders_submitted == 1
    assert result.runtime.fills_received == 1
    assert portfolio.positions()[0].quantity > Decimal(0)  # type: ignore[attr-defined]
    assert feed.state is MarketDataFeedState.STOPPED


def test_execution_stays_next_bar_when_the_bars_arrive_over_a_socket() -> None:
    bars = _flat_bars(5)
    clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed(_bar_steps(bars), clock=clock)
    session, _ = _session(feed, clock)

    result = PaperTradingRunner(session=session, feed=feed, feed_metrics=feed, max_bars=5).run()

    detail = result.detail
    assert detail is not None
    decided = _WARMUP_BARS - 1
    assert detail.bars[decided].signals != ()
    assert detail.bars[decided].fills == ()
    assert detail.bars[decided + 1].fills != ()


def test_the_pipeline_cannot_tell_a_socket_from_a_replay() -> None:
    # The strongest statement Phase 7A can make: identical candles, one set arriving as
    # WebSocket frames and one handed straight to the session, produce identical accounts.
    bars = _flat_bars(6)

    live_clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed(_bar_steps(bars), clock=live_clock)
    live_session, live_portfolio = _session(feed, live_clock)
    live = PaperTradingRunner(session=live_session, feed=feed, feed_metrics=feed, max_bars=6).run()

    direct_clock = SimulatedClock(ANCHOR)
    direct_engine, direct_broker, direct_portfolio = make_backtest(strategy=BuyOnce(_Params()))
    direct_session = PaperTradingSession(
        session_id="live-paper-1",
        engine=direct_engine,
        broker=direct_broker,
        portfolio=direct_portfolio,
        config=direct_engine._config,
        clock=direct_clock,
    )
    direct_session.start()
    for bar in bars:
        direct_clock.set_time(bar.close_time)
        direct_session.submit_bar(bar)
    direct = direct_session.result()

    assert live.runtime.bars_processed == direct.runtime.bars_processed
    assert live.runtime.fills_received == direct.runtime.fills_received
    assert live.snapshot.equity == direct.snapshot.equity
    assert live_portfolio.positions() == direct_portfolio.positions()  # type: ignore[attr-defined]


def test_forming_candles_from_the_socket_never_reach_the_session() -> None:
    bars = _flat_bars(4)
    clock = SimulatedClock(ANCHOR)
    steps: list[_Step] = []
    for bar in bars:
        steps.append(_frame(make_kline_frame(bar, is_closed=False), at=bar.open_time))
        steps.append(_frame(make_kline_frame(bar), at=bar.close_time))
    feed, _, _ = _feed(steps, clock=clock)
    session, _ = _session(feed, clock)

    result = PaperTradingRunner(session=session, feed=feed, feed_metrics=feed, max_bars=4).run()

    assert result.runtime.bars_received == 4
    assert result.runtime.bars_rejected == 0
    assert feed.metrics.forming_suppressed == 4


def test_a_gap_mid_session_stops_the_run_and_still_closes_everything() -> None:
    clock = SimulatedClock(ANCHOR)
    first = make_bar(index=0)
    later = make_bar(index=6)
    feed, transport, _ = _feed(
        [
            _frame(make_kline_frame(first), at=first.close_time),
            _frame(make_kline_frame(later), at=later.close_time),
        ],
        clock=clock,
    )
    session, _ = _session(feed, clock)

    with pytest.raises(DataGapError):
        PaperTradingRunner(session=session, feed=feed, feed_metrics=feed).run()

    # The runner's finally block still ran: the session was stopped and the feed released.
    assert session.is_running is False
    assert transport.is_connected is False
    assert feed.state is MarketDataFeedState.STOPPED


def test_a_run_over_a_socket_reads_no_wall_clock() -> None:
    # Every instant in the run comes from the injected clock. If anything below reached for
    # the real one, this session would be dated by the machine rather than by the market.
    bars = _flat_bars(4)
    clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed(_bar_steps(bars), clock=clock)
    session, _ = _session(feed, clock)

    result = PaperTradingRunner(session=session, feed=feed, feed_metrics=feed, max_bars=4).run()

    assert result.status.started_at == ANCHOR
    assert clock.now() == bars[-1].close_time
    assert result.snapshot.taken_at == bars[-1].close_time


def test_a_silent_session_still_records_every_candle_it_saw() -> None:
    bars = _flat_bars(3)
    clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed(_bar_steps(bars), clock=clock)
    engine, broker, portfolio = make_backtest(strategy=Silent(_Params()))
    session = PaperTradingSession(
        session_id="quiet",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
    )

    result = PaperTradingRunner(session=session, feed=feed, feed_metrics=feed, max_bars=3).run()

    assert result.runtime.bars_processed == 3
    assert result.runtime.orders_submitted == 0
    assert feed.metrics.bars_emitted == 3
