"""Reconnection policy: how long to wait, and how the wait is bounded.

Exponential backoff with a ceiling and no jitter. Jitter exists to desynchronise a fleet
of clients stampeding a recovering server; a trading process runs one feed against one
venue, so it buys nothing here — and it would cost the property that makes the whole
platform testable, namely that the same inputs produce the same run.

**Retrying is not bounded by a count, on purpose.** It once was: an attempt budget that
raised once spent, reasoned from the fear of a feed that stalls silently while a session
keeps reporting a stale, plausible-looking portfolio. That fear was real but the fix was
wrong for a process meant to run seven days unattended — a transport hiccup that took six
reconnection attempts to clear, on infrastructure that had otherwise run cleanly for two
days, ended a session in thirty-one seconds. The failure this format actually needs to
catch is a session trading on data that stopped arriving, and that is what
:class:`~quantplatform.core.errors.DataGapError` and the session's stall watchdog already
exist to catch — the first by refusing to trade across a hole in the series, the second by
observing the pipeline directly rather than counting an unrelated proxy for it. Neither of
them cares how many times the transport had to retry to get there. What is now bounded
instead is the *delay* between attempts, which is what keeps an outage from burning CPU or
spamming the venue — :attr:`BackoffSchedule.max_delay_seconds` still caps every wait, no
matter how long the outage or how many attempts it has taken.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from quantplatform.core.errors import DomainValidationError

__all__ = ["BackoffSchedule", "ReconnectPolicy"]

_MAX_GROWTH_EXPONENT: Final[int] = 50
"""Ceiling on the exponent :meth:`BackoffSchedule.delay_for` will actually compute.

An outage lasting long enough — hours, now that reconnection has no attempt limit — pushes
the attempt number into the thousands, and ``multiplier ** (attempt - 1)`` would overflow
a float long before that (verified: ``2.0 ** 1029`` raises ``OverflowError`` on CPython).
Fifty doublings of any sane ``initial_delay_seconds`` already exceeds any sane
``max_delay_seconds`` by many orders of magnitude, so capping the exponent here changes
nothing observable — the result was always going to be clamped to the ceiling — and it
costs nothing to compute regardless of how many attempts an outage has actually taken.
"""


@dataclass(frozen=True, slots=True)
class BackoffSchedule:
    """The delay curve and attempt budget for reconnection.

    Args:
        initial_delay_seconds: Wait before the first retry.
        max_delay_seconds: Ceiling the growing delay is clamped to.
        multiplier: Growth factor per attempt; ``1.0`` gives a constant delay.
        max_attempts: Consecutive failures tolerated before giving up.
    """

    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0
    max_attempts: int = 5
    """No longer a point at which reconnection stops. Kept as the threshold
    :attr:`ReconnectPolicy.is_exhausted` reports once crossed — informational only, for a
    caller that wants to know an outage has run longer than the nominal, everyday case."""

    def __post_init__(self) -> None:
        """Check the schedule is usable.

        Raises:
            DomainValidationError: If any parameter is out of range or the ceiling sits
                below the initial delay.
        """
        if self.initial_delay_seconds <= 0:
            raise DomainValidationError(
                "initial_delay_seconds must be strictly positive",
                initial_delay_seconds=self.initial_delay_seconds,
            )
        if self.multiplier < 1:
            raise DomainValidationError("multiplier must be at least 1", multiplier=self.multiplier)
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise DomainValidationError(
                "max_delay_seconds must not be below initial_delay_seconds",
                max_delay_seconds=self.max_delay_seconds,
                initial_delay_seconds=self.initial_delay_seconds,
            )
        if self.max_attempts < 1:
            raise DomainValidationError(
                "max_attempts must be at least 1", max_attempts=self.max_attempts
            )

    def delay_for(self, attempt: int) -> float:
        """Return the delay preceding a given attempt.

        Args:
            attempt: One-based attempt number.

        Returns:
            ``initial * multiplier ** (attempt - 1)``, clamped to the ceiling.

        Raises:
            DomainValidationError: If ``attempt`` is not at least one.
        """
        if attempt < 1:
            raise DomainValidationError("attempt must be at least 1", attempt=attempt)
        exponent = min(attempt - 1, _MAX_GROWTH_EXPONENT)
        grown = self.initial_delay_seconds * (self.multiplier**exponent)
        return min(grown, self.max_delay_seconds)

    def permits(self, attempt: int) -> bool:
        """Return whether a given one-based attempt is still within budget."""
        return 1 <= attempt <= self.max_attempts


class ReconnectPolicy:
    """Tracks consecutive failures and hands out the next delay."""

    def __init__(self, schedule: BackoffSchedule | None = None) -> None:
        """Create a policy.

        Args:
            schedule: Delay curve and budget; a conservative default is used when omitted.
        """
        self._schedule = schedule if schedule is not None else BackoffSchedule()
        self._attempts = 0

    @property
    def schedule(self) -> BackoffSchedule:
        """Return the schedule in force."""
        return self._schedule

    @property
    def attempts(self) -> int:
        """Return how many consecutive failures have been recorded."""
        return self._attempts

    @property
    def is_exhausted(self) -> bool:
        """Return whether the attempt budget has been spent."""
        return self._attempts >= self._schedule.max_attempts

    def next_delay(self) -> float:
        """Consume one attempt and return how long to wait before it.

        Never refuses. An attempt past the nominal budget still gets a delay, clamped to
        the same ceiling as every other attempt — see :attr:`is_exhausted` for the signal
        that the nominal budget has been spent, which a caller may use for observability
        but which no longer stops reconnection on its own.

        Returns:
            The delay in seconds.
        """
        self._attempts += 1
        return self._schedule.delay_for(self._attempts)

    def reset(self) -> None:
        """Clear the failure count after a connection has proved itself.

        Called once a reconnected stream has been re-established, so an outage tomorrow
        starts from a full budget rather than inheriting today's.
        """
        self._attempts = 0
