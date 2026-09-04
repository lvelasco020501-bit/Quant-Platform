"""Command line entry point.

Exposes the platform version, a configuration check that surfaces unsafe or incoherent
settings, and the ``data`` command group for historical market-data validation, ingestion
and inspection. No command ever prints a secret.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from quantplatform import __version__
from quantplatform.cli import data, paper, research
from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.errors import QuantPlatformError
from quantplatform.status import gather_status, render_status
from quantplatform.strategies.registry import build_default_registry

__all__ = ["app"]

app = typer.Typer(
    name="quantplatform",
    help="Quantitative trading platform administration commands.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(data.app)
app.add_typer(paper.app)
app.add_typer(research.app)

_EXIT_CONFIGURATION_ERROR = 2


@app.command()
def version() -> None:
    """Print the installed platform version."""
    typer.echo(__version__)


_SessionOption = Annotated[
    str | None,
    typer.Option("--session-id", help="Which session to describe. Defaults to the running one."),
]
_SmokeHoursOption = Annotated[
    float | None,
    typer.Option("--for-hours", help="Length of the run being tracked, to show progress."),
]
_ColourOption = Annotated[
    bool | None,
    typer.Option("--color/--no-color", help="Force colour on or off. Auto-detected by default."),
]


@app.command("status")
def status(
    session_id: _SessionOption = None,
    for_hours: _SmokeHoursOption = None,
    color: _ColourOption = None,
) -> None:
    """Show how the paper session is doing, in plain language.

    Strictly read-only. It opens the session's own files for reading and holds nothing that
    could start, stop, resume or alter a session — the ``status`` domain cannot import
    execution, risk, portfolio or paper, so that is a structural guarantee rather than a
    convention. Safe to run at any time, including while a session is live.

    Raises:
        typer.Exit: With a non-zero code when configuration cannot be loaded at all.
    """
    try:
        settings = load_settings()
    except QuantPlatformError as exc:
        typer.echo(json.dumps(exc.to_dict(), default=str, indent=2), err=True)
        raise typer.Exit(code=_EXIT_CONFIGURATION_ERROR) from exc
    except ValueError as exc:
        typer.echo(json.dumps({"code": "invalid_configuration", "message": str(exc)}), err=True)
        raise typer.Exit(code=_EXIT_CONFIGURATION_ERROR) from exc

    gathered = gather_status(settings, registry=build_default_registry(), session_id=session_id)
    typer.echo(render_status(gathered, colour=color, smoke_hours=for_hours))


@app.command("check-config")
def check_config() -> None:
    """Validate the effective configuration and print a redacted summary.

    Raises:
        typer.Exit: With a non-zero code when the configuration is invalid or unsafe.
    """
    try:
        settings = load_settings()
    except QuantPlatformError as exc:
        typer.echo(json.dumps(exc.to_dict(), default=str), err=True)
        raise typer.Exit(code=_EXIT_CONFIGURATION_ERROR) from exc

    typer.echo(json.dumps(_summarise(settings), indent=2, default=str))


def _summarise(settings: Settings) -> dict[str, object]:
    """Build a summary of the configuration that contains no secret material.

    Args:
        settings: Validated configuration.

    Returns:
        A JSON-serialisable summary safe to print.
    """
    return {
        "environment": settings.environment.value,
        "execution_mode": settings.execution_mode.value,
        "live_trading_armed": settings.live_trading_armed,
        "symbol": settings.market.symbol,
        "market_type": settings.market.market_type.value,
        "timeframe": settings.market.timeframe.value,
        "exchange": settings.exchange.name,
        "exchange_testnet": settings.exchange.testnet,
        "exchange_credentials_present": settings.exchange.has_credentials,
        "risk": {
            "max_open_positions": settings.risk.max_open_positions,
            "max_daily_orders": settings.risk.max_daily_orders,
            "max_hourly_orders": settings.risk.max_hourly_orders,
            "max_daily_drawdown_fraction": str(settings.risk.max_daily_drawdown_fraction),
            "max_total_drawdown_fraction": str(settings.risk.max_total_drawdown_fraction),
            "allow_short": settings.risk.allow_short,
            "allow_leverage": settings.risk.allow_leverage,
        },
    }


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
