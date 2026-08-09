"""Feed telemetry that crosses domain boundaries.

A live feed knows how healthy it has been. A daily report needs to say so. Neither package
may import the other — ``paper`` and ``reporting`` are both forbidden from reaching into
``marketdata``, and that isolation is what keeps a session unable to tell a socket from a
CSV replay. So the number has to travel through a neutral layer both sides already depend
on, exactly as risk and execution share their fee assumptions through
:mod:`quantplatform.core.models.execution_policy`.

This is that contract: an immutable reading of a feed's counters at one instant. The feed
produces it, a paper session carries it without looking inside, and a report turns it into
:class:`~quantplatform.reporting.models.FeedDiagnostics`. Nothing in this module knows what
a WebSocket is.

**A snapshot, not a live view.** Reading it twice gives the same answer; the feed keeps its
own running counters and hands out frozen copies. A report describing a day must not shift
underneath the process writing it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Self

from pydantic import Field, model_validator

from quantplatform.core.errors import FeedTelemetryRegressionError
from quantplatform.core.models.base import DomainModel

__all__ = ["ADDITIVE_FEED_COUNTERS", "ZERO_FEED_METRICS", "FeedMetricsSnapshot"]

ADDITIVE_FEED_COUNTERS: Final[tuple[str, ...]] = (
    "reconnect_count",
    "heartbeat_timeouts",
    "detected_gaps",
    "rejected_frames",
    "malformed_frames",
    "candles_received",
    "candles_accepted",
    "candles_rejected",
    "duplicate_candles",
)
"""Every counter that only climbs, and is therefore safe to subtract across a window.

Deliberately does not include :attr:`FeedMetricsSnapshot.acceptance_rate`: it is a ratio,
and the difference of two ratios is not the ratio of the difference.
"""


class FeedMetricsSnapshot(DomainModel):
    """An immutable reading of a market-data feed's own counters.

    Field names are the operational vocabulary a report speaks, which is deliberately not
    the internal vocabulary a feed adapter uses for its running counters. The adapter maps
    its own names onto these once, at the boundary, rather than every reader learning them.
    """

    reconnect_count: int = Field(default=0, ge=0)
    """Times the transport dropped and was re-established."""

    heartbeat_timeouts: int = Field(default=0, ge=0)
    """Times the stream fell silent past its budget and was presumed dead."""

    detected_gaps: int = Field(default=0, ge=0)
    """Continuity breaks found in the candle series. Each one paused the feed."""

    rejected_frames: int = Field(default=0, ge=0)
    """Frames carrying data the feed refused: unusable candles plus refused ones.

    Always at least :attr:`candles_rejected`; the difference is
    :attr:`malformed_frames`, which never became a candle at all.
    """

    malformed_frames: int = Field(default=0, ge=0)
    """Frames that could not be parsed, or whose candle failed validation.

    Each one also raised. The counter records that it happened; it does not mean the feed
    carried on past it.
    """

    candles_received: int = Field(default=0, ge=0)
    """Candles successfully parsed out of frames, before any sequence rule was applied."""

    candles_accepted: int = Field(default=0, ge=0)
    """Candles delivered to the trading pipeline."""

    candles_rejected: int = Field(default=0, ge=0)
    """Candles parsed but not delivered: still forming, or already seen."""

    duplicate_candles: int = Field(default=0, ge=0)
    """The share of :attr:`candles_rejected` that were republished candles.

    Broken out because it means something different from the rest: a duplicate after a
    reconnect is the venue behaving correctly, while a steady stream of them is not.
    """

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the counters describe a possible history.

        Raises:
            ValueError: If more candles were delivered than parsed, if the accepted and
                rejected counts exceed what was received, or if the frame counts contradict
                the candle counts.
        """
        if self.candles_accepted + self.candles_rejected > self.candles_received:
            msg = "candles accepted and rejected cannot exceed candles received"
            raise ValueError(msg)
        if self.rejected_frames < self.candles_rejected:
            msg = "rejected frames cannot be fewer than rejected candles"
            raise ValueError(msg)
        if self.malformed_frames > self.rejected_frames:
            msg = "malformed frames cannot exceed rejected frames"
            raise ValueError(msg)
        if self.duplicate_candles > self.candles_rejected:
            msg = "duplicate candles cannot exceed rejected candles"
            raise ValueError(msg)
        return self

    def delta_since(self, previous: FeedMetricsSnapshot) -> FeedMetricsSnapshot:
        """Return what happened between an earlier reading and this one.

        The feed's counters are cumulative and never reset, so a day's activity is the
        difference between the reading at its end and the reading at its start. Subtracting
        here rather than resetting at the feed keeps the feed ignorant of what a "day" is,
        and keeps a restart from erasing a window nobody has reported yet.

        **Only additive counters are subtracted.** A rate is not a counter: the difference
        between yesterday's acceptance rate and today's is not today's acceptance rate. The
        result recomputes its own :attr:`acceptance_rate` from the delta counts.

        Args:
            previous: The earlier reading, typically the last day's closing baseline.

        Returns:
            A snapshot whose every count covers the window between the two readings.

        Raises:
            FeedTelemetryRegressionError: If any counter is lower than it was. Counters only
                climb, so this means the readings do not describe one continuous run, and a
                negative daily count is not something to paper over.
        """
        regressions = {
            name: (getattr(previous, name), getattr(self, name))
            for name in ADDITIVE_FEED_COUNTERS
            if getattr(self, name) < getattr(previous, name)
        }
        if regressions:
            raise FeedTelemetryRegressionError(
                "feed counters moved backwards between readings",
                counters=sorted(regressions),
                observed={
                    name: f"{before} -> {now}"
                    for name, (before, now) in sorted(regressions.items())
                },
            )
        return FeedMetricsSnapshot(
            **{
                name: getattr(self, name) - getattr(previous, name)
                for name in ADDITIVE_FEED_COUNTERS
            }
        )

    @property
    def acceptance_rate(self) -> Decimal | None:
        """Return the share of parsed candles that reached the pipeline.

        Returns:
            The ratio, or ``None`` before any candle was parsed — a rate over zero
            observations is undefined, and reporting it as zero would read like a total
            feed failure rather than a quiet start.
        """
        if self.candles_received == 0:
            return None
        return Decimal(self.candles_accepted) / Decimal(self.candles_received)

    @property
    def is_clean(self) -> bool:
        """Return whether the feed ran without a gap, a reconnect or a heartbeat loss."""
        return (
            self.detected_gaps == 0
            and self.reconnect_count == 0
            and self.heartbeat_timeouts == 0
            and self.malformed_frames == 0
        )


ZERO_FEED_METRICS: Final[FeedMetricsSnapshot] = FeedMetricsSnapshot()
"""The baseline a session starts from, before any day has been reported.

Using an explicit zero rather than ``None`` is what makes the first day's report cover
everything the feed did since the session began, instead of covering nothing.
"""
