"""An independent observer that notices when the pipeline stops making progress.

The incident this module answers: a paper session's process stayed alive for close to
eight hours — TCP socket ``ESTABLISHED``, near-zero CPU, no crash, no reconnect logged —
while processing not one closed candle. Every existing safety net (heartbeat timeout,
reconnect budget, gap detection) lives *inside* the same blocking pump loop that had
stopped returning control to Python, so none of them could fire; a failure inside the
loop cannot be caught by a check that only runs when the loop runs.

**Why this has to be a second thread, in a platform built to have none.** Every other
component here — :mod:`quantplatform.paper.runner` foremost — is deliberately
synchronous, for reproducibility. That guarantee is unaffected by this module: the
watchdog never submits a bar, never touches strategy, risk, execution or portfolio state,
and never reaches into the feed. It only reads two already-published readings on a timer
and logs. A single narrowly-scoped exception to "no threads," made *because* it is the
only way to detect the one failure mode nothing inside the loop can detect about itself —
the same reason a resting pulse cannot be checked by asking yourself whether you are still
breathing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from quantplatform.core.clock import Clock
from quantplatform.core.errors import ConfigurationError
from quantplatform.core.logging_config import get_logger
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.paper.results import RuntimeMetrics

__all__ = ["StallDiagnosis", "StallWatchdog"]

_LOGGER = get_logger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class StallDiagnosis:
    """Where the watchdog concluded the pipeline last made progress, and since when."""

    stage: str
    """A short, stable identifier for the stalled stage. Never free text, so a report or
    an alerting rule can match on it: ``feed_delivery``, ``feed_telemetry_frozen``,
    ``session_rejection``, or ``unknown_no_progress``."""

    detail: str
    """A human-readable explanation of the evidence behind :attr:`stage`."""

    seconds_since_last_bar: float
    """How long it has been since a bar was actually processed, measured against the
    injected clock — the one figure in this class the watchdog knows with certainty,
    independent of anything the stalled loop itself reported."""


class StallWatchdog:
    """Alerts when no closed bar has been processed for longer than a timeframe allows.

    Read-only and side-effect-free with respect to the trading pipeline: it calls two
    callables handed to it at construction — a reader of the session's
    :class:`~quantplatform.paper.results.RuntimeMetrics` and a reader of the last feed
    telemetry the session was given — compares them against the injected
    :class:`~quantplatform.core.clock.Clock`, and logs. Nothing it does can change a
    trading decision.

    **A known, honest limit, stated once here rather than left to be discovered.** The
    feed reading it reads is only as fresh as the pump loop's last completed iteration.
    If that loop is the thing that stalled, the feed reading freezes at the instant the
    stall began and stays frozen for as long as the stall lasts — which is itself
    diagnostic (:attr:`StallDiagnosis.stage` ``"feed_telemetry_frozen"``), but it cannot,
    by itself, distinguish "the feed died at that instant" from "the feed is fine and
    something downstream never returned for the next reading." What the watchdog *can*
    say with certainty, because it comes from the clock rather than from anything the
    stalled loop produced, is how long it has been since a bar was actually processed.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        threshold_seconds: float,
        session_metrics: Callable[[], RuntimeMetrics],
        feed_metrics: Callable[[], FeedMetricsSnapshot | None],
        session_id: str,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        started_at: datetime | None = None,
    ) -> None:
        """Wire a watchdog.

        Args:
            clock: Injected time source. In production the same
                :class:`~quantplatform.core.clock.SystemClock` the session itself uses;
                a test drives it with a
                :class:`~quantplatform.core.clock.SimulatedClock` and calls
                :meth:`check_once` directly, with no real thread involved.
            threshold_seconds: How long a session may go without processing a bar before
                this is a stall rather than an ordinary gap between candles. Expected to
                be the timeframe's own duration plus an explicit margin — never the bare
                timeframe, which would alert on every single bar.
            session_metrics: Returns the session's current
                :class:`~quantplatform.paper.results.RuntimeMetrics`.
            feed_metrics: Returns the feed telemetry the session was last given, or
                ``None`` if none has ever arrived.
            session_id: Carried on every log line, so a multi-session log can be filtered.
            poll_interval_seconds: How often the background thread checks. Irrelevant to
                :meth:`check_once`, which a test calls directly without waiting.
            started_at: When the session itself began, used as the liveness anchor before
                any bar has ever been processed. Defaults to ``clock.now()`` at
                construction.

        Raises:
            ConfigurationError: If ``threshold_seconds`` or ``poll_interval_seconds`` is
                not strictly positive.
        """
        if threshold_seconds <= 0:
            raise ConfigurationError(
                "the stall threshold must be strictly positive",
                threshold_seconds=threshold_seconds,
            )
        if poll_interval_seconds <= 0:
            raise ConfigurationError(
                "the watchdog poll interval must be strictly positive",
                poll_interval_seconds=poll_interval_seconds,
            )
        self._clock = clock
        self._threshold = float(threshold_seconds)
        self._session_metrics = session_metrics
        self._feed_metrics = feed_metrics
        self._session_id = session_id
        self._poll_interval = float(poll_interval_seconds)
        self._started_at = started_at if started_at is not None else clock.now()

        self._previous_feed_reading: FeedMetricsSnapshot | None = None
        self._alerted = False
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()

    # --- The pure check, exercised directly by tests -----------------------------------------

    def check_once(self) -> StallDiagnosis | None:
        """Perform one liveness check and return a diagnosis if the pipeline is stalled.

        Reads the injected clock and the two callables given at construction; touches
        nothing else and starts no thread, so a test exercises the full diagnosis logic
        by calling this directly against a :class:`~quantplatform.core.clock.SimulatedClock`.

        Returns:
            A diagnosis if more than the configured threshold has passed since a bar was
            last processed, else ``None``. Logs a single ``CRITICAL`` line the moment a
            stall is first detected, and a single recovery line when it clears; a stall
            that is still ongoing at the next poll is not logged again, so a long outage
            produces one alert rather than one every poll interval.
        """
        runtime = self._session_metrics()
        now = self._clock.now()
        anchor = (
            runtime.last_processed_at
            if runtime.last_processed_at is not None
            else (self._started_at)
        )
        elapsed = (now - anchor).total_seconds()

        if elapsed <= self._threshold:
            if self._alerted:
                _LOGGER.info(
                    "pipeline resumed",
                    extra={"session_id": self._session_id, "seconds_stalled": elapsed},
                )
            self._alerted = False
            self._previous_feed_reading = self._feed_metrics()
            return None

        diagnosis = self._diagnose(runtime, elapsed)
        if not self._alerted:
            _LOGGER.critical(
                "no closed bar processed within timeframe plus margin",
                extra={
                    "session_id": self._session_id,
                    "stage": diagnosis.stage,
                    "detail": diagnosis.detail,
                    "seconds_since_last_bar": diagnosis.seconds_since_last_bar,
                    "threshold_seconds": self._threshold,
                },
            )
            self._alerted = True
        return diagnosis

    def _diagnose(self, runtime: RuntimeMetrics, elapsed: float) -> StallDiagnosis:
        """Name the stage the pipeline last reached before it stopped moving.

        Evaluated in order of how far upstream the evidence reaches, each check ruling
        out everything before it:
        """
        feed = self._feed_metrics()
        previous_feed = self._previous_feed_reading
        self._previous_feed_reading = feed

        if runtime.bars_received == 0:
            return StallDiagnosis(
                stage="feed_delivery",
                detail=(
                    "no bar has ever reached submit_bar(); the feed has not delivered a "
                    "single closed candle to the session since it started"
                ),
                seconds_since_last_bar=elapsed,
            )
        if feed is not None and previous_feed is not None and feed == previous_feed:
            return StallDiagnosis(
                stage="feed_telemetry_frozen",
                detail=(
                    "the feed's own reported counters have not changed since the "
                    "previous check; either the feed has stopped advancing -- most "
                    "likely blocked inside closed_bars(), receive() or a reconnect that "
                    "never returns -- or the runner's per-bar telemetry refresh has "
                    "itself stopped running, which means the same thing: the pump loop "
                    "is not returning control"
                ),
                seconds_since_last_bar=elapsed,
            )
        if runtime.bars_rejected > 0 and runtime.bars_processed == 0:
            return StallDiagnosis(
                stage="session_rejection",
                detail=(
                    f"{runtime.bars_rejected} bar(s) reached the session and were all "
                    "refused before processing; check clock/grace configuration, or a "
                    "feed replaying bars the session already considers past"
                ),
                seconds_since_last_bar=elapsed,
            )
        return StallDiagnosis(
            stage="unknown_no_progress",
            detail=(
                "bars have been received and processed before, but none in the "
                "configured window, and no single counter isolates why; the feed "
                "telemetry reading changed since the last check, so the feed itself "
                "appears to still be advancing"
            ),
            seconds_since_last_bar=elapsed,
        )

    # --- The background thread ----------------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread. Idempotent: a second call is a no-op.

        The thread is a daemon: it never blocks process shutdown on its own, and
        :meth:`stop` is still the correct way to end it deliberately.
        """
        if self._thread is not None:
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"stall-watchdog-{self._session_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the background thread to finish and wait for it. Safe to call repeatedly."""
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        """The background loop: poll, sleep, repeat, until asked to stop."""
        while not self._stop_requested.is_set():
            try:
                self.check_once()
            except Exception:
                _LOGGER.exception(
                    "stall watchdog check failed", extra={"session_id": self._session_id}
                )
            self._stop_requested.wait(self._poll_interval)

    def __enter__(self) -> StallWatchdog:
        """Start the watchdog for the duration of a ``with`` block."""
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Stop the watchdog when the ``with`` block ends, whatever the reason."""
        self.stop()
