"""``quantplatform status`` describes a session without being able to touch it.

The command exists because reading a live paper session meant `journalctl`, `tail`, `jq` and
knowing which file held which number. Everything here is about the two properties that make
it safe to hand to someone who is not going to read the source: it tells the truth, including
when the truth is "I don't know", and it cannot change anything.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantplatform.cli.main import app
from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.enums import CircuitBreakerReason, ExecutionMode, StopKind
from quantplatform.core.models.paper import CURRENT_SCHEMA_VERSION, PaperSessionState
from quantplatform.core.models.portfolio import Balance, Position
from quantplatform.core.models.risk import CircuitBreakerState, PositionRiskState, StopSpecification
from quantplatform.status import Health, SessionStatus, gather_status, render_status
from quantplatform.status import model as status_model
from quantplatform.storage.paper_state import FilePaperStateRepository
from quantplatform.storage.session_lock import SessionLock
from quantplatform.strategies.registry import build_default_registry
from tests.factories import SYMBOL, make_bar

SESSION = "status-session"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def directories(tmp_path: Path) -> dict[str, Path]:
    made = {
        "reports_directory": tmp_path / "reports",
        "state_directory": tmp_path / "state",
        "log_directory": tmp_path / "logs",
    }
    for path in made.values():
        path.mkdir(parents=True, exist_ok=True)
    return made


def _settings(directories: dict[str, Path], **paper: object) -> Settings:
    return load_settings(
        paper={
            "session_id": SESSION,
            "strategy_id": "breakout",
            "strategy_params": {"entry_lookback": "20", "exit_lookback": "10"},
            "symbols": (SYMBOL,),
            **{key: str(value) for key, value in directories.items()},
            **paper,
        }
    )


def _state(**overrides: object) -> PaperSessionState:
    started = datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "session_id": SESSION,
        "strategy_id": "breakout",
        "execution_mode": ExecutionMode.PAPER,
        "quote_asset": "USDT",
        "started_at": started,
        "saved_at": started + timedelta(hours=1),
        "balances": (
            Balance(
                asset="USDT",
                free=Decimal("10000"),
                locked=Decimal(0),
                updated_at=started + timedelta(hours=1),
            ),
        ),
        "bars_processed": 5,
        # The model refuses a session that claims processed bars but records none, so the
        # default fixture is a coherent session rather than an impossible one.
        "last_bar": make_bar(close=Decimal("60000")),
        "realized_pnl": Decimal(0),
        "total_fees": Decimal(0),
    }
    base.update(overrides)
    return PaperSessionState(**base)  # type: ignore[arg-type]


def _persist(directories: dict[str, Path], state: PaperSessionState) -> None:
    FilePaperStateRepository(directories["state_directory"]).save(state)


def _gather(directories: dict[str, Path], **paper: object) -> SessionStatus:
    return gather_status(_settings(directories, **paper), registry=build_default_registry())


# --- running and stopped ---------------------------------------------------------------


def test_a_session_with_no_lock_reads_as_stopped(directories: dict[str, Path]) -> None:
    status = _gather(directories)

    assert status.running is False
    assert status.health == Health.STOPPED


def test_a_session_holding_a_live_lock_reads_as_running(directories: dict[str, Path]) -> None:
    lock = SessionLock(directory=directories["state_directory"], session_id=SESSION)
    lock.acquire(now=datetime.now(UTC))
    try:
        _persist(directories, _state())

        status = _gather(directories)

        assert status.running is True
        assert status.health == Health.HEALTHY
    finally:
        lock.release()


def test_a_lock_held_by_a_dead_process_is_reported_not_believed(
    directories: dict[str, Path],
) -> None:
    # A pid that cannot exist. The session died without releasing its lock, and saying
    # "running" here would be the most dangerous sentence this command could print.
    SessionLock(
        directory=directories["state_directory"], session_id=SESSION, pid=999_999_998
    ).acquire(now=datetime.now(UTC))
    _persist(directories, _state())

    status = _gather(directories)

    assert status.running is False
    assert status.health == Health.DEGRADED
    assert any("no longer running" in note for note in status.notes)


# --- warm-up ------------------------------------------------------------------------------


def test_warmup_is_incomplete_before_the_strategy_has_its_history(
    directories: dict[str, Path],
) -> None:
    _persist(directories, _state(bars_processed=5))

    status = _gather(directories)

    assert status.required_history == 21
    assert status.warmup_complete is False
    assert "5 / 21" in render_status(status, colour=False)


def test_warmup_is_complete_once_enough_bars_have_been_seen(
    directories: dict[str, Path],
) -> None:
    _persist(directories, _state(bars_processed=21))

    status = _gather(directories)

    assert status.warmup_complete is True
    assert "COMPLETE" in render_status(status, colour=False)


# --- warm-up of a strategy whose contract comes from its parameters ------------------------

# ``breakout_trend`` is the first promoted strategy whose warm-up is a property of the
# instance rather than of the class: its METADATA describes the canonical 20/10/200
# configuration, and the session configured below runs 40/20/400. The engine has always
# validated against the instance — ``BaseStrategy.validate_context`` reads ``self.metadata``,
# which ``ParametricStrategy`` overrides — so a panel reading the class was quietly
# describing a different strategy from the one trading.


def _parametric(directories: dict[str, Path], **paper: object) -> SessionStatus:
    """Gather status for a session running ``breakout_trend`` at M23's 40/20/400."""
    _persist(directories, _state(strategy_id="breakout_trend", bars_processed=5))
    return _gather(
        directories,
        strategy_id="breakout_trend",
        strategy_params={"entry_lookback": "40", "exit_lookback": "20", "trend_period": "400"},
        **paper,
    )


def test_warmup_is_measured_against_the_configured_instance_not_the_class(
    directories: dict[str, Path],
) -> None:
    status = _parametric(directories)

    # 400, from sma_400, and not the 200 the class declares for its canonical sma_200.
    assert build_default_registry().metadata_for("breakout_trend").required_history == 200
    assert status.required_history == 400
    assert status.warmup_complete is False
    assert "5 / 400" in render_status(status, colour=False)


def test_the_panel_does_not_call_a_parametric_warmup_complete_early(
    directories: dict[str, Path],
) -> None:
    # The bug's visible consequence: at 200 bars the class figure says COMPLETE while the
    # engine is still refusing to let the strategy have an opinion, roughly a month early.
    _persist(directories, _state(strategy_id="breakout_trend", bars_processed=200))

    status = _gather(
        directories,
        strategy_id="breakout_trend",
        strategy_params={"entry_lookback": "40", "exit_lookback": "20", "trend_period": "400"},
    )

    assert status.warmup_complete is False
    assert "COMPLETE" not in render_status(status, colour=False)


def test_a_strategy_that_declares_on_the_class_is_unchanged(
    directories: dict[str, Path],
) -> None:
    # ``breakout`` derives nothing from its parameters, so instance and class agree and this
    # fix must not move its number.
    _persist(directories, _state(bars_processed=5))

    status = _gather(directories)

    declared = build_default_registry().metadata_for("breakout").required_history
    assert status.required_history == declared
    assert status.required_history == 21


def test_parameters_the_strategy_refuses_fall_back_to_the_class_and_say_so(
    directories: dict[str, Path],
) -> None:
    # A misconfigured session still gets a reading. The number shown is the class default,
    # which may not be this session's, so the reading says that rather than implying it.
    _persist(directories, _state(strategy_id="breakout_trend", bars_processed=5))

    status = _gather(
        directories,
        strategy_id="breakout_trend",
        strategy_params={"entry_lookback": "40"},
    )

    assert status.required_history == 200
    assert any("configured parameters" in note for note in status.notes)


def test_parameters_belonging_to_another_strategy_are_not_applied(
    directories: dict[str, Path],
) -> None:
    # The session is running one strategy and configuration has moved on to another. The
    # configured parameters describe the configured strategy, not the running one, so they
    # may not be used to build it.
    _persist(directories, _state(strategy_id="breakout_trend", bars_processed=5))

    status = _gather(
        directories,
        strategy_id="breakout",
        strategy_params={"entry_lookback": "20", "exit_lookback": "10"},
    )

    assert status.strategy_id == "breakout_trend"
    assert status.required_history == 200
    assert any("cannot describe it" in note for note in status.notes)


def test_a_reader_configured_with_no_strategy_at_all_says_what_it_is_showing(
    directories: dict[str, Path],
) -> None:
    # Mission Control is pointed at a session's directories and may be started without the
    # session's own environment. It then knows which strategy is running, from the state
    # file, but not the numbers it is running — so it shows the class figure and labels it
    # rather than presenting 200 as if it were this session's requirement.
    _persist(directories, _state(strategy_id="breakout_trend", bars_processed=5))

    status = _gather(directories, strategy_id=None, strategy_params={})

    assert status.required_history == 200
    assert any("may not be this session's" in note for note in status.notes)


# --- positions -----------------------------------------------------------------------------


def test_a_flat_session_says_so_plainly(directories: dict[str, Path]) -> None:
    _persist(directories, _state())

    rendered = render_status(_gather(directories), colour=False)

    assert "No open position" in rendered


def test_an_open_position_shows_its_entry_mark_and_stop(directories: dict[str, Path]) -> None:
    opened = datetime(2026, 9, 4, 20, 30, tzinfo=UTC)
    bar = make_bar(close=Decimal("60000"))
    position = Position(
        symbol=SYMBOL,
        base_asset="BTC",
        quote_asset="USDT",
        quantity=Decimal("0.1"),
        avg_entry_price=Decimal("59000"),
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=opened,
        updated_at=opened,
    )
    risk = PositionRiskState(
        symbol=SYMBOL,
        stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("57230")),
        quantity=Decimal("0.1"),
        initial_risk_amount=Decimal("177"),
        current_risk_amount=Decimal("177"),
        entry_price=Decimal("59000"),
        opened_at=opened,
    )
    _persist(directories, _state(positions=(position,), position_risk=(risk,), last_bar=bar))

    status = _gather(directories)
    rendered = render_status(status, colour=False)

    assert status.open_positions
    assert status.open_positions[0].symbol == SYMBOL
    assert "59,000.00" in rendered
    assert "57,230.00" in rendered
    # Unrealised profit is the position's own calculation at the last closed bar, not a
    # figure this command works out for itself.
    assert status.unrealized_pnl == position.unrealized_pnl(bar.close)


def test_an_open_position_without_a_recorded_stop_is_called_out(
    directories: dict[str, Path],
) -> None:
    opened = datetime(2026, 9, 4, 20, 30, tzinfo=UTC)
    position = Position(
        symbol=SYMBOL,
        base_asset="BTC",
        quote_asset="USDT",
        quantity=Decimal("0.1"),
        avg_entry_price=Decimal("59000"),
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=opened,
        updated_at=opened,
    )
    _persist(directories, _state(positions=(position,), last_bar=make_bar(close=Decimal("60000"))))

    rendered = render_status(_gather(directories), colour=False)

    assert "NONE RECORDED" in rendered


# --- breakers -------------------------------------------------------------------------------


def test_a_latched_breaker_makes_the_whole_system_read_failed(
    directories: dict[str, Path],
) -> None:
    # Even with a live process: the session is no longer allowed to trade, and a green
    # banner over a halted account is the single most misleading thing this could print.
    lock = SessionLock(directory=directories["state_directory"], session_id=SESSION)
    lock.acquire(now=datetime.now(UTC))
    try:
        breaker = CircuitBreakerState(
            tripped_at=datetime(2026, 9, 4, 21, 0, tzinfo=UTC),
            reason=CircuitBreakerReason.DAILY_LOSS_LIMIT,
            consecutive_losses=0,
            daily_loss=Decimal("310"),
        )
        _persist(directories, _state(breakers=(breaker,)))

        status = _gather(directories)
        rendered = render_status(status, colour=False)

        assert status.health == Health.FAILED
        assert "TRIPPED" in rendered
        assert "daily_loss_limit" in rendered
    finally:
        lock.release()


# --- unknown is not zero -----------------------------------------------------------------------


def test_missing_data_renders_as_not_available_rather_than_zero(
    directories: dict[str, Path],
) -> None:
    # No snapshot at all. Cash, fees and realised profit are unknown — and reporting them as
    # 0.00 would tell an operator the account is flat and empty when nothing is known at all.
    status = _gather(directories)
    rendered = render_status(status, colour=False)

    assert status.cash is None
    assert status.realized_pnl is None
    assert status.total_fees is None
    assert status.unrealized_pnl is None
    assert "N/A" in rendered
    assert any("no snapshot" in note for note in status.notes)


def test_activity_counts_are_unavailable_until_a_daily_report_exists(
    directories: dict[str, Path],
) -> None:
    _persist(directories, _state())

    rendered = render_status(_gather(directories), colour=False)

    assert "no daily report written yet" in rendered
    # The one metric that exists nowhere at all is named, not quietly omitted.
    assert "signal counts are not persisted" in rendered


def test_progress_is_unavailable_until_a_target_is_declared(
    directories: dict[str, Path],
) -> None:
    _persist(directories, _state())
    status = _gather(directories)

    without = render_status(status, colour=False)
    assert "N/A (no target declared)" in without

    with_target = render_status(status, colour=False, smoke_hours=72)
    assert "Progress" in with_target
    assert "%" in with_target


# --- failing safely -------------------------------------------------------------------------


def test_a_corrupt_state_file_is_reported_rather_than_crashing(
    directories: dict[str, Path],
) -> None:
    path = FilePaperStateRepository(directories["state_directory"]).path_for(SESSION)
    path.write_text("{ this is not json")

    status = _gather(directories)
    rendered = render_status(status, colour=False)

    assert status.state_present is False
    assert status.health == Health.DEGRADED
    assert any("could not be read" in note for note in status.notes)
    assert "N/A" in rendered


def test_a_state_file_of_the_wrong_shape_is_reported_rather_than_crashing(
    directories: dict[str, Path],
) -> None:
    path = FilePaperStateRepository(directories["state_directory"]).path_for(SESSION)
    path.write_text(json.dumps({"session_id": SESSION, "unexpected": True}))

    status = _gather(directories)

    assert status.state_present is False
    assert any("could not be read" in note for note in status.notes)


def test_an_unregistered_strategy_leaves_warmup_unknown_without_failing(
    directories: dict[str, Path],
) -> None:
    _persist(directories, _state(strategy_id="not_a_real_strategy"))

    status = _gather(directories)

    assert status.required_history is None
    assert status.warmup_complete is None


# --- secrets ------------------------------------------------------------------------------------


def test_the_output_never_contains_configuration_secrets(
    directories: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QP_DATABASE__DSN", "postgresql+psycopg://quant:sup3rs3cret@db:5432/qp")
    monkeypatch.setenv("QP_EXCHANGE__API_KEY", "AKIAEXAMPLEKEY")
    monkeypatch.setenv("QP_EXCHANGE__API_SECRET", "s3cr3t-exchange-value")
    _persist(directories, _state())

    rendered = render_status(_gather(directories), colour=False)

    assert "sup3rs3cret" not in rendered
    assert "AKIAEXAMPLEKEY" not in rendered
    assert "s3cr3t-exchange-value" not in rendered
    assert "postgresql" not in rendered


# --- read-only ------------------------------------------------------------------------------------


def test_gathering_status_changes_nothing_on_disk(directories: dict[str, Path]) -> None:
    lock = SessionLock(directory=directories["state_directory"], session_id=SESSION)
    lock.acquire(now=datetime.now(UTC))
    try:
        _persist(directories, _state())
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(directories["state_directory"].rglob("*"))
            if path.is_file()
        }

        _gather(directories)
        render_status(_gather(directories), colour=False)

        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(directories["state_directory"].rglob("*"))
            if path.is_file()
        }
        assert after == before
    finally:
        lock.release()


def test_status_never_claims_the_session_lock(directories: dict[str, Path]) -> None:
    # If status took the lock, a live session could not have it — and the next legitimate
    # start would be refused by a reader.
    _gather(directories)

    lock_file = directories["state_directory"] / "paper-session.lock"
    assert not lock_file.exists()


def test_the_status_domain_cannot_reach_anything_that_trades() -> None:
    # The read-only guarantee, asserted rather than described. `tests/architecture` enforces
    # the same rule across the whole domain; this states it where the command is tested.
    source = Path(status_model.__file__).read_text()
    for forbidden in ("quantplatform.execution", "quantplatform.risk", "quantplatform.paper."):
        assert forbidden not in source


# --- the command itself ------------------------------------------------------------------


def test_the_status_command_runs_and_reports_a_stopped_session(
    directories: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QP_PAPER__SESSION_ID", SESSION)
    monkeypatch.setenv("QP_PAPER__STRATEGY_ID", "breakout")
    monkeypatch.setenv("QP_PAPER__STATE_DIRECTORY", str(directories["state_directory"]))
    monkeypatch.setenv("QP_PAPER__REPORTS_DIRECTORY", str(directories["reports_directory"]))
    monkeypatch.setenv("QP_PAPER__LOG_DIRECTORY", str(directories["log_directory"]))

    result = CliRunner().invoke(app, ["status", "--no-color"])

    assert result.exit_code == 0
    assert "QUANT PLATFORM" in result.stdout
    assert "STOPPED" in result.stdout


def test_the_status_command_has_no_option_that_could_change_a_session() -> None:
    result = CliRunner().invoke(app, ["status", "--help"])

    assert result.exit_code == 0
    for forbidden in ("--resume", "--start", "--stop", "--reset", "--fresh", "--delete"):
        assert forbidden not in result.stdout
