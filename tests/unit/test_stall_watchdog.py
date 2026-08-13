"""The independent observer that notices a pump loop has stopped returning control.

The incident this module answers left a process alive for close to eight hours with a
connected socket, near-zero CPU, no crash and no reconnect — processing nothing. Every
safety net that lived *inside* the pump loop was unable to fire, because the loop itself
had stopped running. These tests exercise :meth:`StallWatchdog.check_once` directly, with
a :class:`~quantplatform.core.clock.SimulatedClock`, so the diagnosis logic is proven
without a single real second of waiting; a small number of thread-level tests at the end
prove :meth:`start`/:meth:`stop` actually drive that same logic on a timer.
"""

from __future__ import annotations

import io
import json
import time
from datetime import timedelta
from typing import Any

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import LogFormat
from quantplatform.core.errors import ConfigurationError
from quantplatform.core.logging_config import configure_logging, get_logger
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.paper.results import RuntimeMetrics
from quantplatform.paper.watchdog import StallDiagnosis, StallWatchdog
from tests.factories import ANCHOR

_THRESHOLD = 3_720.0  # one hour plus a two-minute margin, the platform's own default shape
_SESSION_ID = "watchdog-test"


@pytest.fixture
def stream() -> io.StringIO:
    """Capture what the watchdog logs, via the platform's real logging pipeline."""
    buffer = io.StringIO()
    configure_logging(level="DEBUG", log_format=LogFormat.JSON, stream=buffer)
    get_logger("quantplatform")
    return buffer


def _emitted(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def _runtime(
    *,
    bars_received: int = 0,
    bars_processed: int = 0,
    bars_rejected: int = 0,
    last_processed_at: object = None,
) -> RuntimeMetrics:
    return RuntimeMetrics(
        bars_received=bars_received,
        bars_processed=bars_processed,
        bars_rejected=bars_rejected,
        last_processed_at=last_processed_at,  # type: ignore[arg-type]
    )


def _feed(**overrides: object) -> FeedMetricsSnapshot:
    return FeedMetricsSnapshot(**overrides)  # type: ignore[arg-type]


def _watchdog(
    clock: SimulatedClock,
    *,
    runtime: RuntimeMetrics,
    feed: FeedMetricsSnapshot | None = None,
    threshold_seconds: float = _THRESHOLD,
) -> StallWatchdog:
    return StallWatchdog(
        clock=clock,
        threshold_seconds=threshold_seconds,
        session_metrics=lambda: runtime,
        feed_metrics=lambda: feed,
        session_id=_SESSION_ID,
        started_at=ANCHOR,
    )


# --- Construction ---------------------------------------------------------------------------


def test_a_non_positive_threshold_is_refused() -> None:
    clock = SimulatedClock(ANCHOR)
    with pytest.raises(ConfigurationError, match="strictly positive"):
        StallWatchdog(
            clock=clock,
            threshold_seconds=0,
            session_metrics=_runtime,
            feed_metrics=lambda: None,
            session_id=_SESSION_ID,
        )


def test_a_non_positive_poll_interval_is_refused() -> None:
    clock = SimulatedClock(ANCHOR)
    with pytest.raises(ConfigurationError, match="poll interval"):
        StallWatchdog(
            clock=clock,
            threshold_seconds=60,
            poll_interval_seconds=0,
            session_metrics=_runtime,
            feed_metrics=lambda: None,
            session_id=_SESSION_ID,
        )


# --- Liveness: healthy, boundary, stalled --------------------------------------------------


def test_a_freshly_started_session_with_no_bar_yet_is_not_a_stall() -> None:
    # Warm-up, not a stall: nothing has had the chance to be processed yet.
    clock = SimulatedClock(ANCHOR)
    watchdog = _watchdog(clock, runtime=_runtime())

    assert watchdog.check_once() is None


def test_a_bar_processed_well_within_the_threshold_does_not_alert() -> None:
    clock = SimulatedClock(ANCHOR + timedelta(seconds=60))
    runtime = _runtime(bars_received=1, bars_processed=1, last_processed_at=ANCHOR)
    watchdog = _watchdog(clock, runtime=runtime)

    assert watchdog.check_once() is None


def test_exactly_at_the_threshold_does_not_alert() -> None:
    # The boundary is inclusive: a stall is strictly *past* the threshold, not at it.
    clock = SimulatedClock(ANCHOR + timedelta(seconds=_THRESHOLD))
    runtime = _runtime(bars_received=1, bars_processed=1, last_processed_at=ANCHOR)
    watchdog = _watchdog(clock, runtime=runtime)

    assert watchdog.check_once() is None


def test_one_second_past_the_threshold_alerts() -> None:
    clock = SimulatedClock(ANCHOR + timedelta(seconds=_THRESHOLD + 1))
    runtime = _runtime(bars_received=1, bars_processed=1, last_processed_at=ANCHOR)
    watchdog = _watchdog(clock, runtime=runtime)

    diagnosis = watchdog.check_once()

    assert diagnosis is not None
    assert isinstance(diagnosis, StallDiagnosis)
    assert diagnosis.seconds_since_last_bar == pytest.approx(_THRESHOLD + 1)


def test_no_bar_ever_processed_past_the_threshold_measures_from_session_start() -> None:
    clock = SimulatedClock(ANCHOR + timedelta(seconds=_THRESHOLD + 1))
    watchdog = _watchdog(clock, runtime=_runtime())

    diagnosis = watchdog.check_once()

    assert diagnosis is not None
    assert diagnosis.stage == "feed_delivery"


# --- Diagnosis: naming the stalled stage -----------------------------------------------------


def test_feed_delivery_when_the_session_has_never_received_a_bar() -> None:
    clock = SimulatedClock(ANCHOR + timedelta(seconds=_THRESHOLD + 1))
    watchdog = _watchdog(clock, runtime=_runtime(bars_received=0))

    diagnosis = watchdog.check_once()

    assert diagnosis is not None
    assert diagnosis.stage == "feed_delivery"
    assert "submit_bar" in diagnosis.detail


def test_feed_telemetry_frozen_when_two_readings_are_identical() -> None:
    # The exact shape of the real incident: bars stopped, and the feed's own counters --
    # taken twice, across a full poll interval -- had not moved either.
    clock = SimulatedClock(ANCHOR)
    runtime = _runtime(bars_received=12, bars_processed=12, last_processed_at=ANCHOR)
    frozen_reading = _feed(candles_received=200, candles_accepted=12)
    watchdog = _watchdog(clock, runtime=runtime, feed=frozen_reading)

    clock.advance(timedelta(seconds=_THRESHOLD + 1))
    first = watchdog.check_once()  # establishes the baseline reading
    clock.advance(timedelta(seconds=5))
    second = watchdog.check_once()  # same reading again -- frozen

    assert first is not None
    assert second is not None
    assert second.stage == "feed_telemetry_frozen"
    assert "pump loop" in second.detail


def test_feed_telemetry_still_advancing_is_not_frozen() -> None:
    clock = SimulatedClock(ANCHOR)
    runtime = _runtime(bars_received=12, bars_processed=12, last_processed_at=ANCHOR)
    watchdog = _watchdog(clock, runtime=runtime, feed=_feed(candles_received=200))

    clock.advance(timedelta(seconds=_THRESHOLD + 1))
    watchdog.check_once()
    # A second, genuinely different reading: the feed is still doing something, even
    # though no *closed bar* has reached the session in this window.
    watchdog._feed_metrics = lambda: _feed(candles_received=250)
    clock.advance(timedelta(seconds=5))

    diagnosis = watchdog.check_once()

    assert diagnosis is not None
    assert diagnosis.stage != "feed_telemetry_frozen"


def test_session_rejection_when_bars_arrive_and_are_all_refused() -> None:
    clock = SimulatedClock(ANCHOR + timedelta(seconds=_THRESHOLD + 1))
    runtime = _runtime(bars_received=40, bars_processed=0, bars_rejected=40)
    watchdog = _watchdog(clock, runtime=runtime)

    diagnosis = watchdog.check_once()

    assert diagnosis is not None
    assert diagnosis.stage == "session_rejection"
    assert "40" in diagnosis.detail


def test_unknown_no_progress_when_nothing_isolates_the_cause() -> None:
    # Bars were processed before (there is a real last_processed_at), nothing was ever
    # rejected, and the feed metrics genuinely changed between checks -- none of the
    # specific stages apply, and the diagnosis says so honestly rather than guessing.
    clock = SimulatedClock(ANCHOR)
    runtime = _runtime(bars_received=5, bars_processed=5, last_processed_at=ANCHOR)
    watchdog = _watchdog(clock, runtime=runtime, feed=_feed(candles_received=100))

    clock.advance(timedelta(seconds=_THRESHOLD + 1))
    watchdog.check_once()
    watchdog._feed_metrics = lambda: _feed(candles_received=101)
    clock.advance(timedelta(seconds=5))

    diagnosis = watchdog.check_once()

    assert diagnosis is not None
    assert diagnosis.stage == "unknown_no_progress"


# --- Alerting: once per episode, not once per poll -------------------------------------------


def test_a_critical_alert_is_logged_exactly_once_per_stall_episode(stream: io.StringIO) -> None:
    clock = SimulatedClock(ANCHOR + timedelta(seconds=_THRESHOLD + 1))
    watchdog = _watchdog(clock, runtime=_runtime())

    watchdog.check_once()
    watchdog.check_once()
    watchdog.check_once()

    criticals = [r for r in _emitted(stream) if r["level"] == "CRITICAL"]
    assert len(criticals) == 1
    assert criticals[0]["extra"]["stage"] == "feed_delivery"
    assert criticals[0]["extra"]["session_id"] == _SESSION_ID


def test_recovery_is_logged_once_and_re_arms_the_alert(stream: io.StringIO) -> None:
    clock = SimulatedClock(ANCHOR)
    state: dict[str, RuntimeMetrics] = {"runtime": _runtime()}
    watchdog = StallWatchdog(
        clock=clock,
        threshold_seconds=_THRESHOLD,
        session_metrics=lambda: state["runtime"],
        feed_metrics=lambda: None,
        session_id=_SESSION_ID,
        started_at=ANCHOR,
    )

    clock.advance(timedelta(seconds=_THRESHOLD + 1))
    watchdog.check_once()  # first alert

    state["runtime"] = _runtime(bars_received=1, bars_processed=1, last_processed_at=clock.now())
    watchdog.check_once()  # recovery

    clock.advance(timedelta(seconds=_THRESHOLD + 1))
    watchdog.check_once()  # a second, distinct episode

    records = _emitted(stream)
    assert sum(1 for r in records if r["level"] == "CRITICAL") == 2
    assert sum(1 for r in records if r["message"] == "pipeline resumed") == 1


# --- The real thread ---------------------------------------------------------------------------


def test_start_and_stop_drive_check_once_on_a_real_thread(stream: io.StringIO) -> None:
    clock = SimulatedClock(ANCHOR + timedelta(seconds=_THRESHOLD + 1))
    watchdog = _watchdog(clock, runtime=_runtime(), threshold_seconds=_THRESHOLD)
    watchdog._poll_interval = 0.01

    watchdog.start()
    try:
        for _ in range(200):
            if any(r["level"] == "CRITICAL" for r in _emitted(stream)):
                break
            time.sleep(0.01)
        else:
            pytest.fail("watchdog thread never logged a critical alert")
    finally:
        watchdog.stop()


def test_stop_joins_the_thread_and_is_idempotent() -> None:
    clock = SimulatedClock(ANCHOR)
    watchdog = _watchdog(clock, runtime=_runtime())
    watchdog._poll_interval = 0.01

    watchdog.start()
    watchdog.stop()
    watchdog.stop()  # must not raise

    assert watchdog._thread is None


def test_start_is_idempotent() -> None:
    clock = SimulatedClock(ANCHOR)
    watchdog = _watchdog(clock, runtime=_runtime())
    watchdog._poll_interval = 0.01

    watchdog.start()
    first_thread = watchdog._thread
    watchdog.start()
    second_thread = watchdog._thread
    watchdog.stop()

    assert first_thread is second_thread


def test_context_manager_starts_and_stops() -> None:
    clock = SimulatedClock(ANCHOR)
    watchdog = _watchdog(clock, runtime=_runtime())
    watchdog._poll_interval = 0.01

    with watchdog:
        assert watchdog._thread is not None

    assert watchdog._thread is None


def test_a_check_that_raises_is_contained_and_the_thread_keeps_running() -> None:
    # An observer that could take the process down would be worse than no observer.
    calls = {"count": 0}

    def _flaky_runtime() -> RuntimeMetrics:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient failure reading metrics")
        return _runtime()

    clock = SimulatedClock(ANCHOR)
    watchdog = StallWatchdog(
        clock=clock,
        threshold_seconds=_THRESHOLD,
        session_metrics=_flaky_runtime,
        feed_metrics=lambda: None,
        session_id=_SESSION_ID,
        started_at=ANCHOR,
    )
    watchdog._poll_interval = 0.01

    watchdog.start()
    try:
        for _ in range(200):
            if calls["count"] >= 2:
                break
            time.sleep(0.01)
        else:
            pytest.fail("watchdog thread stopped polling after the first failure")
    finally:
        watchdog.stop()
