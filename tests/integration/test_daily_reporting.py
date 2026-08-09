"""Phase 7B: reports produced by a real session, and written to a real directory.

The session below is the production one, driving the production strategy, risk engine,
simulated broker and portfolio. The reporting layer is bolted on as an observer and the
tests check two things at once: that the numbers it reports match what the engine itself
computed, and that observing changed nothing about what the session did.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.models.market import MarketBar
from quantplatform.paper import PaperTradingSession
from quantplatform.paper.results import SessionResult
from quantplatform.reporting import (
    CHART_FILENAMES,
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
from tests.factories import ANCHOR, make_backtest, make_bars
from tests.integration.test_backtest_engine import (
    BuyOnce,
    BuyThenSell,
    Silent,
    _flat_bars,
    _Params,
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

        def on_day_rollover(self, *, completed_day: date, result: SessionResult) -> None:
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
    assert statistics.reconnect_count == 5
    assert statistics.gap_count == 2
    assert statistics.duplicate_candles == 7
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
