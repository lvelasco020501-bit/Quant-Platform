"""Phase 7B: reports produced by a real session, and written to a real directory.

The session below is the production one, driving the production strategy, risk engine,
simulated broker and portfolio. The reporting layer is bolted on as an observer and the
tests check two things at once: that the numbers it reports match what the engine itself
computed, and that observing changed nothing about what the session did.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.errors import (
    DataProviderError,
    FeedTelemetryRegressionError,
    TelemetryNotConfiguredError,
)
from quantplatform.core.interfaces import FeedMetricsReader, StreamingMarketDataProvider
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.telemetry import (
    ADDITIVE_FEED_COUNTERS,
    ZERO_FEED_METRICS,
    FeedMetricsSnapshot,
    SymbolRulesTelemetry,
)
from quantplatform.marketdata.feed import BinanceSpotMarketDataFeed
from quantplatform.paper import (
    InMemoryPaperStateRepository,
    PaperTradingRunner,
    PaperTradingSession,
)
from quantplatform.paper.results import SessionResult
from quantplatform.reporting import (
    CHART_FILENAMES,
    AlertCode,
    DailyReport,
    DailyReportBuilder,
    DailyReportRecorder,
    DailyReportWriter,
    FeedDiagnostics,
    HealthLevel,
    ReportFormat,
    ReportingConfiguration,
    reconstruct_round_trips,
)
from quantplatform.reporting.summary import render_markdown
from tests.factories import ANCHOR, make_backtest, make_bars
from tests.integration.test_backtest_engine import (
    BuyOnce,
    BuyThenSell,
    Silent,
    _flat_bars,
    _Params,
)
from tests.integration.test_marketdata_feed import (
    _bar_steps,
    _fail,
    _feed,
    _frame,
    _ScriptedTransport,
    _Step,
)

_PNG_MAGIC = b"\x89PNG"


def _config(directory: Path, **overrides: object) -> ReportingConfiguration:
    defaults: dict[str, object] = {
        "output_directory": directory,
        "chart_dpi": 60,
        "chart_width_inches": 4.0,
        "chart_height_inches": 3.0,
    }
    return ReportingConfiguration(**{**defaults, **overrides})  # type: ignore[arg-type]


def _session(
    *,
    clock: SimulatedClock,
    strategy: object | None = None,
    observer: DailyReportRecorder | None = None,
) -> tuple[PaperTradingSession, object]:
    engine, broker, portfolio = make_backtest(
        strategy=strategy if strategy is not None else BuyThenSell(_Params()),  # type: ignore[arg-type]
    )
    session = PaperTradingSession(
        session_id="reported-session",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        day_rollover_observer=observer,
    )
    return session, portfolio


def _run(session: PaperTradingSession, clock: SimulatedClock, bars: tuple[MarketBar, ...]) -> None:
    session.start()
    for bar in bars:
        clock.set_time(bar.close_time)
        session.submit_bar(bar)
    session.stop()


def _two_day_bars() -> tuple[MarketBar, ...]:
    """Thirty hourly bars spanning two UTC days, with a profitable round trip on day one."""
    closes = [Decimal(50_000)] * 3 + [Decimal(50_000), Decimal(53_000)] + [Decimal(53_000)] * 25
    return make_bars(closes)


def _three_day_bars() -> tuple[MarketBar, ...]:
    """Sixty hourly bars, so two UTC day boundaries are crossed and two reports are emitted.

    Close times run 01:00 on the 1st to 12:00 on the 3rd, putting 23 bars in the first day,
    24 in the second and the remainder in the third. The profitable round trip closes on
    day one, so day two can be compared against a day that actually traded.
    """
    closes = [Decimal(50_000)] * 3 + [Decimal(50_000), Decimal(53_000)] + [Decimal(53_000)] * 55
    return make_bars(closes)


def _recorder(
    config: ReportingConfiguration,
    clock: SimulatedClock,
    *,
    writer: DailyReportWriter | None = None,
    diagnostics: FeedDiagnostics | None = None,
) -> DailyReportRecorder:
    return DailyReportRecorder(
        builder=DailyReportBuilder(config=config, clock=clock),
        sink=writer.write if writer is not None else None,
        diagnostics=(lambda: diagnostics) if diagnostics is not None else None,
    )


# --- Session integration --------------------------------------------------------------------


def test_a_session_emits_one_report_when_the_day_rolls_over(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _two_day_bars())

    assert len(recorder.reports) == 1
    assert recorder.reports[0].day == date(2026, 1, 1)
    assert recorder.failures == 0


def test_a_session_that_never_crosses_a_day_boundary_emits_nothing(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _flat_bars(5))

    assert recorder.reports == ()


def test_the_report_describes_the_day_that_ended_not_the_one_starting(tmp_path: Path) -> None:
    # The observer fires before the first bar of the new day is processed, so the closing
    # figures belong wholly to the day being reported.
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _two_day_bars())

    statistics = recorder.reports[0].statistics
    assert statistics.bars_processed == 23  # 01:00 .. 23:00 close times
    assert statistics.trade_count == 1


def test_observing_a_session_changes_nothing_about_what_it_trades(tmp_path: Path) -> None:
    # The load-bearing claim of the whole phase. Same bars, same pipeline, one run watched
    # and one not: the accounts must be identical.
    bars = _two_day_bars()

    watched_clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), watched_clock)
    watched, watched_portfolio = _session(clock=watched_clock, observer=recorder)
    _run(watched, watched_clock, bars)

    quiet_clock = SimulatedClock(ANCHOR)
    quiet, quiet_portfolio = _session(clock=quiet_clock)
    _run(quiet, quiet_clock, bars)

    assert watched.result().snapshot.equity == quiet.result().snapshot.equity
    assert watched.result().fills == quiet.result().fills
    assert watched_portfolio.positions() == quiet_portfolio.positions()  # type: ignore[attr-defined]
    assert watched.runtime_metrics().bars_processed == quiet.runtime_metrics().bars_processed


def test_a_broken_reporter_cannot_stop_a_session(tmp_path: Path) -> None:
    # A week-long session must not die because a disk filled at midnight.
    class _Exploding:
        def day_of(self, moment: object) -> date:
            return ReportingConfiguration(output_directory=tmp_path).day_of(moment)  # type: ignore[arg-type]

        def on_day_rollover(
            self,
            *,
            completed_day: date,
            result: SessionResult,
            feed_metrics: FeedMetricsSnapshot | None = None,
        ) -> None:
            _ = (completed_day, result, feed_metrics)
            msg = "the reporting disk is full"
            raise OSError(msg)

    clock = SimulatedClock(ANCHOR)
    session, _ = _session(clock=clock, observer=_Exploding())  # type: ignore[arg-type]

    _run(session, clock, _two_day_bars())

    assert session.runtime_metrics().bars_processed == 30
    assert session.runtime_metrics().report_failures == 1


def test_a_failing_sink_is_counted_rather_than_hidden(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)

    def _refuse(report: DailyReport) -> None:
        _ = report
        msg = "sink unavailable"
        raise RuntimeError(msg)

    recorder = DailyReportRecorder(
        builder=DailyReportBuilder(config=_config(tmp_path), clock=clock), sink=_refuse
    )
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _two_day_bars())

    assert recorder.failures == 1
    assert recorder.reports == ()


def test_the_reporting_zone_decides_where_the_day_breaks(tmp_path: Path) -> None:
    # Identical bars, two zones. Tokyo is nine hours ahead, so its day ends at 15:00 UTC and
    # the reported day holds nine fewer hourly bars than the same day measured in UTC.
    bars = _two_day_bars()

    utc_clock = SimulatedClock(ANCHOR)
    utc_recorder = _recorder(_config(tmp_path / "utc"), utc_clock)
    utc_session, _ = _session(clock=utc_clock, observer=utc_recorder)
    _run(utc_session, utc_clock, bars)

    tokyo_clock = SimulatedClock(ANCHOR)
    tokyo_recorder = _recorder(_config(tmp_path / "tokyo", timezone="Asia/Tokyo"), tokyo_clock)
    tokyo_session, _ = _session(clock=tokyo_clock, observer=tokyo_recorder)
    _run(tokyo_session, tokyo_clock, bars)

    assert utc_recorder.reports[0].day == tokyo_recorder.reports[0].day == date(2026, 1, 1)
    assert utc_recorder.reports[0].statistics.bars_processed == 23
    assert tokyo_recorder.reports[0].statistics.bars_processed == 14


# --- Figures agree with the engine ------------------------------------------------------------


def test_reported_trades_match_the_engine_realised_pnl() -> None:
    # The strongest available check on the round-trip reconstruction: it is a second reading
    # of the same fills, and it must agree with the ledger that produced them.
    clock = SimulatedClock(ANCHOR)
    session, _ = _session(clock=clock)
    _run(session, clock, _two_day_bars())
    result = session.result()

    trips = reconstruct_round_trips(result.fills)

    assert len(trips) == 1
    assert result.performance is not None
    assert sum(trip.gross_pnl for trip in trips) == result.performance.realized_pnl
    assert trips[0].gross_pnl > Decimal(0)


def test_reported_equity_matches_the_session_snapshot(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    _run(session, clock, _two_day_bars())

    report = recorder.reports[0]
    detail = session.result().detail
    assert detail is not None
    day_one = [point for point in detail.equity_curve if point.at.date() == date(2026, 1, 1)]
    assert report.statistics.daily_equity == day_one[-1].equity
    assert report.statistics.opening_equity == detail.config.initial_capital
    assert report.statistics.daily_pnl == day_one[-1].equity - detail.config.initial_capital


def test_intraday_drawdown_is_measured_against_the_day_own_peak(tmp_path: Path) -> None:
    # Inheriting the run's high-water mark would show a deep drawdown on a day that never
    # fell, purely because a better day happened earlier.
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    _run(session, clock, _two_day_bars())

    assert recorder.reports[0].statistics.max_drawdown >= Decimal(0)
    assert recorder.reports[0].series.drawdown != ()


def test_a_day_with_no_trades_reports_undefined_rather_than_zero(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, strategy=Silent(_Params()), observer=recorder)

    _run(session, clock, make_bars([Decimal(50_000)] * 30))

    statistics = recorder.reports[0].statistics
    assert statistics.trade_count == 0
    assert statistics.win_rate is None
    assert statistics.profit_factor is None
    assert statistics.expectancy is None
    assert statistics.average_position_size is None
    assert recorder.reports[0].is_quiet is True


def test_an_open_position_at_the_rollover_is_not_counted_as_a_trade(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, strategy=BuyOnce(_Params()), observer=recorder)

    _run(session, clock, make_bars([Decimal(50_000)] * 30))

    statistics = recorder.reports[0].statistics
    assert statistics.trade_count == 0
    assert statistics.exposure_utilization is not None
    assert statistics.exposure_utilization > Decimal(0)


def test_feed_observations_reach_the_report_and_its_health(tmp_path: Path) -> None:
    # The session cannot see its own data source, so these come from outside it.
    clock = SimulatedClock(ANCHOR)
    diagnostics = FeedDiagnostics(
        reconnects=5,
        gaps_detected=2,
        heartbeat_failures=1,
        duplicate_candles=7,
        out_of_order_candles=1,
        unknown_symbols=1,
        missing_bars=3,
        runtime_exceptions=1,
        clock_drift_seconds=Decimal(9),
    )
    recorder = _recorder(_config(tmp_path), clock, diagnostics=diagnostics)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _two_day_bars())

    statistics = recorder.reports[0].statistics
    assert statistics.daily_reconnects == 5
    assert statistics.daily_gaps == 2
    assert statistics.daily_duplicate_candles == 7
    assert statistics.out_of_order_candles == 1
    assert statistics.unknown_symbols == 1
    assert recorder.reports[0].health.level is HealthLevel.RED
    assert recorder.reports[0].alerts.count > 0


# --- Persistence ------------------------------------------------------------------------------


def test_a_written_day_lands_in_a_dated_directory_with_every_artefact(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    writer = DailyReportWriter(config=_config(tmp_path))
    recorder = _recorder(_config(tmp_path), clock, writer=writer)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _two_day_bars())

    written = writer.written[0]
    assert written.directory == tmp_path / "2026" / "01" / "01"
    assert sorted(path.name for path in written.paths) == sorted(
        ["daily.json", "daily.csv", "daily.md", *CHART_FILENAMES]
    )
    for path in written.paths:
        assert path.is_file()
        assert path.stat().st_size > 0


def test_every_chart_is_a_real_png(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    writer = DailyReportWriter(config=_config(tmp_path))
    recorder = _recorder(_config(tmp_path), clock, writer=writer)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _two_day_bars())

    for path in writer.written[0].chart_paths:
        assert path.read_bytes()[:4] == _PNG_MAGIC


def test_charts_are_still_drawn_for_a_day_with_nothing_on_it(tmp_path: Path) -> None:
    # A missing file reads as a broken pipeline; an empty chart reads as a quiet day.
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path)
    writer = DailyReportWriter(config=config)
    session, _ = _session(clock=clock)
    session.start()
    report = DailyReportBuilder(config=config, clock=clock).build(
        day=date(2026, 1, 1), result=session.result()
    )

    written = writer.write(report)

    assert len(written.chart_paths) == len(CHART_FILENAMES)
    for path in written.chart_paths:
        assert path.read_bytes()[:4] == _PNG_MAGIC


def test_a_written_report_reads_back_identically(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    writer = DailyReportWriter(config=_config(tmp_path))
    recorder = _recorder(_config(tmp_path), clock, writer=writer)
    session, _ = _session(clock=clock, observer=recorder)
    _run(session, clock, _two_day_bars())

    restored = writer.read(date(2026, 1, 1))

    assert restored == recorder.reports[0]


def test_reading_a_day_that_was_never_written_returns_nothing(tmp_path: Path) -> None:
    writer = DailyReportWriter(config=_config(tmp_path))

    assert writer.read(date(2026, 1, 1)) is None


def test_only_the_configured_formats_are_written(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path, formats=(ReportFormat.JSON,), render_charts=False)
    writer = DailyReportWriter(config=config)
    session, _ = _session(clock=clock)
    session.start()
    report = DailyReportBuilder(config=config, clock=clock).build(
        day=date(2026, 1, 1), result=session.result()
    )

    written = writer.write(report)

    assert [path.name for path in written.paths] == ["daily.json"]
    assert written.csv_path is None
    assert written.markdown_path is None


def test_a_day_written_without_json_cannot_be_read_back(tmp_path: Path) -> None:
    # JSON is the only round-trippable format; CSV flattens and Markdown prose-ifies.
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path, formats=(ReportFormat.CSV,), render_charts=False)
    writer = DailyReportWriter(config=config)
    session, _ = _session(clock=clock)
    session.start()
    writer.write(
        DailyReportBuilder(config=config, clock=clock).build(
            day=date(2026, 1, 1), result=session.result()
        )
    )

    assert writer.read(date(2026, 1, 1)) is None


def test_the_markdown_page_names_the_day_and_its_health(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    writer = DailyReportWriter(config=_config(tmp_path))
    recorder = _recorder(_config(tmp_path), clock, writer=writer)
    session, _ = _session(clock=clock, observer=recorder)
    _run(session, clock, _two_day_bars())

    markdown_path = writer.written[0].markdown_path
    assert markdown_path is not None
    text = markdown_path.read_text(encoding="utf-8")

    assert "# Daily report — 2026-01-01" in text
    assert "**Health**: **GREEN**" in text
    assert "## Health checks" in text


# --- Comparison -------------------------------------------------------------------------------


def test_the_second_day_is_compared_with_the_first(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _three_day_bars())

    first, second = recorder.reports
    assert first.comparison is None
    assert second.comparison is not None
    assert second.comparison.previous_day == first.day
    assert second.comparison.trade_count_delta == (
        second.statistics.trade_count - first.statistics.trade_count
    )


def test_deterioration_is_named_rather_than_left_to_be_noticed(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _three_day_bars())

    second = recorder.reports[1]
    assert second.comparison is not None
    # Day two closes no trades after day one closed a profitable one, so PnL fell.
    assert second.comparison.pnl_delta < Decimal(0)
    assert any("PnL fell" in note for note in second.comparison.deteriorations)
    assert any("PnL fell" in note for note in second.summary.anomalies)


def test_the_previous_day_can_be_recovered_from_disk_across_a_restart(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path)
    writer = DailyReportWriter(config=config)
    first_recorder = _recorder(config, clock, writer=writer)
    session, _ = _session(clock=clock, observer=first_recorder)
    _run(session, clock, _two_day_bars())

    recovered = writer.read_previous(date(2026, 1, 2))

    assert recovered is not None
    assert recovered.day == date(2026, 1, 1)


def test_looking_back_skips_days_the_session_was_down(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path)
    writer = DailyReportWriter(config=config)
    session, _ = _session(clock=clock)
    session.start()
    writer.write(
        DailyReportBuilder(config=config, clock=clock).build(
            day=date(2026, 1, 1), result=session.result()
        )
    )

    assert writer.read_previous(date(2026, 1, 5)) is not None
    assert writer.read_previous(date(2026, 1, 5), lookback_days=2) is None


# --- Retention --------------------------------------------------------------------------------


def _write_day(writer: DailyReportWriter, config: ReportingConfiguration, day: date) -> None:
    clock = SimulatedClock(ANCHOR)
    session, _ = _session(clock=clock)
    session.start()
    writer.write(
        DailyReportBuilder(config=config, clock=clock).build(day=day, result=session.result())
    )


def test_nothing_is_pruned_unless_retention_was_configured(tmp_path: Path) -> None:
    # Deleting an audit trail is never a side effect of writing one.
    config = _config(tmp_path, render_charts=False)
    writer = DailyReportWriter(config=config)
    _write_day(writer, config, date(2020, 1, 1))

    assert writer.prune(today=date(2026, 6, 1)) == ()
    assert (tmp_path / "2020" / "01" / "01").is_dir()


def test_retention_removes_only_days_past_the_window(tmp_path: Path) -> None:
    config = _config(tmp_path, render_charts=False, retention_days=30)
    writer = DailyReportWriter(config=config)
    _write_day(writer, config, date(2026, 1, 1))
    _write_day(writer, config, date(2026, 3, 1))

    removed = writer.prune(today=date(2026, 3, 10))

    assert removed == (tmp_path / "2026" / "01" / "01",)
    assert not (tmp_path / "2026" / "01" / "01").exists()
    assert (tmp_path / "2026" / "03" / "01").is_dir()


def test_retention_leaves_anything_that_is_not_a_day_directory_alone(tmp_path: Path) -> None:
    config = _config(tmp_path, render_charts=False, retention_days=1)
    writer = DailyReportWriter(config=config)
    _write_day(writer, config, date(2020, 1, 1))
    stray = tmp_path / "notes" / "2020" / "backup"
    stray.mkdir(parents=True)
    (stray / "keep.txt").write_text("keep me", encoding="utf-8")

    writer.prune(today=date(2026, 6, 1))

    assert (stray / "keep.txt").is_file()


def test_pruning_an_empty_tree_is_harmless(tmp_path: Path) -> None:
    config = _config(tmp_path / "absent", retention_days=1)

    assert DailyReportWriter(config=config).prune(today=date(2026, 1, 1)) == ()


# --- Days that produced nothing -----------------------------------------------------------------


def test_a_day_with_no_bars_at_all_still_produces_a_report(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path)
    session, _ = _session(clock=clock)
    session.start()

    report = DailyReportBuilder(config=config, clock=clock).build(
        day=date(2026, 1, 1), result=session.result()
    )

    assert report.statistics.bars_processed == 0
    assert report.statistics.daily_pnl == Decimal(0)
    assert report.statistics.sharpe_ratio is None
    assert report.series.is_empty is True
    assert "no bars were processed" in report.summary.headline
    assert report.health.level is HealthLevel.GREEN


def test_a_day_of_pure_rejection_reports_the_rejections(tmp_path: Path) -> None:
    # Sizing everything to zero makes risk refuse every intent, so the day trades nothing
    # and every refusal is visible.
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path)
    engine, broker, portfolio = make_backtest(
        strategy=BuyThenSell(_Params()), max_order_notional=Decimal(1)
    )
    session = PaperTradingSession(
        session_id="refused",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
    )
    _run(session, clock, _two_day_bars())

    report = DailyReportBuilder(config=config, clock=clock).build(
        day=date(2026, 1, 1), result=session.result()
    )

    assert report.statistics.risk_rejections > 0
    assert report.statistics.approved_orders == 0
    assert report.statistics.trade_count == 0
    assert any("refused by risk" in note for note in report.summary.anomalies), (
        report.summary.anomalies
    )


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Tokyo", "America/New_York"])
def test_a_report_is_produced_in_any_supported_zone(tmp_path: Path, timezone: str) -> None:
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path, timezone=timezone)
    recorder = _recorder(config, clock)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, _two_day_bars())

    assert recorder.reports != ()
    assert all(report.timezone == timezone for report in recorder.reports)


def test_report_days_never_overlap(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path, timezone="Asia/Tokyo"), clock)
    session, _ = _session(clock=clock, observer=recorder)

    _run(session, clock, make_bars([Decimal(50_000)] * 60))

    days = [report.day for report in recorder.reports]
    assert days == sorted(set(days))
    assert sum(report.statistics.bars_processed for report in recorder.reports) <= 60


def test_a_restarted_session_carries_its_previous_day_forward(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path)
    writer = DailyReportWriter(config=config)
    session, _ = _session(clock=clock)
    session.start()
    earlier = DailyReportBuilder(config=config, clock=clock).build(
        day=date(2025, 12, 31), result=session.result()
    )
    writer.write(earlier)

    later_clock = SimulatedClock(ANCHOR)
    recorder = DailyReportRecorder(
        builder=DailyReportBuilder(config=config, clock=later_clock),
        sink=writer.write,
        previous=earlier,
    )
    restarted, _ = _session(clock=later_clock, observer=recorder)
    _run(restarted, later_clock, _two_day_bars())

    assert recorder.reports[0].comparison is not None
    assert recorder.reports[0].comparison.previous_day == date(2025, 12, 31)


def test_the_recorder_tracks_the_day_boundary_it_was_configured_with(tmp_path: Path) -> None:
    recorder = _recorder(_config(tmp_path, timezone="Asia/Tokyo"), SimulatedClock(ANCHOR))

    assert recorder.day_of(ANCHOR) == date(2026, 1, 1)
    assert recorder.day_of(ANCHOR + timedelta(hours=15)) == date(2026, 1, 2)


# --- Feed telemetry reaching the report ---------------------------------------------------------


def _live_bars() -> tuple[MarketBar, ...]:
    """Thirty hourly bars, crossing one UTC midnight."""
    return make_bars([Decimal(50_000)] * 30)


def _feed_over(
    steps: list[_Step], clock: SimulatedClock
) -> tuple[BinanceSpotMarketDataFeed, _ScriptedTransport, list[float]]:
    return _feed(steps, clock=clock)


def test_feed_metrics_travel_from_the_socket_to_the_report(tmp_path: Path) -> None:
    # The whole point of the phase, end to end: a real feed's counters reach a report
    # without paper or reporting ever importing the market-data package.
    bars = _live_bars()
    clock = SimulatedClock(ANCHOR)
    steps = [
        *_bar_steps(bars[:4]),
        _fail(),  # one reconnect
        *_bar_steps(bars[4:]),
    ]
    feed, _, _ = _feed_over(steps, clock)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)

    PaperTradingRunner(
        session=session,
        feed=feed,
        max_bars=len(bars),
        feed_metrics=feed,
    ).run()

    assert recorder.reports != ()
    statistics = recorder.reports[0].statistics
    assert statistics.feed_metrics_available is True
    assert statistics.daily_reconnects == 1
    assert statistics.daily_candles_received > 0
    assert statistics.daily_candles_accepted > 0


def test_a_reconnect_beyond_the_threshold_changes_the_health_score(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    builder = DailyReportBuilder(config=_config(tmp_path), clock=clock)
    session, _ = _session(clock=clock)
    _run(session, clock, _live_bars())
    result = session.result()

    tolerated = builder.build(
        day=date(2026, 1, 1),
        result=result,
        feed_metrics=FeedMetricsSnapshot(
            reconnect_count=1, candles_received=23, candles_accepted=23
        ),
    )
    excessive = builder.build(
        day=date(2026, 1, 1),
        result=result,
        feed_metrics=FeedMetricsSnapshot(
            reconnect_count=9, candles_received=23, candles_accepted=23
        ),
    )

    assert tolerated.health.level is HealthLevel.GREEN
    assert excessive.health.level is HealthLevel.RED
    assert excessive.alerts.by_code(AlertCode.MULTIPLE_RECONNECTS) is not None


def test_a_detected_gap_reaches_the_report(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    builder = DailyReportBuilder(config=_config(tmp_path), clock=clock)
    session, _ = _session(clock=clock)
    _run(session, clock, _live_bars())

    report = builder.build(
        day=date(2026, 1, 1),
        result=session.result(),
        feed_metrics=FeedMetricsSnapshot(detected_gaps=2, candles_received=23, candles_accepted=23),
    )

    assert report.statistics.daily_gaps == 2
    assert report.health.feed_level is not HealthLevel.GREEN
    assert report.alerts.by_code(AlertCode.GAP_DETECTED) is not None


def test_heartbeat_failures_reach_the_report(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    builder = DailyReportBuilder(config=_config(tmp_path), clock=clock)
    session, _ = _session(clock=clock)
    _run(session, clock, _live_bars())

    report = builder.build(
        day=date(2026, 1, 1),
        result=session.result(),
        feed_metrics=FeedMetricsSnapshot(
            heartbeat_timeouts=6, candles_received=23, candles_accepted=23
        ),
    )

    assert report.statistics.daily_heartbeat_failures == 6
    assert report.health.feed_level is HealthLevel.RED


def test_a_measured_but_spotless_feed_still_reports_green(tmp_path: Path) -> None:
    # Measuring must not make a clean day look worse than not measuring it.
    clock = SimulatedClock(ANCHOR)
    builder = DailyReportBuilder(config=_config(tmp_path), clock=clock)
    session, _ = _session(clock=clock)
    _run(session, clock, _live_bars())

    report = builder.build(
        day=date(2026, 1, 1),
        result=session.result(),
        feed_metrics=FeedMetricsSnapshot(candles_received=23, candles_accepted=23),
    )

    assert report.health.level is HealthLevel.GREEN
    assert report.health.feed_level is HealthLevel.GREEN
    assert report.alerts.count == 0
    assert report.statistics.feed_metrics_available is True


def test_a_feed_dropping_candles_is_caught_where_it_used_to_pass(tmp_path: Path) -> None:
    # Before this phase the report read the session's own acceptance rate, which counts
    # only bars the session refused. A feed that never delivered them was invisible.
    clock = SimulatedClock(ANCHOR)
    builder = DailyReportBuilder(config=_config(tmp_path), clock=clock)
    session, _ = _session(clock=clock)
    _run(session, clock, _live_bars())
    result = session.result()

    unmeasured = builder.build(day=date(2026, 1, 1), result=result)
    measured = builder.build(
        day=date(2026, 1, 1),
        result=result,
        feed_metrics=FeedMetricsSnapshot(
            candles_received=100, candles_accepted=40, candles_rejected=60, rejected_frames=60
        ),
    )

    assert unmeasured.health.level is HealthLevel.GREEN
    assert measured.health.level is HealthLevel.RED
    assert measured.alerts.by_code(AlertCode.LOW_ACCEPTANCE_RATE) is not None


def test_the_session_carries_the_snapshot_without_reading_it(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    snapshot = FeedMetricsSnapshot(reconnect_count=7, candles_received=5, candles_accepted=5)

    session.record_feed_metrics(snapshot)

    assert session.feed_metrics is snapshot
    _run(session, clock, _live_bars())
    assert recorder.reports[0].statistics.daily_reconnects == 7


def test_recording_a_snapshot_never_changes_what_the_session_trades() -> None:
    bars = _live_bars()

    watched_clock = SimulatedClock(ANCHOR)
    watched, watched_portfolio = _session(clock=watched_clock)
    watched.start()
    for bar in bars:
        watched_clock.set_time(bar.close_time)
        watched.record_feed_metrics(
            FeedMetricsSnapshot(reconnect_count=99, detected_gaps=99, candles_received=1)
        )
        watched.submit_bar(bar)
    watched.stop()

    quiet_clock = SimulatedClock(ANCHOR)
    quiet, quiet_portfolio = _session(clock=quiet_clock)
    _run(quiet, quiet_clock, bars)

    assert watched.result().fills == quiet.result().fills
    assert watched.result().snapshot.equity == quiet.result().snapshot.equity
    assert watched_portfolio.positions() == quiet_portfolio.positions()  # type: ignore[attr-defined]


def test_a_malformed_frame_is_counted_before_it_stops_the_feed() -> None:
    # Counting is not handling. The frame still ends the run; the report can now say so.
    clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed_over([_frame("this is not json")], clock)

    with pytest.raises(DataProviderError):
        list(itertools.islice(feed.closed_bars(), 1))

    assert feed.metrics.malformed_frames == 1
    assert feed.metrics.health_snapshot().malformed_frames == 1
    assert feed.metrics.health_snapshot().is_clean is False


def test_the_written_report_carries_feed_health_in_every_format(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    config = _config(tmp_path, render_charts=False)
    writer = DailyReportWriter(config=config)
    session, _ = _session(clock=clock)
    _run(session, clock, _live_bars())
    report = DailyReportBuilder(config=config, clock=clock).build(
        day=date(2026, 1, 1),
        result=session.result(),
        feed_metrics=FeedMetricsSnapshot(
            reconnect_count=2,
            detected_gaps=1,
            heartbeat_timeouts=1,
            malformed_frames=1,
            rejected_frames=4,
            candles_received=30,
            candles_accepted=23,
            candles_rejected=3,
        ),
    )

    written = writer.write(report)

    assert written.markdown_path is not None
    markdown = written.markdown_path.read_text(encoding="utf-8")
    assert "## Feed health" in markdown
    assert "| Detected gaps | 1 |" in markdown
    assert written.csv_path is not None
    assert "feed_status" in written.csv_path.read_text(encoding="utf-8")
    restored = writer.read(date(2026, 1, 1))
    assert restored is not None
    assert restored.statistics.daily_reconnects == 2
    assert restored.statistics.daily_malformed_frames == 1


# --- Daily telemetry across days ---------------------------------------------------------------


class _Cumulative:
    """A feed metrics reader whose counters climb, as a real feed's do."""

    def __init__(self) -> None:
        self.snapshot = ZERO_FEED_METRICS

    def read_feed_metrics(self) -> FeedMetricsSnapshot:
        return self.snapshot

    def advance(self, **deltas: int) -> None:
        values = {
            name: getattr(self.snapshot, name) + deltas.get(name, 0)
            for name in ADDITIVE_FEED_COUNTERS
        }
        self.snapshot = FeedMetricsSnapshot(**values)


def _daily_bars(days: int) -> tuple[MarketBar, ...]:
    """Enough hourly bars to cross ``days`` UTC midnights, plus one bar into the last day."""
    return make_bars([Decimal(50_000)] * (24 * days + 2))


def test_day_one_reconnects_do_not_appear_on_day_two(tmp_path: Path) -> None:
    # The defect this phase closes. Cumulative counters made Monday's reconnects reappear
    # every day for the rest of the week.
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    reader = _Cumulative()

    session.start()
    for bar in _daily_bars(2):
        clock.set_time(bar.close_time)
        if bar.close_time.hour == 12 and bar.close_time.day == 1:
            reader.advance(reconnect_count=3, detected_gaps=2, heartbeat_timeouts=1)
        reader.advance(candles_received=1, candles_accepted=1)
        session.record_feed_metrics(reader.read_feed_metrics())
        session.submit_bar(bar)
    session.stop()

    first, second = recorder.reports[0], recorder.reports[1]
    assert first.statistics.daily_reconnects == 3
    assert first.statistics.daily_gaps == 2
    assert first.statistics.daily_heartbeat_failures == 1
    assert second.statistics.daily_reconnects == 0
    assert second.statistics.daily_gaps == 0
    assert second.statistics.daily_heartbeat_failures == 0


def test_health_recovers_the_day_after_a_bad_one(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    reader = _Cumulative()

    session.start()
    for bar in _daily_bars(2):
        clock.set_time(bar.close_time)
        if bar.close_time.hour == 12 and bar.close_time.day == 1:
            reader.advance(detected_gaps=9)
        reader.advance(candles_received=1, candles_accepted=1)
        session.record_feed_metrics(reader.read_feed_metrics())
        session.submit_bar(bar)
    session.stop()

    assert recorder.reports[0].health.level is HealthLevel.RED
    assert recorder.reports[1].health.level is HealthLevel.GREEN
    assert recorder.reports[1].health.feed_level is HealthLevel.GREEN


def test_three_cumulative_days_produce_three_correct_daily_reports(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    reader = _Cumulative()
    per_day = {1: 1, 2: 4, 3: 2}

    session.start()
    for bar in _daily_bars(3):
        clock.set_time(bar.close_time)
        if bar.close_time.hour == 12 and bar.close_time.day in per_day:
            reader.advance(reconnect_count=per_day[bar.close_time.day])
        reader.advance(candles_received=1, candles_accepted=1)
        session.record_feed_metrics(reader.read_feed_metrics())
        session.submit_bar(bar)
    session.stop()

    assert [report.day.day for report in recorder.reports] == [1, 2, 3]
    assert [report.statistics.daily_reconnects for report in recorder.reports] == [1, 4, 2]
    # Cumulative would have read 1, 5, 7 — each day inheriting every earlier fault.
    assert reader.snapshot.reconnect_count == 7


def test_the_first_report_covers_everything_since_the_session_began(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)

    assert session.feed_baseline == ZERO_FEED_METRICS
    session.record_feed_metrics(
        FeedMetricsSnapshot(reconnect_count=2, candles_received=30, candles_accepted=30)
    )
    _run(session, clock, _two_day_bars())

    assert recorder.reports[0].statistics.daily_reconnects == 2
    assert recorder.reports[0].statistics.daily_candles_received == 30


def test_a_produced_report_advances_the_baseline(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    closing = FeedMetricsSnapshot(reconnect_count=2, candles_received=30, candles_accepted=30)

    session.record_feed_metrics(closing)
    _run(session, clock, _two_day_bars())

    assert recorder.reports != ()
    assert session.feed_baseline == closing


def test_a_failed_report_leaves_the_baseline_where_it_was(tmp_path: Path) -> None:
    # Advancing past a day nobody reported would delete that day's feed history rather than
    # defer it. Holding the baseline folds the lost window into the next report instead.
    clock = SimulatedClock(ANCHOR)

    def _refuse(report: DailyReport) -> None:
        _ = report
        msg = "sink unavailable"
        raise RuntimeError(msg)

    recorder = DailyReportRecorder(
        builder=DailyReportBuilder(config=_config(tmp_path), clock=clock), sink=_refuse
    )
    session, _ = _session(clock=clock, observer=recorder)
    session.record_feed_metrics(
        FeedMetricsSnapshot(reconnect_count=2, candles_received=30, candles_accepted=30)
    )

    _run(session, clock, _two_day_bars())

    assert recorder.failures == 1
    assert session.feed_baseline == ZERO_FEED_METRICS
    assert session.runtime_metrics().report_failures == 1


def test_a_lost_day_folds_into_the_next_report_rather_than_vanishing(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    accepted: list[DailyReport] = []
    failures = {"remaining": 1}

    def _flaky(report: DailyReport) -> None:
        if failures["remaining"]:
            failures["remaining"] -= 1
            msg = "disk full"
            raise RuntimeError(msg)
        accepted.append(report)

    recorder = DailyReportRecorder(
        builder=DailyReportBuilder(config=_config(tmp_path), clock=clock), sink=_flaky
    )
    session, _ = _session(clock=clock, observer=recorder)
    reader = _Cumulative()

    session.start()
    for bar in _daily_bars(2):
        clock.set_time(bar.close_time)
        if bar.close_time.hour == 12:
            reader.advance(reconnect_count=1)
        reader.advance(candles_received=1, candles_accepted=1)
        session.record_feed_metrics(reader.read_feed_metrics())
        session.submit_bar(bar)
    session.stop()

    # Day one's report was lost, so day two carries both days' reconnects — visibly larger,
    # which is how a lost report should show up rather than as silence.
    assert len(accepted) == 1
    assert accepted[0].statistics.daily_reconnects == 2


def test_the_baseline_survives_a_stop_and_resume(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    repository = InMemoryPaperStateRepository()
    recorder = _recorder(_config(tmp_path), clock)
    engine, broker, portfolio = make_backtest(strategy=Silent(_Params()))
    session = PaperTradingSession(
        session_id="restarted",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=repository,
        day_rollover_observer=recorder,
    )
    closing = FeedMetricsSnapshot(reconnect_count=3, candles_received=30, candles_accepted=30)
    session.record_feed_metrics(closing)
    _run(session, clock, _two_day_bars())
    assert session.feed_baseline == closing

    stored = repository.load("restarted")
    assert stored is not None
    assert stored.feed_baseline == closing

    revived = PaperTradingSession(
        session_id="restarted",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=repository,
    )
    revived.resume()

    assert revived.feed_baseline == closing


def test_a_session_that_never_reported_resumes_from_zero(tmp_path: Path) -> None:
    _ = tmp_path
    clock = SimulatedClock(ANCHOR)
    repository = InMemoryPaperStateRepository()
    engine, broker, portfolio = make_backtest(strategy=Silent(_Params()))
    session = PaperTradingSession(
        session_id="fresh",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=repository,
    )
    _run(session, clock, _flat_bars(4))

    revived = PaperTradingSession(
        session_id="fresh",
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=repository,
    )
    revived.resume()

    assert revived.feed_baseline == ZERO_FEED_METRICS


def test_a_counter_regression_is_refused_rather_than_reported(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    builder = DailyReportBuilder(config=_config(tmp_path), clock=clock)
    session, _ = _session(clock=clock)
    _run(session, clock, _live_bars())

    with pytest.raises(FeedTelemetryRegressionError, match="moved backwards"):
        builder.build(
            day=date(2026, 1, 1),
            result=session.result(),
            feed_metrics=FeedMetricsSnapshot(reconnect_count=1),
            previous_feed_metrics=FeedMetricsSnapshot(reconnect_count=8),
        )


# --- Real-feed wiring ---------------------------------------------------------------------------


def test_a_live_feed_without_telemetry_is_refused_at_wiring_time(tmp_path: Path) -> None:
    # A run that cannot see its own data quality produces reports that look clean for the
    # wrong reason. Wiring time is the last cheap place to catch that.
    clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed_over([], clock)
    session, _ = _session(clock=clock, observer=_recorder(_config(tmp_path), clock))

    with pytest.raises(TelemetryNotConfiguredError, match="must be wired with a feed_metrics"):
        PaperTradingRunner(session=session, feed=feed)


def test_a_live_feed_is_its_own_telemetry_reader(tmp_path: Path) -> None:
    _ = tmp_path
    clock = SimulatedClock(ANCHOR)
    feed, _, _ = _feed_over([], clock)

    assert isinstance(feed, FeedMetricsReader)
    assert feed.read_feed_metrics() == ZERO_FEED_METRICS


def test_a_replay_feed_still_runs_without_telemetry(tmp_path: Path) -> None:
    # A deterministic replay has no health to report; forcing it to invent counters would
    # put fiction where a report expects measurement.
    _ = tmp_path
    clock = SimulatedClock(ANCHOR)
    bars = _flat_bars(4)

    class _Replay:
        symbols = ("BTC/USDT",)

        def closed_bars(self) -> Iterator[MarketBar]:
            for bar in bars:
                clock.set_time(bar.close_time)
                yield bar

        def close(self) -> None:
            return

    replay = _Replay()
    assert not isinstance(replay, StreamingMarketDataProvider)
    session, _ = _session(clock=clock)

    result = PaperTradingRunner(session=session, feed=replay, max_bars=4).run()

    assert result.runtime.bars_processed == 4


# --- Venue rules reach the report -----------------------------------------------------------


def _telemetry(**overrides: object) -> SymbolRulesTelemetry:
    defaults: dict[str, object] = {
        "refresh_attempts": 4,
        "refresh_successes": 4,
        "last_refresh_at": ANCHOR,
        "age_seconds": 3600.0,
        "stale_after_seconds": 86_400,
    }
    return SymbolRulesTelemetry(**{**defaults, **overrides})  # type: ignore[arg-type]


def _reported_with(tmp_path: Path, telemetry: SymbolRulesTelemetry | None) -> DailyReportRecorder:
    """Run a two-day session that carries one symbol-rules reading throughout."""
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, observer=recorder)
    session.start()
    for bar in _two_day_bars():
        clock.set_time(bar.close_time)
        if telemetry is not None:
            session.record_symbol_rules_telemetry(telemetry)
        session.submit_bar(bar)
    session.stop()
    return recorder


def test_a_refresh_reading_reaches_the_daily_report(tmp_path: Path) -> None:
    recorder = _reported_with(tmp_path, _telemetry(rule_changes=1))

    statistics = recorder.reports[0].statistics

    assert statistics.symbol_rules_telemetry_available is True
    assert statistics.symbol_rules_refresh_attempts == 4
    assert statistics.symbol_rules_refresh_successes == 4
    assert statistics.symbol_rules_changes == 1
    assert statistics.symbol_rules_age_seconds == Decimal("3600.0")
    assert statistics.symbol_rules_stale_after_seconds == 86_400


def test_a_healthy_refresh_leaves_the_day_green(tmp_path: Path) -> None:
    recorder = _reported_with(tmp_path, _telemetry())

    report = recorder.reports[0]

    assert report.health.level is HealthLevel.GREEN
    assert AlertCode.SYMBOL_RULES_STALE not in {a.code for a in report.alerts.alerts}


def test_a_failing_refresh_is_visible_in_the_report(tmp_path: Path) -> None:
    # Visible while the rules in force are still valid — the warning arrives with hours of
    # slack, not after trading has already stopped.
    recorder = _reported_with(
        tmp_path,
        _telemetry(
            refresh_attempts=6,
            refresh_successes=4,
            refresh_failures=2,
            consecutive_failures=2,
            last_failure_reason="DataProviderError: venue unreachable",
        ),
    )

    report = recorder.reports[0]

    assert report.health.level is HealthLevel.YELLOW
    assert AlertCode.SYMBOL_RULES_REFRESH_FAILING in {a.code for a in report.alerts.alerts}
    assert "venue unreachable" in render_markdown(report)


def test_stale_rules_make_the_day_red(tmp_path: Path) -> None:
    recorder = _reported_with(
        tmp_path,
        _telemetry(
            refresh_attempts=8,
            refresh_successes=1,
            refresh_failures=7,
            consecutive_failures=7,
            age_seconds=200_000.0,
            last_failure_reason="OSError: connection reset",
        ),
    )

    report = recorder.reports[0]

    assert report.health.level is HealthLevel.RED
    assert AlertCode.SYMBOL_RULES_STALE in {a.code for a in report.alerts.alerts}


def test_a_session_with_no_refresh_loop_reports_it_as_unmeasured(tmp_path: Path) -> None:
    recorder = _reported_with(tmp_path, None)

    report = recorder.reports[0]

    assert report.statistics.symbol_rules_telemetry_available is False
    assert "No symbol-rules telemetry was supplied" in render_markdown(report)


def test_the_venue_rules_section_survives_a_write_and_read(tmp_path: Path) -> None:
    writer = DailyReportWriter(config=_config(tmp_path))
    recorder = _reported_with(tmp_path, _telemetry(refresh_failures=1, refresh_attempts=5))
    report = recorder.reports[0]

    writer.write(report)
    restored = writer.read_previous(report.day + timedelta(days=1))

    assert restored is not None
    assert restored.statistics.symbol_rules_refresh_failures == 1
    assert restored.statistics.symbol_rules_refresh_attempts == 5


# --- A report cannot invent a loss --------------------------------------------------------


def test_a_day_that_traded_nothing_reports_a_flat_account(tmp_path: Path) -> None:
    # Day one of the aborted run published `drawdown_exceeded: 100.0%` and
    # `large_loss: the day lost 10000` for a session that never placed an order. The peak
    # equity came from configuration while the account held nothing, so the two disagreed
    # and the report believed the larger number.
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, _ = _session(clock=clock, strategy=Silent(_Params()), observer=recorder)

    _run(session, clock, _two_day_bars())

    report = recorder.reports[0]
    statistics = report.statistics
    raised = {alert.code for alert in report.alerts.alerts}

    assert statistics.max_drawdown == Decimal(0)
    assert statistics.daily_pnl == Decimal(0)
    assert statistics.opening_equity == statistics.daily_equity
    assert AlertCode.DRAWDOWN_EXCEEDED not in raised
    assert AlertCode.LARGE_LOSS not in raised


def test_opening_equity_matches_the_account_the_session_actually_holds(tmp_path: Path) -> None:
    clock = SimulatedClock(ANCHOR)
    recorder = _recorder(_config(tmp_path), clock)
    session, portfolio = _session(clock=clock, strategy=Silent(_Params()), observer=recorder)
    opening = portfolio.balances()[0].total  # type: ignore[attr-defined]

    _run(session, clock, _two_day_bars())

    assert recorder.reports[0].statistics.opening_equity == opening
