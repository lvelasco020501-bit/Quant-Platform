"""The ``quantplatform data`` command group against a real database."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from typer.testing import CliRunner

from quantplatform.cli.main import app
from tests.data_helpers import fixture
from tests.integration.conftest import TEST_DSN_ENV_VAR

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_CONFIGURATION_ERROR = 2


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at a migrated throwaway database with a fixed historical boundary."""
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)

    dsn = os.environ.get(TEST_DSN_ENV_VAR) or f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("QP_DATABASE__DSN", dsn)
    # Judge the fixtures against the moment they were captured, so a deliberately
    # historical dataset is neither treated as open nor reported as stale.
    monkeypatch.setenv("QP_DATA__HISTORICAL_BACKFILL_END", "2026-01-01T05:00:00+00:00")
    monkeypatch.chdir(PROJECT_ROOT)

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield
    finally:
        command.downgrade(config, "base")


@pytest.fixture
def runner() -> CliRunner:
    """Return a Typer test runner."""
    return CliRunner()


@pytest.mark.usefixtures("cli_env")
def test_validate_reports_success_without_persisting(runner: CliRunner) -> None:
    result = runner.invoke(app, ["data", "validate", "--file", str(fixture("valid.csv"))])
    assert result.exit_code == EXIT_OK

    summary = json.loads(result.stdout)
    assert summary["persisted"] is False
    assert summary["rows"]["valid"] == 4
    assert summary["bars"]["inserted"] == 0

    inspected = runner.invoke(app, ["data", "inspect"])
    assert json.loads(inspected.stdout)["bar_count"] == 0


@pytest.mark.usefixtures("cli_env")
def test_ingest_persists_and_inspect_reports_it(runner: CliRunner) -> None:
    ingested = runner.invoke(app, ["data", "ingest", "--file", str(fixture("valid.csv"))])
    assert ingested.exit_code == EXIT_OK
    assert json.loads(ingested.stdout)["bars"]["inserted"] == 4

    inspected = runner.invoke(app, ["data", "inspect"])
    assert inspected.exit_code == EXIT_OK
    summary = json.loads(inspected.stdout)
    assert summary["bar_count"] == 4
    assert summary["first_open_time"] == "2026-01-01T00:00:00+00:00"
    assert summary["last_open_time"] == "2026-01-01T03:00:00+00:00"
    assert summary["missing_bars"] == 0


@pytest.mark.usefixtures("cli_env")
def test_inspect_summarises_gaps(runner: CliRunner) -> None:
    runner.invoke(app, ["data", "ingest", "--file", str(fixture("missing_interval.csv"))])

    summary = json.loads(runner.invoke(app, ["data", "inspect"]).stdout)
    assert summary["bar_count"] == 2
    assert summary["gap_runs"] == 1
    assert summary["missing_bars"] == 2


@pytest.mark.usefixtures("cli_env")
def test_repeated_ingest_is_idempotent(runner: CliRunner) -> None:
    runner.invoke(app, ["data", "ingest", "--file", str(fixture("valid.csv"))])
    second = runner.invoke(app, ["data", "ingest", "--file", str(fixture("valid.csv"))])

    assert json.loads(second.stdout)["bars"]["exact_duplicates"] == 4
    assert json.loads(runner.invoke(app, ["data", "inspect"]).stdout)["bar_count"] == 4


@pytest.mark.usefixtures("cli_env")
def test_fatal_file_exits_non_zero_and_persists_nothing(runner: CliRunner) -> None:
    result = runner.invoke(app, ["data", "ingest", "--file", str(fixture("missing_column.csv"))])
    assert result.exit_code == EXIT_FATAL

    assert json.loads(runner.invoke(app, ["data", "inspect"]).stdout)["bar_count"] == 0


@pytest.mark.usefixtures("cli_env")
def test_unsupported_timeframe_is_a_configuration_error(runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["data", "validate", "--file", str(fixture("valid.csv")), "--timeframe", "7z"]
    )
    assert result.exit_code == EXIT_CONFIGURATION_ERROR


@pytest.mark.usefixtures("cli_env")
def test_unknown_market_type_is_a_configuration_error(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["data", "validate", "--file", str(fixture("valid.csv")), "--market-type", "options"],
    )
    assert result.exit_code == EXIT_CONFIGURATION_ERROR


@pytest.mark.usefixtures("cli_env")
def test_no_command_output_leaks_the_database_dsn(runner: CliRunner) -> None:
    outputs = [
        runner.invoke(app, ["data", "ingest", "--file", str(fixture("valid.csv"))]).output,
        runner.invoke(app, ["data", "inspect"]).output,
        runner.invoke(app, ["data", "validate", "--file", str(fixture("valid.csv"))]).output,
    ]
    for output in outputs:
        assert "aiosqlite" not in output
        assert "password" not in output.lower()
        assert "quant:quant" not in output
