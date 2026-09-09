"""Feed health graded on closed candles, not on the venue's streaming cadence.

Binance's kline stream republishes the *forming* candle roughly once a second. Only the
last frame of each hour carries a closed one. Discarding the rest is the feed doing its job,
and for five straight days a live session was graded red for doing it: ``feed_stability``
divided accepted candles by *every* parsed frame and reported 0.06% against a floor of 95%.

Five consecutive red reports for a non-event is worse than no check at all. A red that is
always on is a red nobody reads, and it would have buried a genuine outage on day six.

These tests pin the distinction the metric was missing. A frame the venue was always going
to send again is not a delivery failure; a closed bar that never arrived is. The same
separation applies one layer up: a session that refuses a bar because it is still forming,
not yet final, or one it has already lived through is refusing correctly, and grading that
refusal as a defect is what made a three-bar startup day look like a 25% failure rate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.reporting.config import AlertThresholds
from quantplatform.reporting.daily import evaluate_alerts
from quantplatform.reporting.health import evaluate_health
from quantplatform.reporting.models import (
    AlertCode,
    DailyStatistics,
    HealthCheck,
    HealthCheckName,
    HealthLevel,
)

# One trading day of hourly bars, as the smoke session actually saw them: 24 closed candles
# delivered, and ~42,000 forming updates correctly thrown away on the way.
CLOSED = 24
FORMING = 42_000


def _statistics(**overrides: object) -> DailyStatistics:
    defaults: dict[str, object] = {
        "opening_equity": Decimal(100_000),
        "daily_equity": Decimal(100_000),
        "daily_pnl": Decimal(0),
    }
    return DailyStatistics(**{**defaults, **overrides})  # type: ignore[arg-type]


def _check(statistics: DailyStatistics, name: HealthCheckName) -> HealthCheck:
    health = evaluate_health(statistics=statistics, thresholds=AlertThresholds())
    return next(check for check in health.checks if check.name is name)


def _live_day(**overrides: object) -> DailyStatistics:
    """Return a day shaped like the one the smoke produced, before any degradation."""
    defaults: dict[str, object] = {
        "feed_metrics_available": True,
        "daily_candles_received": FORMING + CLOSED,
        "daily_forming_candles": FORMING,
        "daily_candles_accepted": CLOSED,
        "daily_candles_rejected": FORMING,
        "daily_rejected_frames": FORMING,
        "daily_session_bars_received": CLOSED,
        "daily_session_bars_processed": CLOSED,
        "daily_session_acceptance_rate": Decimal(1),
        "bars_processed": CLOSED,
    }
    return _statistics(**{**defaults, **overrides})


# --- The snapshot separates a forming candle from a failed one -----------------------------------


def test_forming_candles_are_not_counted_as_a_delivery_failure() -> None:
    snapshot = FeedMetricsSnapshot(
        candles_received=FORMING + CLOSED,
        forming_candles=FORMING,
        candles_accepted=CLOSED,
        candles_rejected=FORMING,
        rejected_frames=FORMING,
    )

    assert snapshot.closed_candles_received == CLOSED
    assert snapshot.acceptance_rate == Decimal(1)


def test_a_closed_candle_that_never_became_a_bar_lowers_the_rate() -> None:
    # One closed candle arrived and was refused — a duplicate, say. That is a real loss and
    # the ratio has to move, which is the whole point of keeping the denominator honest.
    snapshot = FeedMetricsSnapshot(
        candles_received=FORMING + CLOSED,
        forming_candles=FORMING,
        candles_accepted=CLOSED - 1,
        candles_rejected=FORMING + 1,
        duplicate_candles=1,
        rejected_frames=FORMING + 1,
    )

    assert snapshot.closed_candles_received == CLOSED
    assert snapshot.acceptance_rate == Decimal(CLOSED - 1) / Decimal(CLOSED)


def test_a_feed_that_delivered_only_forming_candles_has_nothing_to_grade() -> None:
    # Mid-hour startup: updates streaming, no candle closed yet. Undefined, not zero —
    # zero would read as "everything was rejected" and paint the check red on a healthy feed.
    snapshot = FeedMetricsSnapshot(
        candles_received=500, forming_candles=500, candles_rejected=500, rejected_frames=500
    )

    assert snapshot.closed_candles_received == 0
    assert snapshot.acceptance_rate is None


def test_forming_candles_cannot_exceed_the_candles_that_were_refused() -> None:
    with pytest.raises(ValueError, match="forming candles"):
        FeedMetricsSnapshot(
            candles_received=100,
            forming_candles=60,
            candles_accepted=50,
            candles_rejected=50,
            rejected_frames=50,
        )


def test_forming_candles_survive_a_daily_difference() -> None:
    start = FeedMetricsSnapshot(
        candles_received=1_000,
        forming_candles=990,
        candles_accepted=10,
        candles_rejected=990,
        rejected_frames=990,
    )
    end = FeedMetricsSnapshot(
        candles_received=3_000,
        forming_candles=2_970,
        candles_accepted=30,
        candles_rejected=2_970,
        rejected_frames=2_970,
    )

    delta = end.delta_since(start)

    assert delta.forming_candles == 1_980
    assert delta.closed_candles_received == 20
    assert delta.acceptance_rate == Decimal(1)


# --- Partial kline updates do not degrade health --------------------------------------------------


def test_a_day_of_forty_two_thousand_forming_updates_is_green() -> None:
    # The regression this milestone exists for. Every figure below is the live session's.
    check = _check(_live_day(), HealthCheckName.FEED_STABILITY)

    assert check.level is HealthLevel.GREEN
    assert check.observed == Decimal(1)


def test_such_a_day_is_green_overall_and_raises_no_alert() -> None:
    statistics = _live_day()
    health = evaluate_health(statistics=statistics, thresholds=AlertThresholds())
    alerts = evaluate_alerts(statistics=statistics, thresholds=AlertThresholds())

    assert health.level is HealthLevel.GREEN
    assert health.failing == ()
    assert AlertCode.LOW_ACCEPTANCE_RATE not in {alert.code for alert in alerts.alerts}


def test_the_check_names_closed_candles_so_the_reader_knows_the_denominator() -> None:
    check = _check(_live_day(), HealthCheckName.FEED_STABILITY)

    assert "closed" in check.message


# --- A missing closed bar still degrades ----------------------------------------------------------


def test_a_missing_closed_bar_still_turns_the_day_yellow() -> None:
    check = _check(_live_day(missing_bars=1), HealthCheckName.MISSING_BARS)

    assert check.level is not HealthLevel.GREEN


def test_closed_candles_that_never_reached_the_pipeline_degrade_feed_stability() -> None:
    # Three quarters of the day's closed candles refused. Forming updates unchanged, so
    # nothing about the venue's cadence can mask it.
    statistics = _live_day(
        daily_candles_accepted=6,
        daily_candles_rejected=FORMING + 18,
        daily_rejected_frames=FORMING + 18,
    )

    check = _check(statistics, HealthCheckName.FEED_STABILITY)
    assert check.observed == Decimal("0.25")
    assert check.level is HealthLevel.RED


def test_a_gap_is_reported_whatever_the_forming_volume() -> None:
    check = _check(_live_day(daily_gaps=1), HealthCheckName.GAP_COUNT)

    assert check.level is not HealthLevel.GREEN


# --- Reconnect recovery ---------------------------------------------------------------------------


def test_a_reconnect_that_lost_no_closed_bar_stays_healthy() -> None:
    # Binance cycles its socket about once a day with a 1001. Both of the smoke's reconnects
    # were that, and neither cost a bar. A healthy recovery must read as healthy.
    statistics = _live_day(daily_reconnects=1)
    health = evaluate_health(statistics=statistics, thresholds=AlertThresholds())

    assert health.level is HealthLevel.GREEN


def test_a_reconnect_that_did_lose_a_bar_is_not_healthy() -> None:
    statistics = _live_day(daily_reconnects=1, missing_bars=1, daily_gaps=1)
    health = evaluate_health(statistics=statistics, thresholds=AlertThresholds())

    assert health.level is not HealthLevel.GREEN


# --- Duplicates and out-of-order candles ----------------------------------------------------------


def test_a_duplicate_after_a_reconnect_is_visible_in_its_own_right() -> None:
    # Republished on reconnect, refused, and reported as a duplicate rather than folded in
    # with the forming updates it has nothing in common with.
    statistics = _live_day(
        daily_duplicate_candles=1,
        daily_candles_accepted=CLOSED - 1,
        daily_candles_rejected=FORMING + 1,
        daily_rejected_frames=FORMING + 1,
    )

    assert statistics.daily_duplicate_candles == 1
    assert statistics.daily_closed_candles_received == CLOSED
    # It costs a closed candle, so it moves the rate — but one republished candle after a
    # reconnect is the venue behaving correctly, so the day stays green rather than paging.
    assert statistics.observed_acceptance_rate == Decimal(CLOSED - 1) / Decimal(CLOSED)
    assert _check(statistics, HealthCheckName.FEED_STABILITY).level is HealthLevel.GREEN


def test_out_of_order_candles_are_reported() -> None:
    statistics = _live_day(out_of_order_candles=2)

    assert statistics.out_of_order_candles == 2


# --- Session acceptance: a correct refusal is not a defect ----------------------------------------


def test_the_bar_a_session_refuses_at_connect_does_not_count_against_it() -> None:
    # Day one of the smoke: the feed emitted 4 closed candles, the session acted on 3, and
    # the fourth was refused because it was not final — arriving mid-flight on connect. The
    # report called that an *error*. It is the session declining to trade a bar it only
    # partly saw, which is the behaviour we want, not a fault to alert on.
    statistics = _statistics(
        feed_metrics_available=True,
        daily_session_bars_received=4,
        daily_session_bars_superseded=1,
        daily_session_bars_processed=3,
        bars_processed=3,
    )

    check = _check(statistics, HealthCheckName.SESSION_BAR_ACCEPTANCE)
    assert check.level is HealthLevel.GREEN


def test_that_startup_day_raises_no_session_alert() -> None:
    statistics = _statistics(
        feed_metrics_available=True,
        daily_session_bars_received=4,
        daily_session_bars_superseded=1,
        daily_session_bars_processed=3,
        bars_processed=3,
    )

    alerts = evaluate_alerts(statistics=statistics, thresholds=AlertThresholds())

    assert AlertCode.SESSION_REJECTING_BARS not in {alert.code for alert in alerts.alerts}


def test_a_session_dropping_bars_for_no_good_reason_is_still_caught() -> None:
    # The guarantee that the exemption above did not become a blanket amnesty: these
    # refusals are not superseded bars, so every one of them counts.
    statistics = _statistics(
        feed_metrics_available=True,
        daily_session_bars_received=24,
        daily_session_bars_superseded=0,
        daily_session_bars_processed=0,
        bars_processed=0,
    )

    check = _check(statistics, HealthCheckName.SESSION_BAR_ACCEPTANCE)
    assert check.level is HealthLevel.RED


def test_a_session_that_only_ever_saw_superseded_bars_has_nothing_to_grade() -> None:
    statistics = _statistics(
        feed_metrics_available=True,
        daily_session_bars_received=2,
        daily_session_bars_superseded=2,
        daily_session_bars_processed=0,
    )

    check = _check(statistics, HealthCheckName.SESSION_BAR_ACCEPTANCE)
    assert check.skipped is True
    assert check.level is HealthLevel.GREEN


def test_superseded_bars_leave_the_rate_at_one_rather_than_above_it() -> None:
    # Twenty processed out of twenty-four delivered, four of which were never actionable.
    # The corrected denominator is twenty, so the day is perfect — and exactly perfect, not
    # 120%, which is the arithmetic a naive exemption would have produced.
    statistics = _statistics(
        feed_metrics_available=True,
        daily_session_bars_received=24,
        daily_session_bars_superseded=4,
        daily_session_bars_processed=20,
    )

    assert statistics.actionable_session_bars == 20
    assert statistics.observed_session_acceptance_rate == Decimal(1)


# --- The economic figures are untouched -----------------------------------------------------------


def test_grading_health_differently_moves_no_economic_figure() -> None:
    statistics = _live_day(
        daily_pnl=Decimal("-49.26"), commission_paid=Decimal("13.29"), trade_count=2
    )

    assert statistics.daily_pnl == Decimal("-49.26")
    assert statistics.commission_paid == Decimal("13.29")
    assert statistics.trade_count == 2
