"""Reconnection policy: how long to wait, and when to stop waiting.

Exponential backoff with a ceiling and a finite attempt budget. Two choices here are
deliberate and worth stating, because both are places where the conventional answer is
wrong for this component.

**No jitter.** Jitter exists to desynchronise a fleet of clients stampeding a recovering
server. A trading process runs one feed against one venue, so jitter buys nothing — and it
would cost the property that makes the whole platform testable, namely that the same inputs
produce the same run.

**A finite budget.** Retrying forever is the usual default and it is the dangerous one
here: it converts an outage into a silent stall. A paper session that quietly stops
receiving candles keeps reporting the portfolio it had an hour ago, and every metric it
publishes stays plausible while being wrong. Exhausting the budget raises instead, so the
run ends where the data ended.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantplatform.core.errors import DomainValidationError, MarketDataConnectionError

__all__ = ["BackoffSchedule", "ReconnectPolicy"]


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
        grown = self.initial_delay_seconds * (self.multiplier ** (attempt - 1))
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

        Returns:
            The delay in seconds.

        Raises:
            MarketDataConnectionError: If the attempt budget is already spent. Raising
                rather than returning a sentinel is the point: a caller cannot accidentally
                treat "give up" as "wait zero seconds and try again".
        """
        if self.is_exhausted:
            raise MarketDataConnectionError(
                "market-data reconnection budget exhausted",
                attempts=self._attempts,
                max_attempts=self._schedule.max_attempts,
            )
        self._attempts += 1
        return self._schedule.delay_for(self._attempts)

    def reset(self) -> None:
        """Clear the failure count after a connection has proved itself.

        Called once a reconnected stream has been re-established, so an outage tomorrow
        starts from a full budget rather than inheriting today's.
        """
        self._attempts = 0
