"""Operational telemetry that crosses domain boundaries.

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

:class:`SymbolRulesTelemetry` travels the same road for the same reason — orchestration
refreshes the venue's trading rules, reporting must be able to say whether that is still
working — and is kept deliberately separate from the feed's counters. They answer different
questions about different subsystems, and a single merged blob would let a healthy feed
disguise a refresh that has been failing for two days.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final, Self

from pydantic import Field, model_validator

from quantplatform.core.errors import FeedTelemetryRegressionError
from quantplatform.core.models.base import DomainModel

__all__ = [
    "ADDITIVE_FEED_COUNTERS",
    "ZERO_FEED_METRICS",
    "FeedMetricsSnapshot",
    "SymbolRulesTelemetry",
]

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


class SymbolRulesTelemetry(DomainModel):
    """An immutable reading of how the venue's trading rules are being kept current.

    Venue rules are fetched, not assumed, and the risk engine refuses to trade on rules
    older than its freshness budget. A week-long run therefore depends on a refresh loop
    that nobody watches — which is precisely the kind of thing that fails quietly. This is
    what makes it watchable.

    **Cumulative for the session, plus a current condition.** The three counters only climb,
    so they are the audit trail: how often refresh was attempted across the whole run and
    how often it worked. :attr:`consecutive_failures` and :attr:`age_seconds` describe *now*,
    which is what health should be graded on — a single failure on Monday must not keep a
    Friday report yellow, and a refresh that has been failing since Monday must not look
    fine because it succeeded a hundred times before that.

    Deliberately **not** part of :class:`FeedMetricsSnapshot`. A feed can be flawless while
    the rules behind it rot, and merging the two would let one hide the other.
    """

    refresh_attempts: int = Field(default=0, ge=0)
    """Times a refresh was tried, successful or not."""

    refresh_successes: int = Field(default=0, ge=0)
    """Times a refresh returned usable rules and replaced the stored snapshot."""

    refresh_failures: int = Field(default=0, ge=0)
    """Times a refresh could not be completed. The previous rules were kept."""

    consecutive_failures: int = Field(default=0, ge=0)
    """Failures since the last success, reset to zero by a success.

    The field that says whether refresh is broken *now*. A cumulative failure count cannot:
    it never falls, so a transient blip on day one would keep a report yellow all week and
    an operator would learn to disregard it.
    """

    rule_changes: int = Field(default=0, ge=0)
    """Refreshes that arrived with limits differing from the ones in force.

    Expected to be rare and worth surfacing when it is not: a venue that changed its tick
    size mid-run has changed what every subsequent order may look like.
    """

    working_order_conflicts: int = Field(default=0, ge=0)
    """Working orders observed to violate the rules that replaced the ones they were
    placed under.

    Recorded, never acted on. Rewriting a live order to match a rule change is an execution
    decision, and this telemetry exists to report a condition rather than to trade on it.
    """

    last_refresh_at: datetime | None = None
    """When rules were last *successfully* refreshed, or ``None`` if never since startup.

    Failures deliberately do not move it. A timestamp that advanced on failure would make
    stale rules look freshly checked, which is the exact confusion this whole mechanism
    exists to prevent.
    """

    last_failure_reason: str | None = None
    """Why the most recent failure happened, for the report to quote. Never a credential:
    the provider it comes from has none to leak."""

    age_seconds: float = Field(default=0.0, ge=0.0)
    """Age of the oldest stored rules at the moment this reading was taken."""

    stale_after_seconds: int = Field(default=0, ge=0)
    """The freshness budget the risk engine will enforce against those rules.

    Carried alongside the age so the reading is self-describing: reporting can grade
    staleness without importing the risk package or being told the limit a second time,
    which is how two configured copies of one number start to disagree.
    """

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the counters describe a possible history.

        Raises:
            ValueError: If successes and failures do not add up to the attempts, if more
                failures are consecutive than ever happened, or if a success is claimed
                without a timestamp.
        """
        if self.refresh_successes + self.refresh_failures > self.refresh_attempts:
            msg = "refresh successes and failures cannot exceed attempts"
            raise ValueError(msg)
        if self.consecutive_failures > self.refresh_failures:
            msg = "consecutive failures cannot exceed total failures"
            raise ValueError(msg)
        if self.refresh_successes > 0 and self.last_refresh_at is None:
            msg = "a successful refresh must carry the time it happened"
            raise ValueError(msg)
        return self

    @property
    def is_stale(self) -> bool:
        """Return whether the stored rules have outlived the risk engine's budget.

        When this is true the risk engine is already refusing every intent for
        ``symbol_rules_freshness``, so a report that showed anything other than red would be
        describing a session that has silently stopped trading.
        """
        return self.stale_after_seconds > 0 and self.age_seconds >= self.stale_after_seconds

    @property
    def is_refreshing(self) -> bool:
        """Return whether the most recent refresh attempt succeeded.

        True before the first attempt: nothing has failed yet, and a fresh startup fetch is
        what put the rules there in the first place.
        """
        return self.consecutive_failures == 0
