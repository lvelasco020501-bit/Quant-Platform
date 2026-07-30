"""Closed-candle policy.

A candle is actionable only once it can no longer change. The platform's rule is::

    now >= close_time + grace_period

The grace period exists because a venue may finalise a candle marginally after its nominal
close; it is configuration, not a constant, so a source that publishes promptly can run
with zero grace.

Time always comes from the injected :class:`~quantplatform.core.clock.Clock`; this module
never reads the wall clock, which is what lets tests drive the boundary exactly with
:class:`~quantplatform.core.clock.SimulatedClock`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from quantplatform.core.clock import Clock
from quantplatform.core.timeutils import ensure_utc

__all__ = ["ClosedCandlePolicy"]


class ClosedCandlePolicy:
    """Decides whether a candle has finalised, as of an injected clock.

    Args:
        clock: Source of the current instant.
        grace_period: Additional settling time required beyond the nominal close.
        reference_time: Optional fixed instant to evaluate against instead of the clock,
            used by historical imports so that a backfill is judged against the moment the
            data was captured rather than against today.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        grace_period: timedelta = timedelta(0),
        reference_time: datetime | None = None,
    ) -> None:
        self._clock = clock
        self._grace_period = grace_period
        self._reference_time = ensure_utc(reference_time) if reference_time is not None else None

    @property
    def grace_period(self) -> timedelta:
        """Return the configured settling time beyond a candle's nominal close."""
        return self._grace_period

    def reference_now(self) -> datetime:
        """Return the instant closure is evaluated against.

        Returns:
            The fixed reference time when one was supplied, otherwise the clock's current
            instant.
        """
        if self._reference_time is not None:
            return self._reference_time
        return ensure_utc(self._clock.now())

    def finalises_at(self, close_time: datetime) -> datetime:
        """Return the instant at which a candle closing at ``close_time`` becomes final."""
        return ensure_utc(close_time) + self._grace_period

    def is_closed(self, close_time: datetime) -> bool:
        """Return whether a candle closing at ``close_time`` has finalised.

        Args:
            close_time: The candle's exclusive close timestamp.

        Returns:
            ``True`` once the reference instant has reached the close time plus the grace
            period.
        """
        return self.reference_now() >= self.finalises_at(close_time)
