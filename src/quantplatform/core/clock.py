"""Application clock abstraction.

No platform component may read wall-clock time directly. Time is injected through the
:class:`Clock` port so that backtests replay a historical timeline deterministically while
paper, shadow and live modes share exactly the same code paths.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from quantplatform.core.errors import DomainValidationError
from quantplatform.core.timeutils import ensure_utc, utc_now

__all__ = ["Clock", "SimulatedClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Source of time for the whole platform.

    Implementations must return timezone-aware UTC instants from :meth:`now` and must
    guarantee that :meth:`monotonic` never decreases.
    """

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonically increasing seconds counter for measuring durations."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the caller for ``seconds`` on this clock's timeline."""
        ...


class SystemClock:
    """Real clock backed by the operating system.

    Used by paper, shadow and live modes.
    """

    __slots__ = ()

    def now(self) -> datetime:
        """Return the current UTC wall-clock instant."""
        return utc_now()

    def monotonic(self) -> float:
        """Return the process monotonic counter in seconds."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Await ``seconds`` of real time.

        Args:
            seconds: Non-negative duration to wait.

        Raises:
            DomainValidationError: If ``seconds`` is negative.
        """
        if seconds < 0:
            raise DomainValidationError("sleep duration must not be negative", seconds=seconds)
        await asyncio.sleep(seconds)


class SimulatedClock:
    """Deterministic clock driven explicitly by the caller.

    Used by backtests and by tests. Time only moves when :meth:`advance`, :meth:`set_time`
    or :meth:`sleep` is called, and it can never move backwards, which mirrors the
    guarantees callers rely on from the real clock.

    Args:
        start: Initial timezone-aware instant.

    Raises:
        DomainValidationError: If ``start`` is naive.
    """

    __slots__ = ("_monotonic", "_now")

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)
        self._monotonic = 0.0

    def now(self) -> datetime:
        """Return the current simulated instant."""
        return self._now

    def monotonic(self) -> float:
        """Return the simulated monotonic counter in seconds."""
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Advance simulated time by ``seconds`` without waiting in real time.

        Args:
            seconds: Non-negative duration to advance.

        Raises:
            DomainValidationError: If ``seconds`` is negative.
        """
        if seconds < 0:
            raise DomainValidationError("sleep duration must not be negative", seconds=seconds)
        self.advance(timedelta(seconds=seconds))

    def advance(self, delta: timedelta) -> datetime:
        """Move simulated time forward.

        Args:
            delta: Non-negative amount of time to advance.

        Returns:
            The new simulated instant.

        Raises:
            DomainValidationError: If ``delta`` is negative.
        """
        if delta < timedelta(0):
            raise DomainValidationError(
                "clock cannot advance by a negative delta", delta=str(delta)
            )
        self._now = self._now + delta
        self._monotonic += delta.total_seconds()
        return self._now

    def set_time(self, value: datetime) -> datetime:
        """Jump simulated time to an explicit instant.

        Args:
            value: Timezone-aware instant that must not precede the current time.

        Returns:
            The new simulated instant.

        Raises:
            DomainValidationError: If ``value`` is naive or moves the clock backwards.
        """
        target = ensure_utc(value)
        if target < self._now:
            raise DomainValidationError(
                "clock cannot move backwards",
                current=self._now.isoformat(),
                requested=target.isoformat(),
            )
        return self.advance(target - self._now)
