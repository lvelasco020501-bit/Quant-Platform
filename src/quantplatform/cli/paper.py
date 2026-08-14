"""Paper trading command line surface.

Two commands, and the split between them is deliberate. ``check`` performs every startup
validation and exits — an operator can prove a deployment is sound without opening a socket
or touching an account. ``run`` performs the same validations and then starts the session.

Nothing here decides anything about trading. Options override configuration, configuration
is validated, and the composition root does the wiring. No command ever prints a secret.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.clock import SystemClock
from quantplatform.core.errors import QuantPlatformError
from quantplatform.core.logging_config import get_logger
from quantplatform.core.models.market import SymbolRules
from quantplatform.marketdata.symbol_rules import BinanceSpotSymbolRulesProvider
from quantplatform.orchestration.logging_setup import close_file_logging, configure_file_logging
from quantplatform.orchestration.paper import (
    PaperDeployment,
    build_paper_deployment,
    symbol_rules_freshness_budget,
    validate_startup,
)
from quantplatform.orchestration.shutdown import ShutdownSignal, shutdown_on_signals
from quantplatform.storage.session_lock import SessionLock
from quantplatform.strategies.registry import build_default_registry

__all__ = ["app"]

app = typer.Typer(
    name="paper",
    help="Continuous paper trading against a live market-data feed.",
    no_args_is_help=True,
)

EXIT_CONFIGURATION_ERROR = 2
EXIT_RUNTIME_ERROR = 1

_LOGGER = get_logger(__name__)

_SymbolsOption = Annotated[
    list[str] | None,
    typer.Option("--symbol", help="Canonical symbol; repeat for several. Overrides settings."),
]
_TimeframeOption = Annotated[
    str | None, typer.Option("--timeframe", help="Bar interval, e.g. 1h. Overrides settings.")
]
_ReportsOption = Annotated[
    Path | None, typer.Option("--reports-dir", help="Where daily reports are written.")
]
_StateOption = Annotated[
    Path | None, typer.Option("--state-dir", help="Where session state is persisted.")
]
_LogsOption = Annotated[Path | None, typer.Option("--log-dir", help="Where log files are written.")]
_SessionOption = Annotated[
    str | None, typer.Option("--session-id", help="Session identity; also the state file name.")
]
_StrategyOption = Annotated[
    str | None, typer.Option("--strategy", help="Registered strategy identifier.")
]
_MaxBarsOption = Annotated[
    int | None, typer.Option("--max-bars", help="Stop after this many received bars.")
]
_ResumeOption = Annotated[
    bool, typer.Option("--resume/--fresh", help="Restore persisted state instead of starting new.")
]


def _overrides(
    *,
    symbols: list[str] | None,
    timeframe: str | None,
    reports_dir: Path | None,
    state_dir: Path | None,
    log_dir: Path | None,
    session_id: str | None,
    strategy: str | None,
    max_bars: int | None,
) -> dict[str, Any]:
    """Collect the command-line overrides that were actually supplied.

    An option left unset is absent from the result rather than present as ``None``, so
    configuration keeps its own value instead of being overwritten with nothing.
    """
    supplied: dict[str, Any] = {}
    if symbols:
        supplied["symbols"] = tuple(symbols)
    if timeframe is not None:
        supplied["timeframe"] = timeframe
    if reports_dir is not None:
        supplied["reports_directory"] = reports_dir
    if state_dir is not None:
        supplied["state_directory"] = state_dir
    if log_dir is not None:
        supplied["log_directory"] = log_dir
    if session_id is not None:
        supplied["session_id"] = session_id
    if strategy is not None:
        supplied["strategy_id"] = strategy
    if max_bars is not None:
        supplied["max_bars"] = max_bars
    return supplied


def _settings_with(overrides: dict[str, Any]) -> Settings:
    """Load configuration and apply command-line overrides to the paper section."""
    settings = load_settings()
    if not overrides:
        return settings
    merged = settings.paper.model_dump() | overrides
    return load_settings(paper=merged)


def _rules_provider() -> BinanceSpotSymbolRulesProvider:
    """Build the read-only source of venue trading rules.

    One provider for the whole process, so the startup fetch and every later refresh read
    the same endpoint through the same validated configuration. Building a second one for
    the refresh loop would be a second place for the endpoint to be wrong.
    """
    return BinanceSpotSymbolRulesProvider(clock=SystemClock())


def _symbol_rules(
    settings: Settings, provider: BinanceSpotSymbolRulesProvider | None = None
) -> dict[str, SymbolRules]:
    """Fetch the venue's real trading rules for every configured instrument.

    Read-only public metadata: no key, no signature, no account. A failure here stops
    startup rather than falling back to a default, because a default tick size is a wrong
    tick size and an order sized against one is a wrong order.

    Raises:
        DataProviderError: If the venue metadata cannot be fetched or parsed.
        MarketDataSubscriptionError: If a configured symbol is absent or not spot-tradable.
        DataIntegrityError: If a symbol's filters are missing or unparseable.
    """
    resolved = provider if provider is not None else _rules_provider()
    return dict(resolved.fetch(settings.paper.symbols))


@app.command("check")
def check(
    symbols: _SymbolsOption = None,
    timeframe: _TimeframeOption = None,
    reports_dir: _ReportsOption = None,
    state_dir: _StateOption = None,
    log_dir: _LogsOption = None,
    session_id: _SessionOption = None,
    strategy: _StrategyOption = None,
) -> None:
    """Run every startup validation and exit without opening a socket.

    Raises:
        typer.Exit: With a non-zero code when the deployment would not start.
    """
    try:
        settings = _settings_with(
            _overrides(
                symbols=symbols,
                timeframe=timeframe,
                reports_dir=reports_dir,
                state_dir=state_dir,
                log_dir=log_dir,
                session_id=session_id,
                strategy=strategy,
                max_bars=None,
            )
        )
        rules = _symbol_rules(settings)
        validate_startup(settings, symbol_rules=rules, registry=build_default_registry())
    except QuantPlatformError as exc:
        typer.echo(json.dumps(exc.to_dict(), default=str, indent=2), err=True)
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc
    except ValueError as exc:
        typer.echo(json.dumps({"code": "invalid_configuration", "message": str(exc)}), err=True)
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc

    typer.echo("READY_FOR_PAPER_RUN")
    typer.echo(json.dumps(_summarise(settings, symbol_rules=rules), indent=2, default=str))


@app.command("run")
def run(  # noqa: PLR0913, PLR0917 - typer derives the CLI from this signature
    symbols: _SymbolsOption = None,
    timeframe: _TimeframeOption = None,
    reports_dir: _ReportsOption = None,
    state_dir: _StateOption = None,
    log_dir: _LogsOption = None,
    session_id: _SessionOption = None,
    strategy: _StrategyOption = None,
    max_bars: _MaxBarsOption = None,
    resume: _ResumeOption = False,  # noqa: FBT002 - a CLI flag needs its default here
) -> None:
    """Start a paper trading session and run until stopped.

    Stops cleanly on ``SIGINT`` or ``SIGTERM``: the feed and runner wind down at their next
    boundary, the session persists a final snapshot, the socket closes and logs are flushed.

    Raises:
        typer.Exit: With a non-zero code when startup or the run itself fails.
    """
    overrides = _overrides(
        symbols=symbols,
        timeframe=timeframe,
        reports_dir=reports_dir,
        state_dir=state_dir,
        log_dir=log_dir,
        session_id=session_id,
        strategy=strategy,
        max_bars=max_bars,
    )
    lock: SessionLock | None = None
    try:
        settings = _settings_with(overrides)
        log_paths = configure_file_logging(
            directory=settings.paper.log_directory,
            level=settings.log_level,
            log_format=settings.log_format,
            secrets=settings.secret_values(),
        )
        # Claimed before anything expensive is built, and before a socket is opened: a
        # second session must be refused at the cheapest possible moment, not after it has
        # already fetched venue rules and connected. Two sessions sharing one state
        # directory interleave their logs and collide on daily reports — an eighteen-hour
        # incident, not a hypothetical.
        lock = SessionLock(
            directory=settings.paper.state_directory, session_id=settings.paper.session_id
        )
        lock.acquire(now=SystemClock().now())
        # The same provider seeds the store and keeps it current. Rules fetched once at
        # startup expire against the risk engine's freshness budget partway through a
        # multi-day run, and every intent is refused from that point on.
        provider = _rules_provider()
        deployment = build_paper_deployment(
            settings,
            symbol_rules=_symbol_rules(settings, provider),
            registry=build_default_registry(),
            rules_provider=provider,
            log_paths=log_paths,
        )
    except QuantPlatformError as exc:
        typer.echo(json.dumps(exc.to_dict(), default=str, indent=2), err=True)
        _release_lock(lock)
        close_file_logging()
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc
    except ValueError as exc:
        typer.echo(json.dumps({"code": "invalid_configuration", "message": str(exc)}), err=True)
        _release_lock(lock)
        close_file_logging()
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc

    try:
        _drive(deployment, resume=resume)
    except QuantPlatformError as exc:
        _LOGGER.error("paper session failed", extra=exc.log_extra())
        typer.echo(json.dumps(exc.to_dict(), default=str, indent=2), err=True)
        raise typer.Exit(code=EXIT_RUNTIME_ERROR) from exc
    finally:
        _release_lock(lock)
        close_file_logging()


def _release_lock(lock: SessionLock | None) -> None:
    """Release the session lock if one was ever claimed.

    ``lock`` is ``None`` when startup failed before the claim — a bad configuration, an
    unusable directory — and releasing nothing is the correct response to that. Releasing
    is never allowed to raise, so a shutdown path cannot fail during shutdown.
    """
    if lock is not None:
        lock.release()


def _drive(deployment: PaperDeployment, *, resume: bool) -> None:
    """Run the deployment under signal-driven shutdown."""
    flag = ShutdownSignal(on_request=deployment.request_stop)
    with shutdown_on_signals(flag):
        result = deployment.run(resume=resume)
    typer.echo(
        json.dumps(_outcome(deployment, result_bars=result.runtime.bars_processed), indent=2)
    )


def _outcome(deployment: PaperDeployment, *, result_bars: int) -> dict[str, object]:
    """Summarise how the run went, for the operator's terminal."""
    return {
        "session_id": deployment.session.session_id,
        "bars_processed": result_bars,
        "reports_written": len(deployment.recorder.reports),
        "report_failures": deployment.recorder.failures,
        "feed_state": deployment.feed.state.value,
        "reports_directory": str(deployment.writer.config.output_directory),
    }


def _summarise(
    settings: Settings, *, symbol_rules: Mapping[str, SymbolRules] | None = None
) -> dict[str, object]:
    """Build a summary of the deployment that contains no secret material."""
    paper = settings.paper
    rules = symbol_rules or {}
    return {
        "status": "READY_FOR_PAPER_RUN" if rules else "NOT_READY",
        "strategy": paper.strategy_id,
        "symbol_rules": {
            symbol: {
                "price_tick": str(rule.price_tick),
                "quantity_step": str(rule.quantity_step),
                "min_quantity": str(rule.min_quantity),
                "max_quantity": None if rule.max_quantity is None else str(rule.max_quantity),
                "min_notional": str(rule.min_notional),
                "max_notional": None if rule.max_notional is None else str(rule.max_notional),
                "source": rule.source,
                "fetched_at": rule.updated_at.isoformat(),
            }
            for symbol, rule in sorted(rules.items())
        },
        "symbol_rules_refresh": {
            "interval_seconds": paper.symbol_rules_refresh_seconds,
            "stale_after_seconds": symbol_rules_freshness_budget(settings),
            "margin_refreshes": (
                symbol_rules_freshness_budget(settings) / paper.symbol_rules_refresh_seconds
            ),
        },
        "telemetry_enabled": True,
        "persistence_enabled": True,
        "close_grace_seconds": paper.close_grace_seconds,
        "session_id": paper.session_id,
        "strategy_id": paper.strategy_id,
        "symbols": list(paper.symbols),
        "timeframe": paper.timeframe.value,
        "execution_mode": settings.execution_mode.value,
        "live_trading_armed": settings.live_trading_armed,
        "websocket_url": paper.websocket_url,
        "heartbeat_timeout_seconds": paper.heartbeat_timeout_seconds,
        "reconnect": {
            "initial_delay_seconds": paper.reconnect_initial_delay_seconds,
            "max_delay_seconds": paper.reconnect_max_delay_seconds,
            "multiplier": paper.reconnect_backoff_multiplier,
            "max_attempts": paper.max_reconnect_attempts,
        },
        "directories": {
            "reports": str(paper.reports_directory),
            "state": str(paper.state_directory),
            "logs": str(paper.log_directory),
        },
    }
