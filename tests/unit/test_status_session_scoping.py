"""One session at a time, or say so.

Mission Control showed a dead session's numbers under a live session's banner: "NO SESSION
RUNNING" beside a warm-up of 114/21, five signals, -49.26 realised, a long position closed
hours earlier, and a feed reported CONNECTED. Every one of those figures was true of the
smoke run that had already stopped. None was true of the paper session then running.

The failure is not cosmetic. A dashboard whose panels come from different sessions is worse
than one that shows nothing: it invites an operator to act on a position that does not
exist. So the rule these tests pin is that every panel is scoped to one session id, chosen
by the lock a live process holds, and anything belonging to a different session is dropped
rather than displayed — N/A over recycled.

Where the sources genuinely disagree the answer is not to pick a winner quietly. It is to
say the data is mixed and refuse to look healthy.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.enums import ExecutionMode, MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.portfolio import Balance, Position
from quantplatform.reporting.config import ReportingConfiguration
from quantplatform.reporting.models import (
    DailyAlerts,
    DailyHealth,
    DailyReport,
    DailySeries,
    DailyStatistics,
    DailySummary,
    HealthLevel,
)
from quantplatform.reporting.writer import DailyReportWriter
from quantplatform.status.events import read_feed_state
from quantplatform.status.model import Health, SessionStatus, gather_status
from quantplatform.storage.paper_state import FilePaperStateRepository
from quantplatform.storage.session_lock import LOCK_FILENAME
from quantplatform.strategies.registry import build_default_registry
from tests.factories import SYMBOL

OLD = "vps-smoke-72h-riskv2-breakout-2026-09-04"
NEW = "vps-paper-riskv2-breakout-20260909T150000Z"
DEAD_PID = 999_999


def _write_lock(directory: Path, *, session_id: str, pid: int, started_at: datetime) -> None:
    (directory / LOCK_FILENAME).write_text(
        json.dumps({"session_id": session_id, "pid": pid, "started_at": started_at.isoformat()}),
        encoding="utf-8",
    )


def _write_state(directory: Path, *, session_id: str, **overrides: object) -> None:
    """Persist a snapshot shaped like the smoke's: warmed up, traded, carrying a position."""
    saved = overrides.pop("saved_at", datetime.now(UTC) - timedelta(hours=1))
    assert isinstance(saved, datetime)
    # The model refuses a session that processed bars without recording the last one, so the
    # fixture cannot describe a state the platform would never have written.
    overrides.setdefault("last_bar", _bar_closing_at(saved))
    state = PaperSessionState(
        session_id=session_id,
        strategy_id="breakout",
        execution_mode=ExecutionMode.PAPER,
        quote_asset="USDT",
        started_at=saved - timedelta(days=4),
        saved_at=saved,
        balances=(
            Balance(asset="USDT", free=Decimal("7290.54"), locked=Decimal(0), updated_at=saved),
        ),
        positions=(
            Position(
                symbol=SYMBOL,
                base_asset="BTC",
                quote_asset="USDT",
                quantity=Decimal("0.03355"),
                avg_entry_price=Decimal("79290.29"),
                realized_pnl=Decimal(0),
                fees_paid=Decimal("2.65"),
                opened_at=saved,
                updated_at=saved,
            ),
        ),
        bars_processed=114,
        realized_pnl=Decimal("-49.26"),
        total_fees=Decimal("13.29"),
        restarts=0,
        **overrides,  # type: ignore[arg-type]
    )
    FilePaperStateRepository(directory).save(state)


def _settings(tmp_path: Path, **paper: object) -> Settings:
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    reports = tmp_path / "reports"
    for path in (state, logs, reports):
        path.mkdir(exist_ok=True)
    return load_settings(
        paper={
            "state_directory": str(state),
            "log_directory": str(logs),
            "reports_directory": str(reports),
            **paper,
        }
    )


def _gather(settings: Settings, session_id: str | None = None) -> SessionStatus:
    return gather_status(settings, registry=build_default_registry(), session_id=session_id)


# --- A live lock decides which session is being described ------------------------------------


def test_a_live_lock_wins_over_the_configured_session(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=OLD)
    state = Path(settings.paper.state_directory)
    _write_state(state, session_id=OLD)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))

    status = _gather(settings)

    assert status.session_id == NEW
    assert status.running is True


def test_the_old_sessions_figures_do_not_leak_into_the_new_one(tmp_path: Path) -> None:
    # The exact contamination the dashboard showed. The new session has written no snapshot
    # yet, so every one of these must be unknown — not the dead session's numbers.
    settings = _settings(tmp_path, session_id=OLD)
    state = Path(settings.paper.state_directory)
    _write_state(state, session_id=OLD)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))

    status = _gather(settings)

    assert status.session_id == NEW
    assert status.realized_pnl is None
    assert status.total_fees is None
    assert status.bars_processed is None
    assert status.open_positions == ()
    assert status.position_risk == ()
    assert status.state_present is False


def test_an_explicit_request_for_a_dead_session_no_longer_hides_the_live_one(
    tmp_path: Path,
) -> None:
    # Mission Control pinned the old id in its environment, and the pin outranked the lock.
    # The pin is still honoured — inspecting a finished session is legitimate — but it can no
    # longer be honoured *silently* while something else is running.
    settings = _settings(tmp_path, session_id=OLD)
    state = Path(settings.paper.state_directory)
    _write_state(state, session_id=OLD)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))

    status = _gather(settings, session_id=OLD)

    assert status.session_id == OLD
    assert status.running is False
    assert status.health == Health.DEGRADED
    assert any(NEW in note and "running" in note for note in status.notes)


def test_a_dead_lock_holder_does_not_make_its_session_the_live_one(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=OLD)
    state = Path(settings.paper.state_directory)
    _write_state(state, session_id=OLD)
    _write_lock(state, session_id=OLD, pid=DEAD_PID, started_at=datetime.now(UTC))

    status = _gather(settings)

    assert status.running is False
    assert status.health == Health.DEGRADED


def test_with_no_lock_at_all_the_configured_session_is_described(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=OLD)
    _write_state(Path(settings.paper.state_directory), session_id=OLD)

    status = _gather(settings)

    assert status.session_id == OLD
    assert status.running is False
    assert status.health == Health.STOPPED


# --- Reports are never borrowed from another session ------------------------------------------


def test_a_daily_report_written_by_another_session_is_not_shown(tmp_path: Path) -> None:
    # Signals, exposure and trade counts came from here. The reports directory outlives the
    # session that filled it, so a report has to prove whose it is before it may be read.
    settings = _settings(tmp_path, session_id=OLD)
    state = Path(settings.paper.state_directory)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))
    _write_report(Path(settings.paper.reports_directory), session_id=OLD)

    status = _gather(settings)

    assert status.session_id == NEW
    assert status.report is None
    assert any("another session" in note for note in status.notes)


def test_a_daily_report_from_the_active_session_is_shown(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=NEW)
    state = Path(settings.paper.state_directory)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))
    _write_report(Path(settings.paper.reports_directory), session_id=NEW)

    status = _gather(settings)

    assert status.report is not None
    assert status.report.session_id == NEW


# --- Feed state belongs to a session, not to a log file ----------------------------------------


def test_a_feed_transition_older_than_the_session_is_not_its_feed_state(tmp_path: Path) -> None:
    # "Feed CONNECTED" beside "no session running" came from here: the marketdata log has no
    # session id in it, so the last transition ever written was read as the current one.
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_feed_log(logs, at=datetime(2026, 9, 9, 14, 0, tzinfo=UTC), to="streaming")

    assert read_feed_state(logs) == "streaming"
    assert read_feed_state(logs, since=datetime(2026, 9, 9, 15, 2, tzinfo=UTC)) is None


def test_a_feed_transition_after_the_session_started_is_its_feed_state(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_feed_log(logs, at=datetime(2026, 9, 9, 15, 3, tzinfo=UTC), to="connected")

    assert read_feed_state(logs, since=datetime(2026, 9, 9, 15, 2, tzinfo=UTC)) == "connected"


# --- Freshness is reported, not assumed ---------------------------------------------------------


def test_a_status_reports_how_old_its_snapshot_is(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=NEW)
    state = Path(settings.paper.state_directory)
    saved = datetime.now(UTC) - timedelta(minutes=30)
    _write_state(state, session_id=NEW, saved_at=saved)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=saved)

    status = _gather(settings)

    assert status.snapshot_age is not None
    assert timedelta(minutes=29) < status.snapshot_age < timedelta(minutes=31)


def test_a_session_with_no_snapshot_reports_no_age_rather_than_zero(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=NEW)
    state = Path(settings.paper.state_directory)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))

    status = _gather(settings)

    assert status.snapshot_age is None
    assert status.last_bar_age is None


def test_the_age_of_the_last_bar_is_measured_from_its_close(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=NEW)
    state = Path(settings.paper.state_directory)
    bar = _bar_closing_at(datetime.now(UTC) - timedelta(hours=2))
    _write_state(state, session_id=NEW, last_bar=bar)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))

    status = _gather(settings)

    expected = datetime.now(UTC) - bar.close_time
    assert status.last_bar_age is not None
    assert abs(status.last_bar_age - expected) < timedelta(seconds=5)
    assert status.last_bar_age >= timedelta(hours=2)


# --- Mixed sources are named, not silently reconciled --------------------------------------------


def test_mixed_session_data_is_reported_as_degraded(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=OLD)
    state = Path(settings.paper.state_directory)
    _write_state(state, session_id=OLD)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))

    status = _gather(settings, session_id=OLD)

    assert status.mixed_session_data is True
    assert status.health == Health.DEGRADED


def test_a_single_session_is_not_flagged_as_mixed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, session_id=NEW)
    state = Path(settings.paper.state_directory)
    _write_state(state, session_id=NEW)
    _write_lock(state, session_id=NEW, pid=os.getpid(), started_at=datetime.now(UTC))

    status = _gather(settings)

    assert status.mixed_session_data is False
    assert status.health == Health.HEALTHY


def _write_report(directory: Path, *, session_id: str) -> None:
    day = datetime.now(UTC).date()
    statistics = DailyStatistics(
        opening_equity=Decimal(10_000), daily_equity=Decimal(10_000), daily_pnl=Decimal(0)
    )
    report = DailyReport(
        session_id=session_id,
        strategy_id="breakout",
        day=day,
        generated_at=datetime.now(UTC),
        quote_asset="USDT",
        timezone="UTC",
        statistics=statistics,
        health=DailyHealth(level=HealthLevel.GREEN, checks=()),
        alerts=DailyAlerts(alerts=()),
        summary=DailySummary(
            headline="a quiet day", profit_line="flat", health_line="green across the board"
        ),
        series=DailySeries(),
        trades=(),
    )
    DailyReportWriter(config=ReportingConfiguration(output_directory=directory)).write(report)


def _write_feed_log(directory: Path, *, at: datetime, to: str) -> None:
    (directory / "marketdata.log").write_text(
        json.dumps(
            {
                "timestamp": at.isoformat(),
                "level": "INFO",
                "logger": "quantplatform.marketdata.feed",
                "message": "feed state transition",
                "extra": {"feed": "binance_spot_ws", "from": "connecting", "to": to},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _bar_closing_at(close: datetime) -> MarketBar:
    """Return a closed hourly bar, snapped down onto the hour grid the model requires."""
    close = close.replace(minute=0, second=0, microsecond=0)
    return MarketBar(
        symbol=SYMBOL,
        market_type=MarketType.SPOT,
        timeframe=Timeframe.H1,
        open_time=close - timedelta(hours=1),
        close_time=close,
        open=Decimal(50_000),
        high=Decimal(50_100),
        low=Decimal(49_900),
        close=Decimal(50_000),
        volume=Decimal(10),
        quote_volume=Decimal(500_000),
        trade_count=100,
        source="test",
        is_closed=True,
    )
