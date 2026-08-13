"""Structured logging, secret redaction and the phase-1 command line surface."""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from quantplatform.cli.main import app
from quantplatform.core.constants import REDACTED_PLACEHOLDER
from quantplatform.core.enums import LogFormat
from quantplatform.core.errors import ConfigurationError, DataGapError, QuantPlatformError
from quantplatform.core.logging_config import configure_logging, get_logger, log_context


@pytest.fixture
def stream() -> io.StringIO:
    """Return a buffer that captures the configured handler's output."""
    return io.StringIO()


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    """Restore the root logger after each test."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)


def _emitted(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse the JSON records written to a stream."""
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_records_are_emitted_as_json(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    get_logger("test").info("hello")

    records = _emitted(stream)
    assert len(records) == 1
    assert records[0]["message"] == "hello"
    assert records[0]["level"] == "INFO"
    assert records[0]["logger"] == "quantplatform.test"
    assert records[0]["timestamp"].endswith("+00:00")


def test_context_binding_is_attached_and_restored(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    logger = get_logger("test")

    with log_context(correlation_id="abc", strategy_id="ema_trend"):
        logger.info("inside")
        with log_context(strategy_id="breakout"):
            logger.info("nested")
    logger.info("outside")

    records = _emitted(stream)
    assert records[0]["context"] == {"correlation_id": "abc", "strategy_id": "ema_trend"}
    assert records[1]["context"] == {"correlation_id": "abc", "strategy_id": "breakout"}
    assert "context" not in records[2]


def test_structured_extras_are_serialised_exactly(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    get_logger("test").info("order", extra={"quantity": Decimal("0.10000001")})

    record = _emitted(stream)[0]
    assert record["extra"]["quantity"] == "0.10000001"


def test_known_secret_values_are_masked_in_messages(stream: io.StringIO) -> None:
    secret = "super-secret-api-key"
    configure_logging(
        level="INFO",
        log_format=LogFormat.JSON,
        secrets=(secret,),
        stream=stream,
    )
    get_logger("test").info("connecting with %s", secret)

    payload = stream.getvalue()
    assert secret not in payload
    assert REDACTED_PLACEHOLDER in payload


def test_short_secret_values_are_masked_by_exact_match(stream: io.StringIO) -> None:
    short_secret = "ab"
    configure_logging(
        level="INFO",
        log_format=LogFormat.JSON,
        secrets=(short_secret,),
        stream=stream,
    )
    get_logger("test").info("token", extra={"raw_value": short_secret})

    record = _emitted(stream)[0]
    assert record["extra"]["raw_value"] == REDACTED_PLACEHOLDER


def test_short_secret_values_are_not_scanned_as_a_substring(stream: io.StringIO) -> None:
    # A short secret must not be blanket-replaced inside unrelated text: doing so would
    # corrupt any ordinary word that happens to contain the same characters.
    short_secret = "ab"
    configure_logging(
        level="INFO",
        log_format=LogFormat.JSON,
        secrets=(short_secret,),
        stream=stream,
    )
    get_logger("test").info("cabbage")

    record = _emitted(stream)[0]
    assert record["message"] == "cabbage"


def test_sensitive_field_names_are_masked_even_when_unknown(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    get_logger("test").info(
        "auth",
        extra={
            "api_secret": "never-configured",
            "nested": {"authorization": "Bearer xyz", "symbol": "BTC/USDT"},
            "symbol": "BTC/USDT",
        },
    )

    record = _emitted(stream)[0]
    assert record["extra"]["api_secret"] == REDACTED_PLACEHOLDER
    assert record["extra"]["nested"]["authorization"] == REDACTED_PLACEHOLDER
    assert record["extra"]["nested"]["symbol"] == "BTC/USDT"
    assert record["extra"]["symbol"] == "BTC/USDT"


def test_exceptions_are_captured(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("test").exception("failed")

    record = _emitted(stream)[0]
    assert "ValueError: boom" in record["exception"]


def test_text_format_is_human_readable(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format=LogFormat.TEXT, stream=stream)
    with log_context(run_id="r-1"):
        get_logger("test").warning("degraded")

    output = stream.getvalue()
    assert "WARNING" in output
    assert "degraded" in output
    assert "run_id=r-1" in output


def test_reconfiguring_does_not_duplicate_handlers(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    get_logger("test").info("once")

    assert len(_emitted(stream)) == 1


# --- CLI -----------------------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Return a Typer test runner."""
    return CliRunner()


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run the CLI in a directory with no inherited platform configuration."""
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("_clean_env")
def test_version_command(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


@pytest.mark.usefixtures("_clean_env")
def test_check_config_reports_a_redacted_summary(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QP_EXCHANGE__API_KEY", "key-abcdef123456")
    monkeypatch.setenv("QP_EXCHANGE__API_SECRET", "secret-abcdef123456")

    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["execution_mode"] == "paper"
    assert summary["live_trading_armed"] is False
    assert summary["exchange_credentials_present"] is True
    assert "secret-abcdef123456" not in result.stdout
    assert "key-abcdef123456" not in result.stdout


@pytest.mark.usefixtures("_clean_env")
def test_check_config_fails_on_unsafe_configuration(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QP_EXECUTION_MODE", "live")

    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 2
    assert "live_trading_not_authorized" in result.stderr


# --- QuantPlatformError.log_extra() ------------------------------------------------------------
#
# The incident this section pins: a DataGapError, correctly raised by the market-data feed
# on a genuine gap, reached the CLI's top-level handler, which tried to report it via
# `_LOGGER.error("paper session failed", extra=exc.to_dict())`. `to_dict()`'s top-level
# "message" key collides with the reserved attribute stdlib `logging` sets on every record
# it builds, and `Logger.makeRecord` raises `KeyError` rather than silently overwriting it.
# The process died on the handler meant to report why it died, and the original error was
# never logged at all. `log_extra()` is the fix; these tests hold it to the incident exactly.


def test_to_dict_still_collides_with_the_reserved_message_attribute(
    stream: io.StringIO,
) -> None:
    # Pinned deliberately, not as the desired behaviour but as the documented hazard that
    # makes log_extra() necessary. If a future stdlib release ever stopped raising here,
    # this test would fail and the rationale in log_extra()'s docstring would need revisiting
    # -- it would not mean the fix could be removed, since every other Python version this
    # platform runs on still raises.
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    exc = DataGapError("2 missing 1h candle(s) for BTC/USDT", feed="binance_spot_ws")

    with pytest.raises(KeyError, match="message"):
        get_logger("test").error("paper session failed", extra=exc.to_dict())


def test_log_extra_never_collides_for_any_registered_error(stream: io.StringIO) -> None:
    # Every QuantPlatformError subclass in the platform, not just the one that crashed.
    # The fix has to hold for errors nobody has hit yet, which is exactly why it nests
    # unconditionally instead of special-casing "message".
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    logger = get_logger("test")
    subclasses = _all_error_subclasses(QuantPlatformError)
    assert len(subclasses) > 30, "sanity check: the error hierarchy should be well populated"

    for error_type in subclasses:
        exc = error_type("boundary condition", symbol="BTC/USDT", observed=1, limit=2)
        logger.error("failed", extra=exc.log_extra())  # must not raise for any subclass


def test_log_extra_reaches_the_gap_error_that_crashed_the_process(
    stream: io.StringIO,
) -> None:
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    exc = DataGapError(
        "2 missing 1h candle(s) for BTC/USDT: expected 2026-08-13T02:00:00+00:00, "
        "received 2026-08-13T04:00:00+00:00",
        feed="binance_spot_ws",
        missing_bars=2,
    )

    get_logger("test").error("paper session failed", extra=exc.log_extra())

    record = _emitted(stream)[0]
    assert record["message"] == "paper session failed"
    reported = record["extra"]["error"]
    assert reported["code"] == "data_gap_error"
    assert "2 missing 1h candle(s)" in reported["message"]
    assert reported["details"] == {"feed": "binance_spot_ws", "missing_bars": 2}


def test_log_extra_preserves_every_field_to_dict_carries() -> None:
    exc = ConfigurationError("bad config", setting="risk.max_open_orders", value=-1)

    assert exc.log_extra() == {"error": exc.to_dict()}
    assert exc.log_extra()["error"]["details"] == {
        "setting": "risk.max_open_orders",
        "value": -1,
    }


def test_log_extra_key_is_never_a_reserved_record_attribute() -> None:
    # The whole guarantee in one assertion: whatever `log_extra()` returns must be safe to
    # merge into a LogRecord's __dict__ unconditionally.
    exc = ConfigurationError("bad config")

    assert set(exc.log_extra()) == {"error"}
    assert "error" not in logging.makeLogRecord({}).__dict__


def _all_error_subclasses(base: type[Exception]) -> list[type[Exception]]:
    """Return every QuantPlatformError subclass currently registered, recursively."""
    found: list[type[Exception]] = []
    for subclass in base.__subclasses__():
        found.append(subclass)
        found.extend(_all_error_subclasses(subclass))
    return found
