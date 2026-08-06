"""Session time, derived from an injected clock.

No business logic here reads a wall clock. Everything comes through
:class:`~quantplatform.core.clock.Clock`, so a test can drive a session through days of
market time in microseconds and get the same decisions a real run would make.

What this adds over the bare clock is the two time questions a session actually asks: how
long have I been running, and has this candle finished yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from quantplatform.core.clock import Clock
from quantplatform.core.models.market import MarketBar
from quantplatform.core.timeutils import ensure_utc

__all__ = ["SessionClock"]


class SessionClock:
    """The clock a paper session reads, plus the questions it asks of one."""

    def __init__(self, clock: Clock, *, close_grace_seconds: int = 0) -> None:
        """Wrap an injected clock.

        Args:
            clock: The platform clock; real in production, simulated in tests.
            close_grace_seconds: Extra time beyond a bar's close before it counts as final,
                absorbing the small delay between a candle ending and a venue publishing it.
        """
        self._clock = clock
        self._grace = timedelta(seconds=close_grace_seconds)
        self._started_at: datetime | None = None
        self._started_monotonic: float | None = None

    @property
    def clock(self) -> Clock:
        """Return the underlying platform clock."""
        return self._clock

    def now(self) -> datetime:
        """Return the current instant, always timezone-aware UTC."""
        return ensure_utc(self._clock.now())

    def mark_start(self) -> datetime:
        """Record the session's start instant, the first time it is called.

        Idempotent on purpose: a session that stops and resumes keeps its original start, so
        uptime measures the life of the session rather than of the current process.

        Returns:
            The session's start instant.
        """
        if self._started_at is None:
            self._started_at = self.now()
            self._started_monotonic = self._clock.monotonic()
        return self._started_at

    def adopt_start(self, started_at: datetime) -> None:
        """Adopt a start instant recovered from persisted state.

        Args:
            started_at: The instant the session originally began.
        """
        self._started_at = ensure_utc(started_at)
        if self._started_monotonic is None:
            self._started_monotonic = self._clock.monotonic()

    @property
    def started_at(self) -> datetime | None:
        """Return the session's start instant, or ``None`` before it started."""
        return self._started_at

    def uptime_seconds(self) -> float:
        """Return how long this process has been running the session.

        Measured on the monotonic counter, not by subtracting timestamps, so a system clock
        correction cannot make a session appear to have run for a negative length of time.
        """
        if self._started_monotonic is None:
            return 0.0
        return max(0.0, self._clock.monotonic() - self._started_monotonic)

    def is_bar_final(self, bar: MarketBar) -> bool:
        """Return whether a bar has closed far enough in the past to be acted on.

        Args:
            bar: The bar in question.

        Returns:
            ``True`` once the clock has passed the bar's exclusive close time plus the
            configured grace period.
        """
        return self.now() >= bar.close_time + self._grace

    def seconds_until_final(self, bar: MarketBar) -> float:
        """Return how long remains before a bar may be acted on, zero if it already may."""
        remaining = (bar.close_time + self._grace - self.now()).total_seconds()
        return max(0.0, remaining)
