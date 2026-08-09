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

_SUBSCRIBE: Final[str] = "SUBSCRIBE"
_UNSUBSCRIBE: Final[str] = "UNSUBSCRIBE"
_MAX_FRAME_BYTES: Final[int] = 1 << 20
"""One megabyte. A candle frame is a few hundred bytes; anything approaching this is not
market data, and an unbounded reader is how a stream becomes a memory exhaustion bug."""

_SYMBOL_PARTS: Final[int] = 2
"""A canonical symbol is exactly ``BASE/QUOTE``."""


class WebSocketCandleTransport:
    """A :class:`~quantplatform.core.interfaces.CandleStreamTransport` over a real socket.

    Synchronous on purpose. The paper runner and session are single-threaded and loop-free
    by design, and putting an event loop underneath them would trade that determinism for
    nothing a single market-data stream needs.

    Holds no market-data logic whatsoever, which is what keeps the untestable part of this
    phase — the part that needs an actual network — down to opening a socket and reading
    text off it.
    """

    def __init__(
        self, *, open_timeout_seconds: float = 10.0, close_timeout_seconds: float = 5.0
    ) -> None:
        """Create a transport.

        Args:
            open_timeout_seconds: How long to wait for the handshake.
            close_timeout_seconds: How long to wait for a graceful close.
        """
        self._open_timeout = open_timeout_seconds
        self._close_timeout = close_timeout_seconds
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
        try:
            message = connection.recv(timeout=timeout_seconds)
        except TimeoutError:
            return None
        except (WebSocketException, OSError) as exc:
            raise MarketDataConnectionError(
                "market-data stream failed while receiving", error=type(exc).__name__
            ) from exc
        if isinstance(message, bytes):
            return message.decode("utf-8", errors="replace")
        return message

    def close(self) -> None:
        """Release the channel. Safe to call repeatedly, and never raises.

        A failure while closing a socket that is already being abandoned carries no
        information worth propagating, and letting it escape would mean a shutdown path
        that fails during shutdown.
        """
        connection, self._connection = self._connection, None
        if connection is None:
            return
        with suppress(WebSocketException, OSError):
            connection.close()

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
            self._state = MarketDataFeedState.DISCONNECTED

    def close(self) -> None:
        """Release everything and refuse further streaming. Safe to call repeatedly."""
        self._stopping = True
        self._transport.close()
        self._clock.forget_frames()
        self._state = MarketDataFeedState.STOPPED

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
        self._state = MarketDataFeedState.CONNECTING
        self._metrics = self._metrics.record(connection_attempts=1)
        self._transport.connect(self._config.websocket_url)
        self._state = MarketDataFeedState.CONNECTED
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
        except MarketDataConnectionError:
            self._reconnect()
            return None
        if frame is None:
            if self._clock.is_heartbeat_expired(self._config.heartbeat_timeout_seconds):
                self._metrics = self._metrics.record(heartbeat_timeouts=1)
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
        self._state = MarketDataFeedState.RECONNECTING
        self._transport.close()
        self._clock.forget_frames()
        while True:
            delay = self._policy.next_delay()
            self._sleep(delay)
            try:
                self._open()
            except (MarketDataConnectionError, MarketDataSubscriptionError):
                self._transport.close()
                self._state = MarketDataFeedState.RECONNECTING
                continue
            self._policy.reset()
            self._metrics = self._metrics.record(reconnects=1)
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
            return self._parser.parse(frame)
        except (DataProviderError, DataIntegrityError):
            self._metrics = self._metrics.record(malformed_frames=1)
            raise

    def _apply(self, admission: CandleAdmission) -> MarketBar | None:
        """Account for one verdict and return the bar to deliver, if any.

        Raises:
            DataGapError: If the verdict is a continuity break.
        """
        if admission.outcome is CandleOutcome.FORMING:
            self._metrics = self._metrics.record(forming_suppressed=1)
            return None
        if admission.outcome is CandleOutcome.DUPLICATE:
            self._metrics = self._metrics.record(duplicates_suppressed=1)
            return None
        if admission.outcome is CandleOutcome.GAP:
            self._pause(admission.gap)
        self._metrics = self._metrics.record(bars_emitted=1)
        self._state = MarketDataFeedState.STREAMING
        return admission.bar

    def _pause(self, gap: GapReport | None) -> None:
        """Stop delivering and demand explicit recovery.

        Raises:
            DataGapError: Always. Detecting a hole and continuing anyway would be the one
                outcome worse than stopping.
        """
        self._state = MarketDataFeedState.PAUSED
        self._pending_gap = gap
        self._metrics = self._metrics.record(gaps_detected=1)
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
