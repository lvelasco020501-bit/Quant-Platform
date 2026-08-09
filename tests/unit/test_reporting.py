"""Phase 7B unit tests: configuration, round trips, statistics, health, alerts and rendering.

Every test here builds its inputs directly rather than running a session, so a day with a
gap, a day that lost money and a day where every order was refused are all one line of
setup. The session-driven path is exercised in the integration suite.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.enums import AlertSeverity, OrderSide
from quantplatform.core.errors import FeedTelemetryRegressionError
from quantplatform.core.models.telemetry import ZERO_FEED_METRICS, FeedMetricsSnapshot
from quantplatform.reporting.config import (
    AlertThresholds,
    ReportFormat,
    ReportingConfiguration,
)
from quantplatform.reporting.daily import evaluate_alerts, reconstruct_round_trips
from quantplatform.reporting.health import evaluate_health
from quantplatform.reporting.models import (
    AlertCode,
    DailyAlerts,
    DailyHealth,
    DailyReport,
    DailySeries,
    DailyStatistics,
    FeedDiagnostics,
    HealthCheckName,
    HealthLevel,
    RoundTrip,
)
from quantplatform.reporting.summary import build_summary, render_csv, render_markdown
from tests.factories import ANCHOR, SYMBOL, make_fill

_DAY = date(2026, 1, 1)


def _statistics(**overrides: object) -> DailyStatistics:
    defaults: dict[str, object] = {
        "opening_equity": Decimal(100_000),
        "daily_equity": Decimal(100_000),
        "daily_pnl": Decimal(0),
    }
    return DailyStatistics(**{**defaults, **overrides})  # type: ignore[arg-type]


def _report(**overrides: object) -> DailyReport:
    statistics = overrides.pop("statistics", _statistics())
    assert isinstance(statistics, DailyStatistics)
    health = evaluate_health(statistics=statistics, thresholds=AlertThresholds())
    alerts = evaluate_alerts(statistics=statistics, thresholds=AlertThresholds())
    defaults: dict[str, object] = {
        "session_id": "session-1",
        "strategy_id": "buy_then_sell",
        "day": _DAY,
        "generated_at": ANCHOR,
        "quote_asset": "USDT",
        "timezone": "UTC",
        "statistics": statistics,
        "health": health,
        "alerts": alerts,
        "summary": build_summary(day=_DAY, statistics=statistics, health=health, alerts=alerts),
    }
    return DailyReport(**{**defaults, **overrides})  # type: ignore[arg-type]


# --- Configuration ----------------------------------------------------------------------------


def test_the_default_configuration_writes_every_format_to_a_dated_tree() -> None:
    config = ReportingConfiguration()

    assert set(config.formats) == {ReportFormat.JSON, ReportFormat.CSV, ReportFormat.MARKDOWN}
    assert config.directory_for(_DAY) == Path("reports/2026/01/01")
    assert config.writes(ReportFormat.JSON) is True


def test_a_reporting_day_is_labelled_in_the_configured_zone() -> None:
    # 23:30 UTC is already the next day in Tokyo. Timestamps stay UTC; only the label moves.
    moment = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)

    assert ReportingConfiguration().day_of(moment) == date(2026, 1, 1)
    assert ReportingConfiguration(timezone="Asia/Tokyo").day_of(moment) == date(2026, 1, 2)


def test_retention_is_off_by_default() -> None:
    # Deleting an audit trail is not something a default should do.
    assert ReportingConfiguration().retention_days is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"formats": ()}, "at least one report format"),
        ({"formats": (ReportFormat.JSON, ReportFormat.JSON)}, "must not repeat"),
        ({"timezone": "Mars/Olympus"}, "unknown time zone"),
        ({"output_directory": Path()}, "must not be empty"),
        ({"chart_dpi": 5}, "greater than or equal"),
    ],
)
def test_an_unusable_configuration_is_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ReportingConfiguration(**overrides)  # type: ignore[arg-type]


def test_configuration_is_frozen_and_rejects_unknown_fields() -> None:
    config = ReportingConfiguration()

    with pytest.raises(ValueError, match="frozen"):
        config.timezone = "UTC"  # type: ignore[misc]
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ReportingConfiguration(api_key="secret")  # type: ignore[call-arg]


def test_a_signed_loss_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        AlertThresholds(max_daily_loss=Decimal(-100))


# --- Round trips ------------------------------------------------------------------------------


def _fill(
    *, side: OrderSide, price: Decimal, quantity: Decimal, hours: int, fee: Decimal = Decimal(0)
) -> object:
    return make_fill(
        side=side,
        price=price,
        quantity=quantity,
        fee=fee,
        executed_at=ANCHOR + timedelta(hours=hours),
    )


def test_a_buy_followed_by_a_sell_becomes_one_round_trip() -> None:
    fills = [
        _fill(side=OrderSide.BUY, price=Decimal(50_000), quantity=Decimal(1), hours=1),
        _fill(
            side=OrderSide.SELL, price=Decimal(52_000), quantity=Decimal(1), hours=5, fee=Decimal(3)
        ),
    ]

    trips = reconstruct_round_trips(fills)  # type: ignore[arg-type]

    assert len(trips) == 1
    trip = trips[0]
    assert trip.entry_price == Decimal(50_000)
    assert trip.exit_price == Decimal(52_000)
    assert trip.gross_pnl == Decimal(2_000)
    assert trip.net_pnl == Decimal(1_997)
    assert trip.holding_seconds == 4 * 3_600
    assert trip.is_win is True


def test_an_open_position_produces_no_trade() -> None:
    # An entry still open has no outcome to be right or wrong about yet.
    fills = [_fill(side=OrderSide.BUY, price=Decimal(50_000), quantity=Decimal(1), hours=1)]

    assert reconstruct_round_trips(fills) == ()  # type: ignore[arg-type]


def test_scaling_into_a_position_uses_average_cost() -> None:
    # Matches the portfolio engine's own basis; a different one here would make the report
    # disagree with the ledger it describes.
    fills = [
        _fill(side=OrderSide.BUY, price=Decimal(50_000), quantity=Decimal(1), hours=1),
        _fill(side=OrderSide.BUY, price=Decimal(60_000), quantity=Decimal(1), hours=2),
        _fill(side=OrderSide.SELL, price=Decimal(56_000), quantity=Decimal(2), hours=6),
    ]

    trip = reconstruct_round_trips(fills)[0]  # type: ignore[arg-type]

    assert trip.entry_price == Decimal(55_000)
    assert trip.gross_pnl == Decimal(2_000)
    assert trip.opened_at == ANCHOR + timedelta(hours=1)


def test_a_partial_exit_leaves_the_trade_open_until_flat() -> None:
    fills = [
        _fill(side=OrderSide.BUY, price=Decimal(50_000), quantity=Decimal(2), hours=1),
        _fill(side=OrderSide.SELL, price=Decimal(51_000), quantity=Decimal(1), hours=2),
    ]
    assert reconstruct_round_trips(fills) == ()  # type: ignore[arg-type]

    fills.append(_fill(side=OrderSide.SELL, price=Decimal(53_000), quantity=Decimal(1), hours=3))
    trip = reconstruct_round_trips(fills)[0]  # type: ignore[arg-type]

    assert trip.exit_price == Decimal(52_000)
    assert trip.gross_pnl == Decimal(4_000)


def test_two_cycles_on_the_same_symbol_are_two_trades() -> None:
    fills = [
        _fill(side=OrderSide.BUY, price=Decimal(50_000), quantity=Decimal(1), hours=1),
        _fill(side=OrderSide.SELL, price=Decimal(51_000), quantity=Decimal(1), hours=2),
        _fill(side=OrderSide.BUY, price=Decimal(49_000), quantity=Decimal(1), hours=3),
        _fill(side=OrderSide.SELL, price=Decimal(48_000), quantity=Decimal(1), hours=4),
    ]

    trips = reconstruct_round_trips(fills)  # type: ignore[arg-type]

    assert [trip.gross_pnl for trip in trips] == [Decimal(1_000), Decimal(-1_000)]
    assert [trip.is_win for trip in trips] == [True, False]


def test_a_closing_fill_with_no_recorded_entry_is_ignored() -> None:
    # No cost basis exists to measure a result against, and inventing one would fabricate PnL.
    fills = [_fill(side=OrderSide.SELL, price=Decimal(50_000), quantity=Decimal(1), hours=1)]

    assert reconstruct_round_trips(fills) == ()  # type: ignore[arg-type]


def test_fills_are_ordered_before_they_are_matched() -> None:
    ordered = [
        _fill(side=OrderSide.BUY, price=Decimal(50_000), quantity=Decimal(1), hours=1),
        _fill(side=OrderSide.SELL, price=Decimal(52_000), quantity=Decimal(1), hours=5),
    ]

    assert reconstruct_round_trips(ordered) == reconstruct_round_trips(  # type: ignore[arg-type]
        list(reversed(ordered))  # type: ignore[arg-type]
    )


def test_a_round_trip_cannot_close_before_it_opens() -> None:
    with pytest.raises(ValueError, match="cannot close before it opens"):
        RoundTrip(
            symbol=SYMBOL,
            side=OrderSide.BUY,
            opened_at=ANCHOR + timedelta(hours=2),
            closed_at=ANCHOR,
            quantity=Decimal(1),
            entry_price=Decimal(1),
            exit_price=Decimal(1),
            gross_pnl=Decimal(0),
            fees=Decimal(0),
            net_pnl=Decimal(0),
        )


# --- Statistics -------------------------------------------------------------------------------


def test_a_rejection_rate_over_no_decisions_is_undefined_not_zero() -> None:
    assert _statistics().order_rejection_ratio is None


def test_the_rejection_rate_counts_both_stages() -> None:
    statistics = _statistics(approved_orders=6, risk_rejections=3, broker_rejections=1)

    assert statistics.order_rejection_ratio == Decimal(4) / Decimal(10)
    assert statistics.total_rejections == 4


def test_statistics_are_frozen() -> None:
    with pytest.raises(ValueError, match="frozen"):
        _statistics().daily_pnl = Decimal(1)  # type: ignore[misc]


# --- Health -----------------------------------------------------------------------------------


def _health(**overrides: object) -> DailyHealth:
    return evaluate_health(statistics=_statistics(**overrides), thresholds=AlertThresholds())


def test_a_clean_day_is_green_across_every_check() -> None:
    health = _health(acceptance_rate=Decimal(1), approved_orders=5)

    assert health.level is HealthLevel.GREEN
    assert health.failing == ()
    assert {check.name for check in health.checks} == set(HealthCheckName)


def test_a_single_gap_turns_the_day_yellow() -> None:
    health = _health(daily_gaps=1)

    assert health.level is HealthLevel.YELLOW
    assert health.failing[0].name is HealthCheckName.GAP_COUNT


def test_enough_gaps_turn_the_day_red() -> None:
    # A counter whose limit is zero cannot be exceeded by a factor, so it escalates by count.
    health = _health(daily_gaps=3)

    assert health.level is HealthLevel.RED


def test_a_profitable_day_can_still_be_red() -> None:
    # Health and performance never blend: profit earned on a history the strategy never
    # fully saw is not evidence the strategy works.
    health = _health(daily_pnl=Decimal(5_000), daily_gaps=9, acceptance_rate=Decimal("0.30"))

    assert health.level is HealthLevel.RED


def test_a_feed_delivering_less_than_the_floor_is_flagged() -> None:
    assert _health(acceptance_rate=Decimal("0.90")).level is HealthLevel.YELLOW
    assert _health(acceptance_rate=Decimal("0.40")).level is HealthLevel.RED


def test_checks_with_nothing_to_measure_report_themselves_skipped() -> None:
    # Claiming green for a measurement nobody took would be asserting something unchecked.
    health = _health()
    skipped = {check.name for check in health.skipped}

    assert HealthCheckName.CLOCK_DRIFT in skipped
    assert HealthCheckName.FEED_STABILITY in skipped
    assert HealthCheckName.ORDER_REJECTION_RATIO in skipped
    assert health.level is HealthLevel.GREEN


def test_measured_clock_drift_is_graded_on_its_magnitude() -> None:
    assert _health(clock_drift_seconds=Decimal(1)).level is HealthLevel.GREEN
    assert _health(clock_drift_seconds=Decimal(-3)).level is HealthLevel.YELLOW
    assert _health(clock_drift_seconds=Decimal(9)).level is HealthLevel.RED


def test_a_day_refusing_most_of_its_orders_is_flagged() -> None:
    health = _health(approved_orders=1, risk_rejections=9)

    assert health.level is HealthLevel.RED
    check = next(c for c in health.checks if c.name is HealthCheckName.ORDER_REJECTION_RATIO)
    assert check.observed == Decimal("0.9")


def test_the_worst_check_decides_the_day() -> None:
    assert HealthLevel.worst((HealthLevel.GREEN, HealthLevel.RED, HealthLevel.YELLOW)) is (
        HealthLevel.RED
    )
    assert HealthLevel.worst(()) is HealthLevel.GREEN


# --- Alerts -----------------------------------------------------------------------------------


def _alerts(**overrides: object) -> DailyAlerts:
    return evaluate_alerts(statistics=_statistics(**overrides), thresholds=AlertThresholds())


def test_a_quiet_day_raises_nothing() -> None:
    assert _alerts(acceptance_rate=Decimal(1)).count == 0


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"max_drawdown": Decimal("0.08")}, AlertCode.DRAWDOWN_EXCEEDED),
        ({"daily_pnl": Decimal(-900)}, AlertCode.LARGE_LOSS),
        ({"daily_gaps": 2}, AlertCode.GAP_DETECTED),
        ({"daily_reconnects": 9}, AlertCode.MULTIPLE_RECONNECTS),
        ({"missing_bars": 4}, AlertCode.MISSING_DATA),
        ({"runtime_exceptions": 1}, AlertCode.RUNTIME_EXCEPTION),
        ({"approved_orders": 1, "risk_rejections": 5}, AlertCode.RISK_REJECTION_SPIKE),
        ({"approved_orders": 10, "broker_rejections": 5}, AlertCode.BROKER_REJECTION_SPIKE),
        ({"acceptance_rate": Decimal("0.5")}, AlertCode.LOW_ACCEPTANCE_RATE),
        (
            {"traded_notional": Decimal(10_000), "slippage_paid": Decimal(500)},
            AlertCode.ABNORMAL_SLIPPAGE,
        ),
        (
            {"traded_notional": Decimal(10_000), "commission_paid": Decimal(500)},
            AlertCode.ABNORMAL_COMMISSION,
        ),
    ],
)
def test_each_condition_raises_its_own_alert(overrides: dict[str, object], code: AlertCode) -> None:
    alerts = _alerts(**overrides)

    assert alerts.by_code(code) is not None, [alert.code for alert in alerts.alerts]


def test_a_severe_drawdown_escalates_to_critical() -> None:
    alerts = _alerts(max_drawdown=Decimal("0.30"))

    assert alerts.by_code(AlertCode.DRAWDOWN_EXCEEDED) is not None
    assert alerts.critical != ()
    assert alerts.critical[0].severity is AlertSeverity.CRITICAL


def test_cost_ratios_are_not_evaluated_on_a_day_that_traded_nothing() -> None:
    # Dividing by a zero notional would either explode or invent a ratio.
    alerts = _alerts(commission_paid=Decimal(50), slippage_paid=Decimal(50))

    assert alerts.by_code(AlertCode.ABNORMAL_COMMISSION) is None
    assert alerts.by_code(AlertCode.ABNORMAL_SLIPPAGE) is None


def test_slippage_is_judged_on_magnitude_not_direction() -> None:
    # Executing far away from the reference is worth knowing about even when it helped.
    alerts = _alerts(traded_notional=Decimal(10_000), slippage_paid=Decimal(-500))

    assert alerts.by_code(AlertCode.ABNORMAL_SLIPPAGE) is not None


# --- Summary and rendering ----------------------------------------------------------------------

_ADVICE_FORBIDDEN = (
    r"\bbuy\b",
    r"\bsell\b",
    r"\bhold\b",
    r"\binvest\b",
    r"\binvestment\b",
    r"\ballocate\b",
    r"\bposition size\b",
    r"\bincrease exposure\b",
    r"\breduce exposure\b",
    r"\bshould trade\b",
    r"\bprofitable strategy\b",
)
"""Advice-shaped phrasing the summary must never produce.

Matched on word boundaries on purpose: "investigate the feed" is an operational instruction
and must stay allowed, while "invest" must not.
"""


def test_the_summary_never_gives_investment_advice() -> None:
    # The reporting layer can see what happened to a process, not what should happen to a
    # portfolio. This keeps that boundary from eroding one helpful sentence at a time.
    statistics = _statistics(
        daily_pnl=Decimal(-4_000),
        max_drawdown=Decimal("0.4"),
        daily_gaps=6,
        acceptance_rate=Decimal("0.2"),
        approved_orders=1,
        risk_rejections=9,
        runtime_exceptions=2,
    )
    report = _report(statistics=statistics)

    prose = " ".join(
        (*report.summary.recommendations, *report.summary.anomalies, report.summary.headline)
    ).lower()

    for phrase in _ADVICE_FORBIDDEN:
        assert re.search(phrase, prose) is None, f"summary offered advice: {phrase!r}"


def test_the_summary_states_the_day_in_plain_terms() -> None:
    summary = _report(
        statistics=_statistics(
            daily_pnl=Decimal(250),
            daily_equity=Decimal(100_250),
            bars_processed=24,
            trade_count=2,
        )
    ).summary

    assert "2 trade(s) closed" in summary.headline
    assert "gained 250" in summary.profit_line


def test_a_day_with_no_bars_says_so() -> None:
    assert "no bars were processed" in _report().summary.headline


def test_a_quiet_day_is_called_out_as_an_anomaly() -> None:
    report = _report(statistics=_statistics(bars_processed=24, acceptance_rate=Decimal(1)))

    assert any("closed no positions" in note for note in report.summary.anomalies)


def test_markdown_carries_every_section() -> None:
    text = render_markdown(_report(statistics=_statistics(daily_gaps=1)))

    for heading in (
        "# Daily report",
        "## Summary",
        "## Account",
        "## Trades",
        "## Costs and order flow",
        "## Process",
        "## Health checks",
        "## Alerts",
        "## Compared with the previous day",
        "## Operational notes",
    ):
        assert heading in text, heading


def test_markdown_says_not_computable_rather_than_zero() -> None:
    # A Sharpe of 0.00 on a day with two bars looks like an answer.
    text = render_markdown(_report())

    assert "not computable" in text


def test_csv_is_one_header_and_one_row() -> None:
    text = render_csv(_report(statistics=_statistics(daily_pnl=Decimal("12.5"))))
    lines = text.strip().split("\n")

    assert len(lines) == 2
    header, row = lines[0].split(","), lines[1].split(",")
    assert len(header) == len(row)
    assert "daily_pnl" in header
    assert row[header.index("daily_pnl")] == "12.5"


def test_csv_escapes_a_value_carrying_a_comma() -> None:
    text = render_csv(_report(session_id="alpha, beta"))

    assert '"alpha, beta"' in text


def test_a_report_round_trips_through_json() -> None:
    original = _report(
        statistics=_statistics(daily_pnl=Decimal("-12.345"), trade_count=1),
        series=DailySeries(trade_pnl=(Decimal("-12.345"),)),
    )

    restored = DailyReport.model_validate_json(original.model_dump_json())

    assert restored == original
    assert restored.statistics.daily_pnl == Decimal("-12.345")


def test_a_report_is_frozen() -> None:
    with pytest.raises(ValueError, match="frozen"):
        _report().day = date(2026, 2, 2)  # type: ignore[misc]


# --- Feed telemetry ----------------------------------------------------------------------------


def _snapshot(**overrides: object) -> FeedMetricsSnapshot:
    defaults: dict[str, object] = {
        "candles_received": 100,
        "candles_accepted": 100,
    }
    return FeedMetricsSnapshot(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_diagnostics_take_every_field_the_feed_can_measure() -> None:
    # The defect this phase closes: before, these all defaulted to zero and a day with
    # eleven reconnects reported a clean stream.
    snapshot = FeedMetricsSnapshot(
        reconnect_count=4,
        heartbeat_timeouts=2,
        detected_gaps=3,
        rejected_frames=30,
        malformed_frames=5,
        candles_received=100,
        candles_accepted=75,
        candles_rejected=25,
        duplicate_candles=10,
    )

    diagnostics = FeedDiagnostics.from_feed_metrics(snapshot)

    assert diagnostics.reconnects == 4
    assert diagnostics.heartbeat_failures == 2
    assert diagnostics.gaps_detected == 3
    assert diagnostics.duplicate_candles == 10
    assert diagnostics.rejected_frames == 30
    assert diagnostics.malformed_frames == 5
    assert diagnostics.candles_received == 100
    assert diagnostics.candles_accepted == 75
    assert diagnostics.candles_rejected == 25
    assert diagnostics.feed_acceptance_rate == Decimal("0.75")


def test_fields_the_feed_cannot_measure_stay_at_their_defaults() -> None:
    # A feed cannot count a broker rejection or a clock drift. Leaving them defaulted is
    # honest; filling them from the snapshot would be inventing numbers.
    diagnostics = FeedDiagnostics.from_feed_metrics(_snapshot())

    assert diagnostics.out_of_order_candles == 0
    assert diagnostics.broker_rejections == 0
    assert diagnostics.clock_drift_seconds is None


def test_observations_the_feed_cannot_make_are_carried_alongside_it() -> None:
    diagnostics = FeedDiagnostics.from_feed_metrics(
        _snapshot(), broker_rejections=3, clock_drift_seconds=Decimal(7)
    )

    assert diagnostics.broker_rejections == 3
    assert diagnostics.clock_drift_seconds == Decimal(7)
    assert diagnostics.candles_received == 100


def test_an_acceptance_rate_over_no_candles_is_undefined() -> None:
    assert FeedDiagnostics().feed_acceptance_rate is None


def test_the_feed_status_ignores_checks_that_are_not_about_the_stream() -> None:
    # An operator asking "can I trust today's data" does not care that the session
    # restarted; that is a different question with a different answer.
    statistics = _statistics(
        acceptance_rate=Decimal(1), session_interruptions=9, clock_drift_seconds=Decimal(99)
    )
    health = evaluate_health(statistics=statistics, thresholds=AlertThresholds())

    assert health.level is HealthLevel.RED
    assert health.feed_level is HealthLevel.GREEN


def test_the_feed_status_turns_red_on_a_stream_fault() -> None:
    statistics = _statistics(acceptance_rate=Decimal(1), daily_gaps=9)
    health = evaluate_health(statistics=statistics, thresholds=AlertThresholds())

    assert health.feed_level is HealthLevel.RED


def test_markdown_carries_a_feed_health_section() -> None:
    text = render_markdown(
        _report(
            statistics=_statistics(
                acceptance_rate=Decimal("0.75"),
                daily_reconnects=4,
                daily_heartbeat_failures=2,
                daily_gaps=3,
                daily_rejected_frames=30,
                daily_malformed_frames=5,
                daily_candles_received=100,
                daily_candles_accepted=75,
                daily_candles_rejected=25,
                feed_metrics_available=True,
            )
        )
    )

    assert "## Feed health" in text
    for row in (
        "| Reconnects | 4 |",
        "| Heartbeat failures | 2 |",
        "| Detected gaps | 3 |",
        "| Rejected frames | 30 |",
        "| Malformed frames | 5 |",
        "| Candles received | 100 |",
        "| Candles accepted | 75 |",
        "| Candles rejected | 25 |",
    ):
        assert row in text, row
    assert "| Overall feed status | red |" in text


def test_a_day_without_feed_metrics_says_unmeasured_rather_than_showing_zeros() -> None:
    # Zeros beside no measurement read as a clean stream. That was the whole defect.
    text = render_markdown(_report(statistics=_statistics(acceptance_rate=Decimal(1))))

    assert "| Overall feed status | unmeasured |" in text
    assert "they are unmeasured" in text


def test_a_measured_clean_day_reads_green_not_unmeasured() -> None:
    text = render_markdown(
        _report(
            statistics=_statistics(
                acceptance_rate=Decimal(1),
                daily_candles_received=50,
                daily_candles_accepted=50,
                feed_metrics_available=True,
            )
        )
    )

    assert "| Overall feed status | green |" in text
    assert "they are unmeasured" not in text


def test_csv_carries_the_feed_columns_and_status() -> None:
    text = render_csv(
        _report(
            statistics=_statistics(
                daily_reconnects=4,
                daily_malformed_frames=5,
                daily_candles_received=100,
                daily_candles_accepted=75,
                daily_candles_rejected=25,
                daily_rejected_frames=30,
                feed_metrics_available=True,
                acceptance_rate=Decimal("0.75"),
            )
        )
    )
    header, row = (line.split(",") for line in text.strip().split("\n"))
    values = dict(zip(header, row, strict=True))

    assert values["feed_status"] == "yellow"
    assert values["daily_reconnects"] == "4"
    assert values["daily_malformed_frames"] == "5"
    assert values["daily_candles_received"] == "100"
    assert values["daily_candles_accepted"] == "75"
    assert values["daily_candles_rejected"] == "25"
    assert values["daily_rejected_frames"] == "30"
    assert values["feed_metrics_available"] == "True"


def test_the_feed_fields_survive_a_json_round_trip() -> None:
    original = _report(
        statistics=_statistics(
            daily_reconnects=4,
            daily_malformed_frames=5,
            daily_candles_received=100,
            daily_candles_accepted=75,
            daily_candles_rejected=25,
            daily_rejected_frames=30,
            feed_metrics_available=True,
        )
    )

    restored = DailyReport.model_validate_json(original.model_dump_json())

    assert restored == original
    assert restored.statistics.daily_malformed_frames == 5
    assert restored.statistics.feed_metrics_available is True


# --- Daily deltas ------------------------------------------------------------------------------


def test_a_day_delta_subtracts_every_additive_counter() -> None:
    opening = FeedMetricsSnapshot(
        reconnect_count=3,
        heartbeat_timeouts=2,
        detected_gaps=1,
        rejected_frames=10,
        malformed_frames=4,
        candles_received=100,
        candles_accepted=80,
        candles_rejected=6,
        duplicate_candles=2,
    )
    closing = FeedMetricsSnapshot(
        reconnect_count=5,
        heartbeat_timeouts=2,
        detected_gaps=4,
        rejected_frames=25,
        malformed_frames=9,
        candles_received=250,
        candles_accepted=200,
        candles_rejected=16,
        duplicate_candles=7,
    )

    daily = closing.delta_since(opening)

    assert daily.reconnect_count == 2
    assert daily.heartbeat_timeouts == 0
    assert daily.detected_gaps == 3
    assert daily.rejected_frames == 15
    assert daily.malformed_frames == 5
    assert daily.candles_received == 150
    assert daily.candles_accepted == 120
    assert daily.candles_rejected == 10
    assert daily.duplicate_candles == 5


def test_the_first_day_measures_from_zero() -> None:
    # No previous baseline means the first report covers everything since the session began,
    # not nothing at all.
    closing = FeedMetricsSnapshot(reconnect_count=4, candles_received=9, candles_accepted=9)

    assert closing.delta_since(ZERO_FEED_METRICS) == closing


def test_a_daily_acceptance_rate_is_recomputed_not_differenced() -> None:
    # The change in a ratio is not the ratio of the change. Yesterday 100% and today 50%
    # does not make today's rate -50%.
    opening = FeedMetricsSnapshot(candles_received=100, candles_accepted=100)
    closing = FeedMetricsSnapshot(candles_received=200, candles_accepted=150)

    daily = closing.delta_since(opening)

    assert opening.acceptance_rate == Decimal(1)
    assert daily.candles_received == 100
    assert daily.acceptance_rate == Decimal("0.5")


def test_a_quiet_day_has_an_undefined_acceptance_rate() -> None:
    steady = FeedMetricsSnapshot(candles_received=50, candles_accepted=50)

    assert steady.delta_since(steady).acceptance_rate is None


def test_a_counter_that_went_backwards_is_refused() -> None:
    # Counters only climb. A smaller reading means the two do not describe one continuous
    # run, and subtracting anyway would put a negative count in a report.
    opening = FeedMetricsSnapshot(reconnect_count=9, candles_received=10, candles_accepted=10)
    closing = FeedMetricsSnapshot(reconnect_count=2, candles_received=20, candles_accepted=20)

    with pytest.raises(FeedTelemetryRegressionError, match="moved backwards") as caught:
        closing.delta_since(opening)
    assert caught.value.details["counters"] == ["reconnect_count"]


def test_the_zero_baseline_is_a_clean_snapshot() -> None:
    assert ZERO_FEED_METRICS.is_clean is True
    assert ZERO_FEED_METRICS.acceptance_rate is None


def test_daily_statistics_name_their_window() -> None:
    # The rename is the fix: nothing on DailyStatistics carries a cumulative feed counter
    # under a name that reads like a daily one.
    fields = set(DailyStatistics.model_fields)

    assert {"daily_reconnects", "daily_gaps", "daily_candles_received"} <= fields
    assert "reconnect_count" not in fields
    assert "gap_count" not in fields
    assert "candles_received" not in fields


def test_feed_stability_prefers_the_measured_daily_rate() -> None:
    measured = _statistics(
        acceptance_rate=Decimal(1),
        daily_feed_acceptance_rate=Decimal("0.20"),
        feed_metrics_available=True,
    )
    unmeasured = _statistics(acceptance_rate=Decimal(1), daily_feed_acceptance_rate=Decimal("0.20"))

    assert measured.observed_acceptance_rate == Decimal("0.20")
    assert unmeasured.observed_acceptance_rate == Decimal(1)


def test_markdown_names_the_feed_section_daily() -> None:
    text = render_markdown(
        _report(statistics=_statistics(daily_reconnects=2, feed_metrics_available=True))
    )

    assert "## Feed health — daily" in text
    assert "| Reconnects | 2 |" in text
