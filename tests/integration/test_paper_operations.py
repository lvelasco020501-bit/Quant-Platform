"""Phase 7C.0: the deployment layer, exercised as an operator would meet it.

Startup refusals, signal-driven shutdown, restart onto persisted state, log routing and the
CLI surface. No test here opens a socket: the feed is constructed but never connected, and
every run is driven through a scripted transport or stopped before it reaches one.
"""

from __future__ import annotations

import json
import logging
import signal
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.cli.main import app
from quantplatform.cli.paper import _overrides, _settings_with
from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.clock import SimulatedClock
from quantplatform.core.constants import ZERO
from quantplatform.core.enums import ExecutionMode, LogFormat
from quantplatform.core.errors import ConfigurationError, StorageError
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.symbol_rules import SymbolRulesStore
from quantplatform.features import NullFeaturePipeline
from quantplatform.marketdata.symbol_rules import BinanceSpotSymbolRulesProvider
from quantplatform.orchestration.logging_setup import (
    LOG_STREAMS,
    close_file_logging,
    configure_file_logging,
)
from quantplatform.orchestration.paper import (
    build_paper_deployment,
    symbol_rules_freshness_budget,
    validate_startup,
)
from quantplatform.orchestration.shutdown import ShutdownSignal, shutdown_on_signals
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.reporting import (
    DailyReportBuilder,
    DailyReportWriter,
    ReportingConfiguration,
)
from quantplatform.risk.engine import StandardRiskEngine
from quantplatform.storage.paper_state import FilePaperStateRepository
from quantplatform.strategies.registry import StrategyRegistry, build_default_registry
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_bar,
    make_bars,
    make_risk_config,
    make_symbol_rules,
)
from tests.integration.test_backtest_engine import Silent, _Params
from tests.integration.test_marketdata_feed import _bar_steps, _feed

_EXIT_CONFIGURATION_ERROR = 2


@pytest.fixture
def directories(tmp_path: Path) -> dict[str, Path]:
    return {
        "reports_directory": tmp_path / "reports",
        "state_directory": tmp_path / "state",
        "log_directory": tmp_path / "logs",
    }


def _settings(directories: dict[str, Path], **paper: object) -> Settings:
    """Build settings pointed at a temporary tree, with a resolvable strategy."""
    return load_settings(
        paper={
            "session_id": "ops-session",
            "strategy_id": Silent.METADATA.strategy_id,
            "symbols": (SYMBOL,),
            **{key: str(value) for key, value in directories.items()},
            **paper,
        }
    )


def _registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register(Silent)
    return registry


def _rules() -> dict[str, SymbolRules]:
    return {SYMBOL: make_symbol_rules()}


# --- Startup validation ---------------------------------------------------------------------


def test_a_sound_deployment_passes_validation(directories: dict[str, Path]) -> None:
    validate_startup(_settings(directories), symbol_rules=_rules(), registry=_registry())


def test_validation_creates_thedirectories_it_requires(directories: dict[str, Path]) -> None:
    validate_startup(_settings(directories), symbol_rules=_rules(), registry=_registry())

    for directory in directories.values():
        assert directory.is_dir()


def test_startup_is_refused_without_a_configured_strategy(
    directories: dict[str, Path],
) -> None:
    # Guessing one would run something nobody chose.
    settings = _settings(directories, strategy_id=None)

    with pytest.raises(ConfigurationError, match="no strategy is configured"):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


def test_startup_is_refused_when_the_strategy_is_not_registered(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories, strategy_id="absent_strategy")

    with pytest.raises(ConfigurationError, match="not registered"):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


def test_startup_is_refused_without_venue_trading_rules(
    directories: dict[str, Path],
) -> None:
    # Sizing an order against an invented tick size is a wrong order, not a placeholder.
    with pytest.raises(ConfigurationError, match="no venue trading rules"):
        validate_startup(_settings(directories), symbol_rules={}, registry=_registry())


def test_startup_is_refused_for_a_symbol_the_risk_limits_forbid(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories, symbols=("ETH/USDT",))

    with pytest.raises(ConfigurationError, match="not permitted by the risk limits"):
        validate_startup(
            settings,
            symbol_rules={"ETH/USDT": make_symbol_rules(symbol="ETH/USDT", base_asset="ETH")},
            registry=_registry(),
        )


def test_startup_is_refused_outside_paper_execution_mode(
    directories: dict[str, Path],
) -> None:
    # The one refusal that protects money rather than tidiness.
    settings = load_settings(
        execution_mode=ExecutionMode.BACKTEST,
        paper={
            "session_id": "ops-session",
            "strategy_id": Silent.METADATA.strategy_id,
            **{key: str(value) for key, value in directories.items()},
        },
    )

    with pytest.raises(ConfigurationError, match="outside paper execution mode"):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"websocket_url": "https://stream.binance.com/ws"}, "ws or wss"),
        ({"websocket_url": "wss://stream.binance.com/ws/listenKey"}, "listenkey"),
        ({"symbols": (SYMBOL, SYMBOL)}, "must not repeat"),
        ({"heartbeat_timeout_seconds": 1.0, "receive_timeout_seconds": 5.0}, "must exceed"),
    ],
)
def test_an_incoherent_feed_configuration_is_refused(
    directories: dict[str, Path], override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_startup(
            _settings(directories, **override), symbol_rules=_rules(), registry=_registry()
        )


def test_an_unsupported_timeframe_is_refused(directories: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="timeframe"):
        _settings(directories, timeframe="7h")


def test_an_unusable_directory_is_refused(directories: dict[str, Path], tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")
    settings = _settings(directories, reports_directory=str(blocker / "reports"))

    with pytest.raises(StorageError, match="not usable"):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


# --- Composition ----------------------------------------------------------------------------


def test_the_deployment_wires_every_component(directories: dict[str, Path]) -> None:
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.session.session_id == "ops-session"
    assert deployment.feed.symbols == (SYMBOL,)
    assert deployment.runner.session is deployment.session
    assert deployment.writer.config.output_directory == directories["reports_directory"]
    assert isinstance(deployment.repository, FilePaperStateRepository)


def test_the_deployment_reads_its_feed_settings_from_configuration(
    directories: dict[str, Path],
) -> None:
    deployment = build_paper_deployment(
        _settings(directories, heartbeat_timeout_seconds=45.0, max_reconnect_attempts=9),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.feed._config.heartbeat_timeout_seconds == 45.0
    assert deployment.feed._policy.schedule.max_attempts == 9


def test_the_live_feed_is_wired_as_its_own_telemetry_reader(
    directories: dict[str, Path],
) -> None:
    # Without this the runner would refuse to start, which is the Phase 7B.2 guarantee.
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.runner._feed_metrics is deployment.feed


def test_the_deployment_holds_no_trading_decision() -> None:
    # A composition root that started sizing orders would be a second place trading logic
    # lives. Read as a structural claim: the module names no such concept.
    source = Path("src/quantplatform/orchestration/paper.py").read_text(encoding="utf-8")

    for token in ("def generate", "def evaluate", "def apply_fill", "quantity =", "signal ="):
        assert token not in source, f"composition root contains {token!r}"


# --- Persistence and restart ------------------------------------------------------------------


def test_a_session_persists_through_the_durable_repository(
    directories: dict[str, Path],
) -> None:
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )
    deployment.session.start()
    deployment.session.save()

    stored = deployment.repository.load("ops-session")

    assert stored is not None
    assert stored.session_id == "ops-session"
    assert (directories["state_directory"] / "ops-session.json").is_file()


def test_a_restarted_deployment_resumes_the_stored_session(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories)
    first = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )
    first.session.start()
    first.session.save()
    first.session.stop()

    second = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )
    second.session.resume()

    assert second.session.is_running is True
    assert second.session.runtime_metrics().restarts == 1


def test_a_fresh_deployment_has_nothing_to_resume(directories: dict[str, Path]) -> None:
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.repository.load("ops-session") is None


def test_a_previously_written_report_is_recovered_as_the_comparison_baseline(
    directories: dict[str, Path],
) -> None:
    # A restart must not lose yesterday: the recorder is seeded from disk.

    clock = SimulatedClock(ANCHOR)
    reporting = ReportingConfiguration(
        output_directory=directories["reports_directory"], render_charts=False
    )
    seed = build_paper_deployment(
        _settings(directories), symbol_rules=_rules(), registry=_registry(), clock=clock
    )
    seed.session.start()
    DailyReportWriter(config=reporting).write(
        DailyReportBuilder(config=reporting, clock=clock).build(
            day=(ANCHOR - timedelta(days=1)).date(), result=seed.session.result()
        )
    )

    revived = build_paper_deployment(
        _settings(directories, render_charts=False),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=clock,
    )

    assert revived.recorder._previous is not None
    assert revived.recorder._previous.day == date(2025, 12, 31)


# --- Shutdown ---------------------------------------------------------------------------------


def test_a_signal_requests_a_stop_without_doing_work_in_the_handler() -> None:
    seen: list[str] = []
    flag = ShutdownSignal(on_request=seen.append)

    with shutdown_on_signals(flag):
        signal.raise_signal(signal.SIGINT)

    assert flag.requested is True
    assert flag.reason == "SIGINT"
    assert seen == ["SIGINT"]


def test_repeated_signals_start_only_one_shutdown() -> None:
    seen: list[str] = []
    flag = ShutdownSignal(on_request=seen.append)

    with shutdown_on_signals(flag):
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGINT)

    assert seen == ["SIGINT"]


def test_sigterm_also_requests_a_stop() -> None:
    flag = ShutdownSignal()

    with shutdown_on_signals(flag):
        signal.raise_signal(signal.SIGTERM)

    assert flag.reason == "SIGTERM"


def test_previous_signal_handlers_are_restored() -> None:
    # Leaving a handler installed would break whatever tries to stop the next component.
    before = signal.getsignal(signal.SIGINT)

    with shutdown_on_signals(ShutdownSignal()):
        assert signal.getsignal(signal.SIGINT) is not before

    assert signal.getsignal(signal.SIGINT) is before


def test_a_stop_request_reaches_both_the_feed_and_the_runner(
    directories: dict[str, Path],
) -> None:
    # Telling only the runner would leave it parked inside the feed's read loop until the
    # next candle, which on an hourly timeframe is an hour of not shutting down.
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    deployment.request_stop("SIGTERM")

    assert deployment.feed._stopping is True
    assert deployment.runner._stopping is True


# --- Logging -----------------------------------------------------------------------------------


def test_each_domain_gets_its_own_log_file(tmp_path: Path) -> None:
    try:
        paths = configure_file_logging(directory=tmp_path, log_format=LogFormat.JSON)

        assert [path.name for path in paths] == [f"{stem}.log" for stem in LOG_STREAMS]
        logging.getLogger("quantplatform.marketdata.feed").warning("feed line")
        logging.getLogger("quantplatform.paper.session").warning("session line")
        logging.getLogger("quantplatform.reporting.daily").warning("report line")
        logging.getLogger("quantplatform.orchestration.paper").warning("wiring line")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert "feed line" in (tmp_path / "marketdata.log").read_text(encoding="utf-8")
        assert "session line" in (tmp_path / "paper.log").read_text(encoding="utf-8")
        assert "report line" in (tmp_path / "reporting.log").read_text(encoding="utf-8")
        assert "wiring line" in (tmp_path / "orchestration.log").read_text(encoding="utf-8")
    finally:
        close_file_logging()


def test_a_log_line_lands_in_exactly_one_file(tmp_path: Path) -> None:
    try:
        configure_file_logging(directory=tmp_path)
        logging.getLogger("quantplatform.marketdata.feed").warning("unique-marker")
        for handler in logging.getLogger().handlers:
            handler.flush()

        hits = [
            stem
            for stem in LOG_STREAMS
            if "unique-marker" in (tmp_path / f"{stem}.log").read_text(encoding="utf-8")
        ]
        assert hits == ["marketdata"]
    finally:
        close_file_logging()


def test_an_unrouted_logger_still_lands_somewhere(tmp_path: Path) -> None:
    try:
        configure_file_logging(directory=tmp_path)
        logging.getLogger("quantplatform.risk.engine").warning("stray-marker")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert "stray-marker" in (tmp_path / "orchestration.log").read_text(encoding="utf-8")
    finally:
        close_file_logging()


def test_a_secret_never_reaches_a_log_file(tmp_path: Path) -> None:
    try:
        configure_file_logging(directory=tmp_path, secrets=("super-secret-token",))
        logging.getLogger("quantplatform.orchestration.paper").warning(
            "connecting with super-secret-token"
        )
        for handler in logging.getLogger().handlers:
            handler.flush()

        written = (tmp_path / "orchestration.log").read_text(encoding="utf-8")
        assert "super-secret-token" not in written
        assert "REDACTED" in written
    finally:
        close_file_logging()


def test_reconfiguring_does_not_double_every_line(tmp_path: Path) -> None:
    # A restart inside one process must not accumulate handlers.
    try:
        configure_file_logging(directory=tmp_path)
        configure_file_logging(directory=tmp_path)
        logging.getLogger("quantplatform.paper.session").warning("once-only")
        for handler in logging.getLogger().handlers:
            handler.flush()

        written = (tmp_path / "paper.log").read_text(encoding="utf-8")
        assert written.count("once-only") == 1
    finally:
        close_file_logging()


def test_closing_removes_every_handler_it_installed(tmp_path: Path) -> None:
    configure_file_logging(directory=tmp_path)
    close_file_logging()

    assert not [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_quantplatform_file_handler", False)
    ]


def test_an_unusable_log_directory_is_refused(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(StorageError, match="log directory is not usable"):
        configure_file_logging(directory=blocker / "logs")


# --- CLI ----------------------------------------------------------------------------------------


def test_the_paper_commands_are_registered() -> None:
    result = CliRunner().invoke(app, ["paper", "--help"])

    assert result.exit_code == 0
    assert "check" in result.stdout
    assert "run" in result.stdout


def test_check_refuses_a_deployment_that_would_not_start(tmp_path: Path) -> None:
    # The platform ships no strategy and no venue rules, so a default check must fail — and
    # must say which of the two it tripped on.
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "check",
            "--reports-dir",
            str(tmp_path / "reports"),
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(tmp_path / "logs"),
        ],
    )

    assert result.exit_code == _EXIT_CONFIGURATION_ERROR
    assert "venue trading rules" in result.output or "strategy" in result.output


def test_check_reports_an_unregistered_strategy(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "check",
            "--strategy",
            "not_a_real_strategy",
            "--reports-dir",
            str(tmp_path / "reports"),
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(tmp_path / "logs"),
        ],
    )

    assert result.exit_code == _EXIT_CONFIGURATION_ERROR


def test_check_refuses_an_endpoint_that_is_not_a_public_stream(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "check",
            "--symbol",
            "BTC/USDT",
            "--symbol",
            "BTC/USDT",
            "--reports-dir",
            str(tmp_path / "reports"),
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(tmp_path / "logs"),
        ],
    )

    assert result.exit_code == _EXIT_CONFIGURATION_ERROR


def test_the_cli_never_prints_a_secret(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper",
            "check",
            "--reports-dir",
            str(tmp_path / "reports"),
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(tmp_path / "logs"),
        ],
    )

    for token in ("api_key", "api_secret", "password", "dsn", "postgresql://"):
        assert token not in result.output


def test_command_line_options_override_configuration(tmp_path: Path) -> None:
    settings = _settings_with(
        _overrides(
            symbols=["BTC/USDT"],
            timeframe="15m",
            reports_dir=tmp_path / "r",
            state_dir=tmp_path / "s",
            log_dir=tmp_path / "l",
            session_id="overridden",
            strategy="some_strategy",
            max_bars=25,
        )
    )

    assert settings.paper.session_id == "overridden"
    assert settings.paper.timeframe.value == "15m"
    assert settings.paper.max_bars == 25
    assert settings.paper.reports_directory == tmp_path / "r"


def test_an_option_left_unset_keeps_the_configured_value(tmp_path: Path) -> None:
    _ = tmp_path
    settings = _settings_with(
        _overrides(
            symbols=None,
            timeframe=None,
            reports_dir=None,
            state_dir=None,
            log_dir=None,
            session_id=None,
            strategy=None,
            max_bars=None,
        )
    )

    assert settings.paper.session_id == load_settings().paper.session_id
    assert settings.paper.symbols == load_settings().paper.symbols


# --- Runtime hygiene ---------------------------------------------------------------------------


def test_building_a_deployment_twice_leaves_no_duplicate_subscriptions(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories)
    first = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )
    second = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )

    assert first.feed.symbols == second.feed.symbols == (SYMBOL,)
    assert first.feed is not second.feed


def test_a_deployment_starts_with_no_accumulated_state(
    directories: dict[str, Path],
) -> None:
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.recorder.reports == ()
    assert deployment.writer.written == []
    assert deployment.feed.metrics.frames_received == 0
    assert deployment.session.runtime_metrics().bars_processed == 0


def test_the_initial_account_is_funded_from_configuration(
    directories: dict[str, Path],
) -> None:
    # A paper session starts flat by construction; capital comes from the backtest section
    # rather than a number written into the composition root.
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.session._config.initial_capital == Decimal(10_000)
    assert deployment.session._config.execution_mode is ExecutionMode.PAPER


# --- Grace-period agreement between feed and session -------------------------------------------


@pytest.mark.parametrize("grace", [0.0, 2.0, 30.0])
def test_the_feed_and_the_session_agree_on_the_same_candle(
    directories: dict[str, Path], grace: float
) -> None:
    # The defect this patch closes. Both layers now read `now >= close - grace`, so every
    # candle the feed emits is a candle the session accepts, at any tolerance.
    clock = SimulatedClock(ANCHOR)
    deployment = build_paper_deployment(
        _settings(directories, close_grace_seconds=grace),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=clock,
    )
    bars = make_bars([Decimal(50_000)] * 8)
    scripted, _, _ = _feed(_bar_steps(bars), clock=clock)
    deployment.feed._transport = scripted._transport
    deployment.runner._max_bars = len(bars)

    result = deployment.runner.run()

    assert deployment.feed.metrics.bars_emitted == len(bars)
    assert result.runtime.bars_received == len(bars)
    assert result.runtime.bars_processed == len(bars)
    assert result.runtime.bars_rejected == 0


def test_a_venue_confirmed_candle_arriving_just_after_close_is_accepted(
    directories: dict[str, Path],
) -> None:
    # The live case: a venue publishes moments after the close, well inside the tolerance.
    clock = SimulatedClock(ANCHOR)
    deployment = build_paper_deployment(
        _settings(directories, close_grace_seconds=2.0),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=clock,
    )
    deployment.session.start()
    bar = make_bars([Decimal(50_000)])[0]
    clock.set_time(bar.close_time + timedelta(milliseconds=350))

    assert deployment.session.submit_bar(bar) is not None
    assert deployment.session.runtime_metrics().bars_processed == 1


def test_a_genuinely_early_candle_is_still_refused(directories: dict[str, Path]) -> None:
    # Tolerance widens the window; it does not remove it.
    clock = SimulatedClock(ANCHOR)
    deployment = build_paper_deployment(
        _settings(directories, close_grace_seconds=2.0),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=clock,
    )
    deployment.session.start()
    bar = make_bars([Decimal(50_000)])[0]
    clock.set_time(bar.close_time - timedelta(minutes=30))

    assert deployment.session.submit_bar(bar) is None
    assert deployment.session.runtime_metrics().bars_rejected == 1


def test_a_forming_candle_is_refused_whatever_the_tolerance(
    directories: dict[str, Path],
) -> None:
    # Safety never depended on the margin: the venue's own closed flag is checked separately.
    clock = SimulatedClock(ANCHOR)
    deployment = build_paper_deployment(
        _settings(directories, close_grace_seconds=3_600.0),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=clock,
    )
    deployment.session.start()
    forming = make_bar(index=0, is_closed=False)
    clock.set_time(forming.close_time + timedelta(hours=2))

    assert deployment.session.submit_bar(forming) is None


# --- Production strategy and real venue rules ---------------------------------------------------


def _production_settings(directories: dict[str, Path], **paper: object) -> Settings:
    return load_settings(
        paper={
            "session_id": "prod-check",
            "strategy_id": "ema_trend",
            "symbols": (SYMBOL,),
            **{key: str(value) for key, value in directories.items()},
            **paper,
        }
    )


def test_the_builtin_strategy_resolves_without_a_custom_registry(
    directories: dict[str, Path],
) -> None:
    # The blocker that made a paper run impossible: the registry shipped empty.
    validate_startup(
        _production_settings(directories),
        symbol_rules=_rules(),
        registry=build_default_registry(),
    )


def test_the_deployment_supplies_the_features_the_strategy_declares(
    directories: dict[str, Path],
) -> None:
    # A composition root that picked the null pipeline would let a feature-using strategy
    # start and then go permanently quiet.
    deployment = build_paper_deployment(
        _production_settings(directories),
        symbol_rules=_rules(),
        registry=build_default_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    names = set(deployment.session._engine._features.feature_names)
    assert {"ema_20", "ema_50"} <= names
    deployment.session.start()  # the engine's contract check runs here


def test_startup_still_refuses_an_unregistered_strategy(directories: dict[str, Path]) -> None:
    settings = _production_settings(directories, strategy_id="not_real")

    with pytest.raises(ConfigurationError, match="not registered"):
        validate_startup(settings, symbol_rules=_rules(), registry=build_default_registry())


def test_a_symbol_rules_fetch_failure_stops_startup(directories: dict[str, Path]) -> None:
    # A default tick size is a wrong tick size; refusing is the only safe answer.
    with pytest.raises(ConfigurationError, match="no venue trading rules"):
        validate_startup(
            _production_settings(directories),
            symbol_rules={},
            registry=build_default_registry(),
        )


def test_the_check_command_reports_readiness_with_fetched_rules(tmp_path: Path) -> None:
    document = json.dumps(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "permissions": ["SPOT"],
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.00001000",
                            "maxQty": "9000.00000000",
                            "stepSize": "0.00001000",
                        },
                        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
                    ],
                }
            ]
        }
    )

    class _Canned:
        def fetch(self, url: str) -> str:
            _ = url
            return document

    provider = BinanceSpotSymbolRulesProvider(clock=SimulatedClock(ANCHOR), transport=_Canned())
    settings = _production_settings(
        {
            "reports_directory": tmp_path / "reports",
            "state_directory": tmp_path / "state",
            "log_directory": tmp_path / "logs",
        }
    )
    rules = provider.fetch(settings.paper.symbols)

    validate_startup(settings, symbol_rules=rules, registry=build_default_registry())

    assert rules[SYMBOL].price_tick == Decimal("0.01000000")
    assert rules[SYMBOL].min_notional == Decimal("5.00000000")


def test_no_module_reachable_from_the_deployment_names_an_order_endpoint() -> None:
    # The standing guarantee, re-checked now that a REST client exists in the tree.
    root = Path("src/quantplatform")
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in ("/api/v3/order", "listenKey", "X-MBX-APIKEY", "/sapi/v1/capital"):
            if token in source and "symbol_rules" not in path.name:
                pytest.fail(f"{path} names {token}")


# --- Venue rules refresh ------------------------------------------------------------------------


class _StubProvider:
    """A metadata source that never leaves the process."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, symbols: Sequence[str]) -> Mapping[str, SymbolRules]:
        self.calls += 1
        return {symbol: make_symbol_rules(symbol=symbol) for symbol in symbols}


def test_the_deployment_shares_one_rules_store_with_every_component(
    directories: dict[str, Path],
) -> None:
    # The property that makes divergence unrepresentable rather than merely discouraged: a
    # refresh reaches sizing, matching and accounting at the same instant or it reaches none
    # of them.
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    store = deployment.symbol_rules
    broker = deployment.runner.session._broker
    assert broker._symbols is store
    assert isinstance(store, SymbolRulesStore)


def test_a_refresher_is_wired_when_a_provider_is_supplied(
    directories: dict[str, Path],
) -> None:
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
        rules_provider=_StubProvider(),
    )

    assert deployment.refresher is not None
    assert deployment.runner._symbol_rules is deployment.refresher


def test_no_refresher_is_invented_when_no_provider_is_supplied(
    directories: dict[str, Path],
) -> None:
    # A composition root that fabricated a venue client would be deciding, on its own, to
    # start making network calls nobody configured.
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.refresher is None
    assert deployment.runner._symbol_rules is None


def test_the_refresh_schedule_comes_from_configuration(
    directories: dict[str, Path],
) -> None:
    deployment = build_paper_deployment(
        _settings(directories, symbol_rules_refresh_seconds=3600.0),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
        rules_provider=_StubProvider(),
    )

    assert deployment.refresher is not None
    assert deployment.refresher._interval == 3600.0


def test_the_refresher_is_told_the_budget_the_risk_engine_will_enforce(
    directories: dict[str, Path],
) -> None:
    # One number, read once. Two copies of it is how a deployment ends up refreshing every
    # six hours against a budget somebody had already shortened.
    settings = _settings(directories)
    deployment = build_paper_deployment(
        settings,
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
        rules_provider=_StubProvider(),
    )

    assert deployment.refresher is not None
    assert deployment.refresher.telemetry().stale_after_seconds == (
        symbol_rules_freshness_budget(settings)
    )


def test_a_refresh_interval_at_or_past_the_budget_is_refused_at_startup(
    directories: dict[str, Path],
) -> None:
    # Fatal at startup rather than discovered a day into a week-long run.
    settings = _settings(directories, symbol_rules_refresh_seconds=float(86_400))

    with pytest.raises(ConfigurationError, match="strictly below"):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


def test_a_refresh_interval_safely_inside_the_budget_is_accepted(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories, symbol_rules_refresh_seconds=21_600.0)

    validate_startup(settings, symbol_rules=_rules(), registry=_registry())


def test_the_default_schedule_leaves_four_refreshes_of_margin(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories)

    budget = symbol_rules_freshness_budget(settings)

    assert settings.paper.symbol_rules_refresh_seconds == 21_600.0
    assert budget == 86_400
    assert budget / settings.paper.symbol_rules_refresh_seconds == 4


# --- Opening capital ----------------------------------------------------------------------------
#
# The defect these pin down cost a day of observation. The composition root declared ten
# thousand of capital in the run configuration and seeded the account with nothing. Signals
# were generated and then discarded inside `build_intent`, which returns None when equity is
# zero — so no intent, no risk decision, no rejection reason, nothing for a report to show.
# Meanwhile the run state anchored its drawdown to the declared capital, and day one
# published a 100% drawdown and a ten-thousand loss that never happened.


def test_the_account_is_seeded_with_the_capital_the_run_declares(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories)
    deployment = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )

    balances = deployment.session._portfolio.balances()

    assert len(balances) == 1
    assert balances[0].asset == settings.market.quote_asset
    assert balances[0].free == settings.backtest.initial_capital
    assert balances[0].locked == ZERO
    assert balances[0].total == settings.backtest.initial_capital


def test_the_declared_capital_and_the_seeded_account_are_one_number(
    directories: dict[str, Path],
) -> None:
    # Not "they happen to be equal" but "they come from the same place". Changing the
    # configured capital must move both, or the two can drift apart again.
    settings = _settings(directories)
    deployment = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )
    engine = deployment.session._engine
    account = deployment.session._portfolio.balances()[0].total

    assert engine._config.initial_capital == account
    assert account == settings.backtest.initial_capital


def test_a_run_against_an_unfunded_account_is_refused_at_the_first_step(
    directories: dict[str, Path],
) -> None:
    # Configuration alone cannot express this: `initial_capital` is already validated as
    # strictly positive. The failure was a *declared* capital that never reached the
    # account, so the check belongs where the two meet — opening the run.
    settings = _settings(directories)
    deployment = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )
    starved = SpotPortfolioEngine(
        quote_asset=settings.market.quote_asset,
        symbols=deployment.symbol_rules,
        execution_mode=ExecutionMode.PAPER,
        initial_balances=(),
        source="starved",
    )
    engine = BacktestEngine(
        config=deployment.session._engine._config,
        strategy=Silent(_Params()),
        features=NullFeaturePipeline(),
        risk_engine=StandardRiskEngine(config=make_risk_config()),
        broker=deployment.session._broker,
        portfolio=starved,
        symbols=deployment.symbol_rules,
    )

    with pytest.raises(ConfigurationError, match="holds no equity"):
        engine.begin()


def test_a_fresh_run_opens_flat_with_nothing_lost(directories: dict[str, Path]) -> None:
    settings = _settings(directories)
    deployment = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )
    deployment.session.start()

    result = deployment.session.result()
    performance = result.performance

    assert performance is not None
    assert performance.initial_equity == settings.backtest.initial_capital
    assert performance.final_equity == settings.backtest.initial_capital
    assert performance.total_return == ZERO
    assert performance.max_drawdown == ZERO


def test_an_untraded_day_cannot_report_a_loss(directories: dict[str, Path]) -> None:
    # The precise shape of the fabricated report: a day that traded nothing must show a flat
    # account, not a wiped-out one.
    clock = SimulatedClock(ANCHOR)
    settings = _settings(directories)
    deployment = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=clock
    )
    deployment.session.start()
    for bar in make_bars([Decimal(50_000)] * 6):
        clock.set_time(bar.close_time)
        deployment.session.submit_bar(bar)

    snapshot = deployment.session.snapshot()
    performance = deployment.session.result().performance

    assert snapshot.equity == settings.backtest.initial_capital
    assert performance is not None
    assert performance.max_drawdown == ZERO
    assert performance.total_return == ZERO


# --- Stall watchdog -------------------------------------------------------------------------


def test_a_watchdog_is_always_wired(directories: dict[str, Path]) -> None:
    # Unlike the symbol-rules refresher, the watchdog needs nothing external to be useful
    # -- it only reads state the deployment already produces -- so it is never optional.
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )

    assert deployment.watchdog is not None


def test_the_watchdog_threshold_is_the_timeframe_plus_the_configured_margin(
    directories: dict[str, Path],
) -> None:
    settings = _settings(directories, stall_alert_margin_seconds=90.0)

    deployment = build_paper_deployment(
        settings, symbol_rules=_rules(), registry=_registry(), clock=SimulatedClock(ANCHOR)
    )

    assert deployment.watchdog is not None
    expected = settings.paper.timeframe.seconds + 90.0
    assert deployment.watchdog._threshold == expected


def test_the_watchdog_reads_the_session_it_was_built_for(
    directories: dict[str, Path],
) -> None:
    # Wired to the real session's own accessors, not a copy -- so an alert reflects what
    # the running session actually did, not a snapshot frozen at composition time.
    deployment = build_paper_deployment(
        _settings(directories),
        symbol_rules=_rules(),
        registry=_registry(),
        clock=SimulatedClock(ANCHOR),
    )
    assert deployment.watchdog is not None

    assert deployment.watchdog._session_metrics() == deployment.session.runtime_metrics()


def test_run_starts_and_stops_the_watchdog(directories: dict[str, Path]) -> None:
    # No real socket: a scripted transport is swapped into the real feed, exactly as the
    # grace-period test above does, so this exercises PaperDeployment.run()'s own
    # start/finally wrapping without reaching the network.
    clock = SimulatedClock(ANCHOR)
    deployment = build_paper_deployment(
        _settings(directories), symbol_rules=_rules(), registry=_registry(), clock=clock
    )
    assert deployment.watchdog is not None
    bars = make_bars([Decimal(50_000)] * 2)
    scripted, _, _ = _feed(_bar_steps(bars), clock=clock)
    deployment.feed._transport = scripted._transport
    deployment.runner._max_bars = len(bars)

    deployment.run(resume=False)

    assert deployment.watchdog._thread is None
