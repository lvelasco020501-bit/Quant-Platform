"""Command line entry point.

Exposes the platform version, a configuration check that surfaces unsafe or incoherent
settings, and the ``data`` command group for historical market-data validation, ingestion
and inspection. No command ever prints a secret.
"""

from __future__ import annotations

import json

import typer

from quantplatform import __version__
from quantplatform.cli import data
from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.errors import QuantPlatformError

__all__ = ["app"]

app = typer.Typer(
    name="quantplatform",
    help="Quantitative trading platform administration commands.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(data.app)

_EXIT_CONFIGURATION_ERROR = 2


@app.command()
def version() -> None:
    """Print the installed platform version."""
    typer.echo(__version__)


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
