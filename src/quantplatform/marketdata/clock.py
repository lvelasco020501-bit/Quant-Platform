"""Feed time, derived from an injected clock.

The market-data layer reads no wall clock. Every instant and every duration comes through
:class:`~quantplatform.core.clock.Clock`, which is what lets a test drive a feed through a
dropped connection, a heartbeat expiry and a full backoff schedule in microseconds and get
exactly the behaviour a real outage would produce.

Two distinct time questions live here, and they are answered from two different sources on
purpose. *Has this candle finished?* is a question about market time, answered from
:meth:`~quantplatform.core.clock.Clock.now`. *Has the connection gone quiet?* is a question
about elapsed duration, answered from :meth:`~quantplatform.core.clock.Clock.monotonic`,
because a wall clock that steps backwards over an NTP correction would otherwise report a
silent socket as healthy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from quantplatform.core.clock import Clock
from quantplatform.core.models.market import MarketBar
from quantplatform.core.timeutils import ensure_utc

__all__ = ["FeedClock"]


class FeedClock:
    """The clock a live feed reads, plus the two questions it asks of one."""

    def __init__(self, clock: Clock, *, close_grace_seconds: float = 0.0) -> None:
        """Wrap an injected clock.

        Args:
            clock: The platform clock; real in production, simulated in tests.
            close_grace_seconds: Tolerance for our clock lagging the venue's when
                confirming that a candle's interval has elapsed. See :meth:`is_bar_final`
                for why this is subtracted rather than added.
        """
        self._clock = clock
        self._grace = timedelta(seconds=close_grace_seconds)
        self._last_frame_monotonic: float | None = None

    @property
    def clock(self) -> Clock:
        """Return the underlying platform clock."""
        return self._clock

    def now(self) -> datetime:
        """Return the current instant, always timezone-aware UTC."""
        return ensure_utc(self._clock.now())

    def mark_frame(self) -> None:
        """Record that traffic has just arrived, restarting the heartbeat window."""
        self._last_frame_monotonic = self._clock.monotonic()

    def forget_frames(self) -> None:
        """Discard the heartbeat window.

        Called when a connection is torn down: the silence before a reconnect says nothing
        about the health of the connection that replaces it.
        """
        self._last_frame_monotonic = None

    def seconds_since_frame(self) -> float | None:
        """Return how long the stream has been silent.

        Returns:
            Seconds since the last frame, or ``None`` when nothing has arrived yet.
        """
        if self._last_frame_monotonic is None:
            return None
        return self._clock.monotonic() - self._last_frame_monotonic

    def is_heartbeat_expired(self, timeout_seconds: float) -> bool:
        """Return whether the stream has been silent longer than the tolerated window.

        A feed that has not yet received its first frame is never expired: the connection
        has not had a chance to prove itself, and reconnecting from that state would loop
        against a venue that is merely slow to send the first update.

        Args:
            timeout_seconds: Silence budget.

        Returns:
            ``True`` when the connection should be presumed dead.
        """
        elapsed = self.seconds_since_frame()
        if elapsed is None:
            return False
        return elapsed >= timeout_seconds

    def is_bar_final(self, bar: MarketBar) -> bool:
        """Return whether a candle's interval has elapsed on our clock.

        Independent of the venue's own closed flag. Both must agree before a bar is acted
        on, so a provider that mislabels a forming candle cannot get one into the pipeline.

        **The grace period is subtracted, not added, and that direction is load-bearing.**
        A venue publishes each closed candle exactly once. Requiring the clock to reach
        ``close_time + grace`` would reject a candle that arrived a few hundred
        milliseconds after its close — and since it is never re-sent, the feed would lose
        it permanently and then report the hole it just created as a gap. Subtracting
        instead forgives our clock running slightly behind the venue's, which is the only
        skew that can actually occur here. It can never admit a forming candle, because
        :attr:`~quantplatform.core.models.market.MarketBar.is_closed` is checked
        independently and a candle the venue has not closed is refused whatever the clock
        says.

        Args:
            bar: Candle to test.

        Returns:
            ``True`` when the clock has reached the candle's close, within tolerance.
        """
        return self.now() >= ensure_utc(bar.close_time) - self._grace

    def seconds_until_final(self, bar: MarketBar) -> float:
        """Return how long until a candle counts as final, zero once it already does."""
        remaining = (ensure_utc(bar.close_time) - self._grace) - self.now()
        return max(0.0, remaining.total_seconds())
