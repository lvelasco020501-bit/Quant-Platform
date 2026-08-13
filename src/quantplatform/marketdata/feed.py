"""The Binance Spot candle feed, and the socket it reads.

Two classes with deliberately unequal shares of the thinking. :class:`WebSocketCandleTransport`
is as thin as an adapter can be — open, send, receive, close — and holds no judgement about
market data at all. :class:`BinanceSpotMarketDataFeed` holds all of it, and holds it against
a port rather than a socket, which is why a dropped connection, an expired heartbeat, a
replayed candle and a hole in the series can each be reproduced exactly in a test.

**This package cannot trade.** There is no order method, no balance method, no signed
request and no credential. The endpoint is validated to be a public stream before a socket
is opened, and the only thing that leaves this process is a ``SUBSCRIBE`` frame naming
public candle streams.

**It is also not the pipeline.** The feed's entire output is a
:class:`~quantplatform.core.models.market.MarketBar` iterator — the same shape a replay
double produced in Phase 6 and the same shape a CSV backtest consumes. Nothing below it
learns where a bar came from, which is the property that lets a paper session prove
something about live behaviour.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from typing import Final

from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from quantplatform.core.clock import Clock
from quantplatform.core.enums import MarketDataFeedState
from quantplatform.core.errors import (
    DataGapError,
    DataIntegrityError,
    DataProviderError,
    MarketDataConnectionError,
    MarketDataSubscriptionError,
)
from quantplatform.core.interfaces import CandleStreamTransport
from quantplatform.core.logging_config import get_logger
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.marketdata.clock import FeedClock
from quantplatform.marketdata.config import MarketDataConfiguration
from quantplatform.marketdata.models import (
    CandleAdmission,
    CandleOutcome,
    FeedMetrics,
    GapReport,
    StreamSubscription,
)
from quantplatform.marketdata.reconnect import BackoffSchedule, ReconnectPolicy
from quantplatform.marketdata.validation import CandleParser, CandleSequenceValidator

__all__ = ["BinanceSpotMarketDataFeed", "WebSocketCandleTransport"]

_LOGGER = get_logger(__name__)

_SUBSCRIBE: Final[str] = "SUBSCRIBE"
_UNSUBSCRIBE: Final[str] = "UNSUBSCRIBE"
_MAX_FRAME_BYTES: Final[int] = 1 << 20
"""One megabyte. A candle frame is a few hundred bytes; anything approaching this is not
market data, and an unbounded reader is how a stream becomes a memory exhaustion bug."""

_SOCKET_TIMEOUT_MARGIN_SECONDS: Final[float] = 10.0
"""How much longer the raw socket's own read timeout is set than the per-call polling
timeout passed to :meth:`WebSocketCandleTransport.receive`.

The root cause of an eight-hour stall traced to exactly one line in the vendored
``websockets`` library: its background reader thread calls the underlying
``socket.recv()`` with **no timeout at all** while a connection is in ordinary
operation — a deadline is only armed once a close handshake has already begun. On a
connection that goes silent without a clean TCP close (no FIN, no RST — a "black hole",
plausible after a network path change), that raw call can block for as long as the
operating system's own dead-peer detection eventually takes, which is not bounded in any
useful sense.

The margin exists so the library's own, already-correct polling timeout is what fires
under every ordinary condition — a real timeout every ``receive_timeout_seconds`` with
nothing wrong at all — and this is purely a backstop that should, in practice, never be
the first thing to trigger. It is not a duplicate of that timeout with a different name;
it bounds a different call, on a different thread, that the polling timeout cannot reach.
"""

_HARD_CLOSE_TIMEOUT_SECONDS: Final[float] = 20.0
"""Absolute ceiling on how long :meth:`WebSocketCandleTransport.close` may take.

Independent of the socket-timeout fix above, and deliberately so: that fix is reasoned
from reading the vendored library's current source, and a future version of it — or a
platform quirk this analysis did not anticipate — could invalidate the reasoning without
announcing itself. This is the structural guarantee that survives that possibility: a
close is handed to a watcher thread, and if it has not finished within this ceiling, the
raw socket is shut down and closed directly, bypassing the library's own close path
entirely. "Bounded" is then a fact about elapsed wall-clock time, not an inference about
what a dependency's internals are expected to do.
"""

_SYMBOL_PARTS: Final[int] = 2
"""A canonical symbol is exactly ``BASE/QUOTE``."""

_SHUT_RDWR: Final[int] = 2
"""The standard POSIX value of ``socket.SHUT_RDWR`` (also used on Windows), inlined so
this package never imports the :mod:`socket` module. An architecture test forbids it here
deliberately — it is how a raw client bypassing the one sanctioned ``websockets``
connection would be built — and the one legitimate use in this module, forcing shut the
*existing*, already-authorised connection's own socket on a hard-close timeout, needs
only this one well-known constant, not the ability to open a new one."""


class WebSocketCandleTransport:
    """A :class:`~quantplatform.core.interfaces.CandleStreamTransport` over a real socket.

    Synchronous on purpose. The paper runner and session are single-threaded and loop-free
    by design, and putting an event loop underneath them would trade that determinism for
    nothing a single market-data stream needs.

    Holds no market-data logic whatsoever, which is what keeps the untestable part of this
    phase — the part that needs an actual network — down to opening a socket and reading
    text off it.

    **Hardened against an indefinite block, two ways.** The raw socket the library reads
    from is kept under an explicit timeout for the whole life of the connection — see
    :data:`_SOCKET_TIMEOUT_MARGIN_SECONDS` for why that is necessary at all — and closing
    that connection is handed to a watcher with a hard ceiling — see
    :data:`_HARD_CLOSE_TIMEOUT_SECONDS`. Together they are what let :meth:`close` and, by
    extension, every reconnect built on it, be a *proven* bound rather than a hoped-for
    one.
    """

    def __init__(
        self,
        *,
        open_timeout_seconds: float = 10.0,
        close_timeout_seconds: float = 5.0,
        hard_close_timeout_seconds: float = _HARD_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        """Create a transport.

        Args:
            open_timeout_seconds: How long to wait for the handshake.
            close_timeout_seconds: How long to wait for a graceful close before the
                backstop forces the socket shut. Handed to the library as its own
                ``close_timeout``, so its first attempt already respects this; the
                backstop exists for what happens if that attempt does not return at all.
            hard_close_timeout_seconds: Absolute ceiling on :meth:`close`, independent of
                whether the library's own close completes. Deliberately looser than
                ``close_timeout_seconds`` — this is the ceiling of last resort, not the
                ordinary path.
        """
        self._open_timeout = open_timeout_seconds
        self._close_timeout = close_timeout_seconds
        self._hard_close_timeout = hard_close_timeout_seconds
        self._connection: ClientConnection | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether a channel is currently open."""
        return self._connection is not None

    def connect(self, url: str) -> None:
        """Open the channel, replacing any previous one.

        Raises:
            MarketDataConnectionError: If the handshake fails.
        """
        self.close()
        try:
            self._connection = connect(
                url,
                open_timeout=self._open_timeout,
                close_timeout=self._close_timeout,
                max_size=_MAX_FRAME_BYTES,
            )
        except (WebSocketException, OSError, TimeoutError) as exc:
            raise MarketDataConnectionError(
                "could not open the market-data stream", error=type(exc).__name__
            ) from exc
        # Bound the raw socket from the instant the connection exists, closing the gap
        # between this call returning and the first receive() — the background reader
        # thread is already running by the time connect() hands control back here.
        self._bound_socket(self._open_timeout + _SOCKET_TIMEOUT_MARGIN_SECONDS)

    def _bound_socket(self, timeout_seconds: float) -> None:
        """Set a hard timeout on the connection's underlying raw socket.

        This is the fix for the actual defect: the vendored library's background reader
        thread calls ``socket.recv()`` with no timeout of its own during ordinary
        operation, so a connection that goes silent without a clean TCP close can block
        that thread — and, transitively, any later close waiting on it — for as long as
        the operating system takes to notice on its own. A socket-level timeout is
        enforced by the OS underneath the library, and does not depend on any cooperation
        from it.

        Safe to call repeatedly; ``socket.settimeout`` only affects reads issued after it
        runs, which is why :meth:`receive` calls this on every poll rather than once.
        """
        if self._connection is not None:
            self._connection.socket.settimeout(timeout_seconds)

    def send(self, payload: str) -> None:
        """Send one text frame.

        Raises:
            MarketDataConnectionError: If the channel is closed or the send fails.
        """
        connection = self._require_connection()
        try:
            connection.send(payload)
        except (WebSocketException, OSError) as exc:
            raise MarketDataConnectionError(
                "could not send on the market-data stream", error=type(exc).__name__
            ) from exc

    def receive(self, timeout_seconds: float) -> str | None:
        """Return the next text frame, or ``None`` when none arrived in time.

        Raises:
            MarketDataConnectionError: If the channel failed while waiting.
        """
        connection = self._require_connection()
        self._bound_socket(timeout_seconds + _SOCKET_TIMEOUT_MARGIN_SECONDS)
        try:
            message = connection.recv(timeout=timeout_seconds)
        except TimeoutError:
            # The overwhelmingly common outcome, once per poll interval whenever the venue
            # has nothing new: logged at DEBUG so it documents the polling cadence during a
            # diagnosis without adding a line to every production log at INFO.
            _LOGGER.debug("receive timed out", extra={"timeout_seconds": timeout_seconds})
            return None
        except (WebSocketException, OSError) as exc:
            _LOGGER.warning(
                "receive failed", extra={"error": type(exc).__name__, "detail": str(exc)}
            )
            raise MarketDataConnectionError(
                "market-data stream failed while receiving", error=type(exc).__name__
            ) from exc
        text = message.decode("utf-8", errors="replace") if isinstance(message, bytes) else message
        _LOGGER.debug("frame received", extra={"bytes": len(text)})
        return text

    def close(self) -> None:
        """Release the channel. Safe to call repeatedly, and never raises.

        A failure while closing a socket that is already being abandoned carries no
        information worth propagating, and letting it escape would mean a shutdown path
        that fails during shutdown.

        **Bounded by :data:`_HARD_CLOSE_TIMEOUT_SECONDS`, provably.** The graceful close
        the library performs is handed to a watcher thread; if it has not finished by the
        deadline, the raw socket is shut down and closed directly here, in this thread,
        without waiting on the watcher any further. This call therefore always returns
        within the ceiling, regardless of what the library's own close path is doing.
        """
        connection, self._connection = self._connection, None
        if connection is None:
            return
        self._close_bounded(connection)

    def _close_bounded(self, connection: ClientConnection) -> None:
        """Run a graceful close on a watcher thread, forcing the socket shut on timeout.

        The one deliberate use of a background thread in this module, for the same
        reason :class:`~quantplatform.paper.watchdog.StallWatchdog` is the one in its own:
        bounding a call from outside is impossible from inside the call itself. The
        watcher is a daemon and is never joined if it overruns — if the library's close
        is itself the thing stuck, forcing the socket shut here lets *this* thread return
        the moment the ceiling is reached; the watcher thread is simply abandoned; it
        holds no lock this module depends on and cannot block interpreter shutdown.
        """
        finished = threading.Event()

        def _graceful_close() -> None:
            with suppress(WebSocketException, OSError):
                connection.close()
            finished.set()

        threading.Thread(target=_graceful_close, name="transport-close", daemon=True).start()
        if finished.wait(self._hard_close_timeout):
            return
        _LOGGER.error(
            "graceful close did not finish within the hard ceiling; forcing the socket "
            "shut directly",
            extra={"hard_close_timeout_seconds": self._hard_close_timeout},
        )
        with suppress(OSError):
            connection.socket.shutdown(_SHUT_RDWR)
        with suppress(OSError):
            connection.socket.close()

    def _require_connection(self) -> ClientConnection:
        """Return the open connection.

        Raises:
            MarketDataConnectionError: If no channel is open.
        """
        if self._connection is None:
            raise MarketDataConnectionError("the market-data stream is not connected")
        return self._connection


class BinanceSpotMarketDataFeed:
    """Live Binance Spot candles, read-only, delivered one closed bar at a time.

    Satisfies both :class:`~quantplatform.core.interfaces.StreamingMarketDataProvider` and
    :class:`~quantplatform.core.interfaces.PaperMarketDataFeed`, so it drops into a paper
    session exactly where a replay double sat, with no adapter between them.
    """

    def __init__(
        self,
        *,
        config: MarketDataConfiguration,
        clock: Clock,
        transport: CandleStreamTransport | None = None,
        schedule: BackoffSchedule | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Wire a feed.

        Args:
            config: Validated endpoint, instruments and timings.
            clock: Injected platform clock; nothing here reads a wall clock.
            transport: Channel to the venue. Defaults to a real WebSocket.
            schedule: Reconnection curve; derived from ``config`` when omitted.
            sleep: How to wait out a backoff delay. Injected so a test can exercise the
                full retry schedule instantly and assert on the delays it was asked for.
        """
        self._config = config
        self._clock = FeedClock(clock, close_grace_seconds=config.close_grace_seconds)
        self._transport = transport if transport is not None else WebSocketCandleTransport()
        self._sleep = sleep if sleep is not None else time.sleep
        self._policy = ReconnectPolicy(
            schedule
            if schedule is not None
            else BackoffSchedule(
                initial_delay_seconds=config.reconnect_initial_delay_seconds,
                max_delay_seconds=config.reconnect_max_delay_seconds,
                multiplier=config.reconnect_backoff_multiplier,
                max_attempts=config.max_reconnect_attempts,
            )
        )

        self._subscriptions: dict[str, StreamSubscription] = {}
        for symbol in config.symbols:
            self._subscriptions[symbol] = self._build_subscription(symbol)

        self._parser = CandleParser(
            symbols={
                subscription.venue_symbol: symbol
                for symbol, subscription in self._subscriptions.items()
            },
            timeframe=config.timeframe,
            market_type=config.market_type,
            source=config.source_id,
        )
        self._sequence = CandleSequenceValidator(timeframe=config.timeframe, clock=self._clock)

        self._state = MarketDataFeedState.DISCONNECTED
        self._metrics = FeedMetrics()
        self._pending_gap: GapReport | None = None
        self._request_id = 0
        self._stopping = False

    # --- Identity and observation -----------------------------------------------------------

    def _transition(self, new_state: MarketDataFeedState) -> None:
        """Change the feed's state. The only place this ever happens.

        A stall investigation starts with "what state was the feed last known to be in,
        and when did it last change" — a question a scattered set of bare assignments
        cannot answer without reading the whole file. Routing every change through here
        both answers it (one INFO line per transition) and guarantees no future change
        site is added without being observed.
        """
        previous = self._state
        self._state = new_state
        if previous is not new_state:
            _LOGGER.info(
                "feed state transition",
                extra={"feed": self.name, "from": previous.value, "to": new_state.value},
            )

    @property
    def name(self) -> str:
        """Return the provenance identifier recorded on every bar."""
        return self._config.source_id

    @property
    def symbols(self) -> Sequence[str]:
        """Return the canonical platform symbols currently subscribed, in sorted order."""
        return tuple(sorted(self._subscriptions))

    @property
    def state(self) -> MarketDataFeedState:
        """Return the feed's connection and synchronisation state."""
        return self._state

    @property
    def metrics(self) -> FeedMetrics:
        """Return what the feed has done so far, separate from what the market did."""
        return self._metrics

    @property
    def pending_gap(self) -> GapReport | None:
        """Return the unresolved continuity break, when the feed is paused for one."""
        return self._pending_gap

    def last_bar(self, symbol: str) -> MarketBar | None:
        """Return the most recent bar delivered for a symbol, if any."""
        return self._sequence.last_accepted(symbol)

    def read_feed_metrics(self) -> FeedMetricsSnapshot:
        """Return the feed's cumulative counters in the vocabulary a report speaks.

        Satisfies :class:`~quantplatform.core.interfaces.FeedMetricsReader`, so the feed can
        be handed to a paper runner as its own telemetry source. Cumulative and never reset:
        a caller wanting one day's activity subtracts two readings, and resetting here would
        erase any window nobody had reported yet.
        """
        return self._metrics.health_snapshot()

    # --- Connection -------------------------------------------------------------------------

    def connect(self) -> None:
        """Open the stream and subscribe to the configured instruments.

        Raises:
            MarketDataConnectionError: If the feed was stopped, or the stream cannot be
                opened.
        """
        if self._state is MarketDataFeedState.STOPPED:
            raise MarketDataConnectionError("the feed has been stopped", feed=self.name)
        if self._transport.is_connected:
            return
        self._policy.reset()
        self._open()

    def disconnect(self) -> None:
        """Close the stream, leaving the feed reusable.

        Subscriptions and continuity state are kept: reconnecting must resume the same
        series, not start a new one, or every reconnection would hide a gap.
        """
        self._transport.close()
        self._clock.forget_frames()
        if self._state is not MarketDataFeedState.STOPPED:
            self._transition(MarketDataFeedState.DISCONNECTED)

    def close(self) -> None:
        """Release everything and refuse further streaming. Safe to call repeatedly."""
        self._stopping = True
        self._transport.close()
        self._clock.forget_frames()
        self._transition(MarketDataFeedState.STOPPED)

    def request_stop(self) -> None:
        """Ask the delivery loop to finish after the candle it is handling.

        Cooperative rather than immediate, matching the paper runner: interrupting between
        a candle and its admission would leave the continuity anchor disagreeing with what
        the pipeline actually saw.
        """
        self._stopping = True

    # --- Subscription -----------------------------------------------------------------------

    def subscribe(self, symbols: Sequence[str]) -> None:
        """Add instruments to the live subscription.

        Args:
            symbols: Canonical platform symbols. Already-subscribed symbols are ignored.

        Raises:
            MarketDataSubscriptionError: If a symbol is not in ``BASE/QUOTE`` form, or the
                subscription frame cannot be sent.
        """
        added: list[StreamSubscription] = []
        for symbol in symbols:
            subscription = self._build_subscription(symbol)
            if symbol in self._subscriptions:
                continue
            self._subscriptions[symbol] = subscription
            self._parser.register(venue_symbol=subscription.venue_symbol, symbol=symbol)
            added.append(subscription)
        if added and self._transport.is_connected:
            self._send(_SUBSCRIBE, tuple(added))

    def unsubscribe(self, symbols: Sequence[str]) -> None:
        """Remove instruments from the live subscription.

        Continuity state for a removed symbol is discarded. Keeping it would mean that
        re-subscribing hours later reported every candle in between as one enormous gap,
        which describes a decision the operator already made rather than a fault.

        Args:
            symbols: Canonical platform symbols. Unknown symbols are ignored.

        Raises:
            MarketDataSubscriptionError: If the unsubscription frame cannot be sent.
        """
        removed: list[StreamSubscription] = []
        for symbol in symbols:
            subscription = self._subscriptions.pop(symbol, None)
            if subscription is None:
                continue
            self._parser.forget(subscription.venue_symbol)
            self._sequence.resynchronize(symbol)
            removed.append(subscription)
        if removed and self._transport.is_connected:
            self._send(_UNSUBSCRIBE, tuple(removed))

    # --- Delivery ---------------------------------------------------------------------------

    def closed_bars(self) -> Iterator[MarketBar]:
        """Yield closed candles as the venue publishes them.

        Connects if needed, reconnects on its own when the stream drops or falls silent,
        and stops yielding rather than skipping when the series breaks.

        Returns:
            An iterator over closed bars in strictly increasing open-time order.

        Raises:
            MarketDataConnectionError: If the feed is stopped, or reconnection is exhausted.
            DataGapError: If a candle is missing, or the feed is already paused for one.
            DataIntegrityError: If the venue publishes a candle that cannot be trusted.
            OutOfOrderDataError: If a candle predates one already delivered.
        """
        self._require_streamable()
        self._stopping = False
        if not self._transport.is_connected:
            self.connect()
        while not self._stopping:
            frame = self._next_frame()
            if frame is None:
                continue
            self._metrics = self._metrics.record(frames_received=1)
            bar = self._parse(frame)
            if bar is None:
                self._metrics = self._metrics.record(control_frames=1)
                continue
            self._metrics = self._metrics.record(candles_parsed=1)
            admitted = self._apply(self._sequence.admit(bar))
            if admitted is not None:
                _LOGGER.info(
                    "closed bar emitted",
                    extra={
                        "feed": self.name,
                        "symbol": admitted.symbol,
                        "close_time": admitted.close_time.isoformat(),
                    },
                )
                yield admitted

    def resynchronize(self, symbol: str | None = None) -> None:
        """Accept a detected gap and allow delivery to resume.

        The explicit recovery step a paused feed requires. It is separate and manual
        because accepting a discontinuity has consequences an operator should own: the
        candles either side of the hole are no longer one continuous series, and no
        automatic policy can know whether that is tolerable for the strategy running.

        Args:
            symbol: Symbol to resynchronise, or ``None`` for every symbol.
        """
        self._sequence.resynchronize(symbol)
        self._pending_gap = None
        if self._state is MarketDataFeedState.PAUSED:
            self._state = (
                MarketDataFeedState.CONNECTED
                if self._transport.is_connected
                else MarketDataFeedState.DISCONNECTED
            )

    # --- Internals --------------------------------------------------------------------------

    def _require_streamable(self) -> None:
        """Refuse to stream from a stopped or paused feed.

        Raises:
            MarketDataConnectionError: If the feed has been stopped.
            DataGapError: If an unresolved gap is still outstanding.
        """
        if self._state is MarketDataFeedState.STOPPED:
            raise MarketDataConnectionError("the feed has been stopped", feed=self.name)
        if self._state is MarketDataFeedState.PAUSED:
            gap = self._pending_gap
            raise DataGapError(
                "the feed is paused on an unresolved gap; resynchronize before continuing",
                feed=self.name,
                gap=gap.describe() if gap is not None else None,
            )

    def _open(self) -> None:
        """Open the transport and restore the subscription on it.

        Raises:
            MarketDataConnectionError: If the stream cannot be opened.
            MarketDataSubscriptionError: If the subscription cannot be sent.
        """
        self._transition(MarketDataFeedState.CONNECTING)
        self._metrics = self._metrics.record(connection_attempts=1)
        self._transport.connect(self._config.websocket_url)
        self._transition(MarketDataFeedState.CONNECTED)
        # The handshake itself counts as liveness, so the heartbeat window starts now
        # rather than at the first candle — a venue that accepts a connection and then
        # says nothing is exactly the failure the heartbeat exists to catch.
        self._clock.mark_frame()
        if self._subscriptions:
            self._send(_SUBSCRIBE, tuple(self._subscriptions.values()))

    def _send(self, method: str, subscriptions: tuple[StreamSubscription, ...]) -> None:
        """Send a subscription control frame.

        Raises:
            MarketDataSubscriptionError: If the frame cannot be delivered.
        """
        self._request_id += 1
        payload = json.dumps(
            {
                "method": method,
                "params": [subscription.stream_name for subscription in subscriptions],
                "id": self._request_id,
            }
        )
        try:
            self._transport.send(payload)
        except MarketDataConnectionError as exc:
            raise MarketDataSubscriptionError(
                "could not send the subscription frame",
                method=method,
                streams=[subscription.stream_name for subscription in subscriptions],
            ) from exc
        self._metrics = self._metrics.record(subscriptions_sent=1)

    def _next_frame(self) -> str | None:
        """Read one frame, reconnecting if the stream failed or fell silent.

        Returns:
            The frame text, or ``None`` when the caller should simply try again.

        Raises:
            MarketDataConnectionError: If reconnection exhausts its budget.
        """
        try:
            frame = self._transport.receive(self._config.receive_timeout_seconds)
        except MarketDataConnectionError as exc:
            _LOGGER.warning(
                "transport failed while receiving; reconnecting",
                extra={"feed": self.name, "error": type(exc).__name__},
            )
            self._reconnect()
            return None
        if frame is None:
            if self._clock.is_heartbeat_expired(self._config.heartbeat_timeout_seconds):
                self._metrics = self._metrics.record(heartbeat_timeouts=1)
                _LOGGER.warning(
                    "heartbeat expired; reconnecting",
                    extra={
                        "feed": self.name,
                        "heartbeat_timeout_seconds": (self._config.heartbeat_timeout_seconds),
                    },
                )
                self._reconnect()
            return None
        self._clock.mark_frame()
        return frame

    def _reconnect(self) -> None:
        """Work through the backoff schedule until the stream is back.

        Continuity state is deliberately untouched, so the first candle after the stream
        returns is judged against the last one delivered before it went away. That single
        fact is what makes reconnection safe: a replayed candle is suppressed, a resumed
        stream that skipped ahead is caught as a gap, and one that rewound is refused.

        Raises:
            MarketDataConnectionError: If the attempt budget is exhausted.
        """
        _LOGGER.warning(
            "reconnecting",
            extra={
                "feed": self.name,
                "max_attempts": self._policy.schedule.max_attempts,
            },
        )
        self._transition(MarketDataFeedState.RECONNECTING)
        self._transport.close()
        self._clock.forget_frames()
        while True:
            try:
                delay = self._policy.next_delay()
            except MarketDataConnectionError:
                _LOGGER.error(
                    "reconnect budget exhausted; giving up",
                    extra={"feed": self.name, "attempts": self._policy.attempts},
                )
                raise
            _LOGGER.debug(
                "reconnect attempt",
                extra={
                    "feed": self.name,
                    "attempt": self._policy.attempts,
                    "delay_seconds": delay,
                },
            )
            self._sleep(delay)
            try:
                self._open()
            except (MarketDataConnectionError, MarketDataSubscriptionError) as exc:
                _LOGGER.warning(
                    "reconnect attempt failed",
                    extra={
                        "feed": self.name,
                        "attempt": self._policy.attempts,
                        "error": type(exc).__name__,
                    },
                )
                self._transport.close()
                self._transition(MarketDataFeedState.RECONNECTING)
                continue
            attempts_used = self._policy.attempts
            self._policy.reset()
            self._metrics = self._metrics.record(reconnects=1)
            _LOGGER.info("reconnected", extra={"feed": self.name, "attempts_used": attempts_used})
            return

    def _parse(self, frame: str) -> MarketBar | None:
        """Parse one frame, recording a malformed one on the way past.

        The counter is incremented and the failure is then re-raised unchanged. Counting is
        not handling: a frame the venue mangled still stops the feed, exactly as it did
        before. What changes is only that the day's report can say the run ended on bad
        data, instead of the operator finding a stopped process and no reason.

        Raises:
            DataProviderError: If the frame is not valid JSON or not a JSON object.
            DataIntegrityError: If a candle is present but cannot be trusted.
            MarketDataSubscriptionError: If the candle names an unsubscribed instrument.
        """
        try:
            bar = self._parser.parse(frame)
        except (DataProviderError, DataIntegrityError) as exc:
            self._metrics = self._metrics.record(malformed_frames=1)
            _LOGGER.warning(
                "malformed frame",
                extra={"feed": self.name, "error": type(exc).__name__, "detail": str(exc)},
            )
            raise
        if bar is not None:
            _LOGGER.debug(
                "kline parsed",
                extra={
                    "feed": self.name,
                    "symbol": bar.symbol,
                    "close_time": bar.close_time.isoformat(),
                    "is_closed": bar.is_closed,
                },
            )
        return bar

    def _apply(self, admission: CandleAdmission) -> MarketBar | None:
        """Account for one verdict and return the bar to deliver, if any.

        Raises:
            DataGapError: If the verdict is a continuity break.
        """
        if admission.outcome is CandleOutcome.FORMING:
            self._metrics = self._metrics.record(forming_suppressed=1)
            _LOGGER.debug(
                "candle still forming; suppressed",
                extra={"feed": self.name, "symbol": admission.bar.symbol},
            )
            return None
        if admission.outcome is CandleOutcome.DUPLICATE:
            self._metrics = self._metrics.record(duplicates_suppressed=1)
            _LOGGER.debug(
                "duplicate candle suppressed",
                extra={"feed": self.name, "symbol": admission.bar.symbol},
            )
            return None
        if admission.outcome is CandleOutcome.GAP:
            self._pause(admission.gap)
        self._metrics = self._metrics.record(bars_emitted=1)
        self._transition(MarketDataFeedState.STREAMING)
        return admission.bar

    def _pause(self, gap: GapReport | None) -> None:
        """Stop delivering and demand explicit recovery.

        Raises:
            DataGapError: Always. Detecting a hole and continuing anyway would be the one
                outcome worse than stopping.
        """
        self._transition(MarketDataFeedState.PAUSED)
        self._pending_gap = gap
        self._metrics = self._metrics.record(gaps_detected=1)
        _LOGGER.error(
            "candle gap detected; feed paused",
            extra={
                "feed": self.name,
                "gap": gap.describe() if gap is not None else None,
                "missing_bars": gap.missing_bars if gap is not None else None,
            },
        )
        raise DataGapError(
            gap.describe() if gap is not None else "market-data continuity break",
            feed=self.name,
            missing_bars=gap.missing_bars if gap is not None else None,
        )

    def _build_subscription(self, symbol: str) -> StreamSubscription:
        """Build the subscription record for a canonical symbol.

        Raises:
            MarketDataSubscriptionError: If the symbol is not in ``BASE/QUOTE`` form.
        """
        parts = symbol.split("/")
        if len(parts) != _SYMBOL_PARTS or not all(
            part.isalnum() and part.isupper() for part in parts
        ):
            raise MarketDataSubscriptionError(
                "symbol must be canonical BASE/QUOTE, for example BTC/USDT", symbol=symbol
            )
        return StreamSubscription(
            symbol=symbol,
            venue_symbol=self._config.venue_symbol(symbol),
            stream_name=self._config.stream_name(symbol),
            timeframe=self._config.timeframe,
        )
