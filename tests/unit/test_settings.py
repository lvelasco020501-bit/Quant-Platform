"""Configuration defaults, coherence rules and the live-trading authorisation gate."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.constants import LIVE_CONFIRMATION_PHRASE
from quantplatform.core.enums import Environment, ExecutionMode, MarketType, Timeframe
from quantplatform.core.errors import ConfigurationError, LiveTradingNotAuthorizedError

_LIVE_ENV = {
    "QP_EXECUTION_MODE": "live",
    "QP_LIVE_TRADING_ENABLED": "true",
    "QP_LIVE_CONFIRMATION": LIVE_CONFIRMATION_PHRASE,
    "QP_ENVIRONMENT": "production",
    "QP_EXCHANGE__API_KEY": "key-abcdef123456",
    "QP_EXCHANGE__API_SECRET": "secret-abcdef123456",
}


@pytest.fixture(autouse=True)
def _isolated_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Run each test in a clean directory with no QP_ variables inherited."""
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_are_paper_and_disarmed() -> None:
    settings = load_settings()
    assert settings.execution_mode is ExecutionMode.PAPER
    assert settings.live_trading_enabled is False
    assert settings.live_trading_armed is False
    assert settings.environment is Environment.DEVELOPMENT


def test_default_market_is_btc_usdt_spot_hourly() -> None:
    settings = load_settings()
    assert settings.market.symbol == "BTC/USDT"
    assert settings.market.market_type is MarketType.SPOT
    assert settings.market.timeframe is Timeframe.H1


def test_default_risk_limits_are_spot_long_only_single_position() -> None:
    risk = load_settings().risk
    assert risk.allow_short is False
    assert risk.allow_leverage is False
    assert risk.max_leverage == Decimal(1)
    assert risk.max_open_positions == 1
    assert risk.allowed_market_types == (MarketType.SPOT,)


def test_nested_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QP_RISK__MAX_DAILY_ORDERS", "7")
    monkeypatch.setenv("QP_RISK__MAX_HOURLY_ORDERS", "2")
    risk = load_settings().risk
    assert risk.max_daily_orders == 7
    assert risk.max_hourly_orders == 2


def test_risk_fractions_are_decimal_not_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QP_RISK__MAX_DAILY_DRAWDOWN_FRACTION", "0.025")
    value = load_settings().risk.max_daily_drawdown_fraction
    assert isinstance(value, Decimal)
    assert value == Decimal("0.025")


def test_shorting_is_refused_on_a_spot_only_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QP_RISK__ALLOW_SHORT", "true")
    with pytest.raises(ValueError, match="shortable market type"):
        load_settings()


def test_leverage_is_refused_on_a_spot_only_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QP_RISK__ALLOW_LEVERAGE", "true")
    monkeypatch.setenv("QP_RISK__MAX_LEVERAGE", "3")
    with pytest.raises(ValueError, match="market type that supports it"):
        load_settings()


def test_traded_symbol_must_be_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QP_MARKET__SYMBOL", "ETH/USDT")
    monkeypatch.setenv("QP_MARKET__BASE_ASSET", "ETH")
    with pytest.raises(ConfigurationError, match="not in the allowed symbol list"):
        load_settings()


def test_enabling_live_without_live_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QP_LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ConfigurationError, match="must be false unless"):
        load_settings()


def test_live_mode_requires_the_enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("QP_LIVE_TRADING_ENABLED", "false")
    with pytest.raises(LiveTradingNotAuthorizedError, match="live_trading_enabled"):
        load_settings()


def test_live_mode_requires_the_exact_confirmation_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("QP_LIVE_CONFIRMATION", "i understand live trading risk")
    with pytest.raises(LiveTradingNotAuthorizedError, match="confirmation phrase"):
        load_settings()


def test_live_mode_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("QP_EXCHANGE__API_SECRET")
    with pytest.raises(LiveTradingNotAuthorizedError, match="credentials"):
        load_settings()


def test_live_mode_is_refused_from_a_development_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("QP_ENVIRONMENT", "development")
    with pytest.raises(LiveTradingNotAuthorizedError, match="development environment"):
        load_settings()


def test_fully_authorised_live_configuration_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    assert settings.live_trading_armed is True
    assert settings.execution_mode is ExecutionMode.LIVE


def test_secrets_are_masked_in_representations(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "secret-abcdef123456"
    monkeypatch.setenv("QP_EXCHANGE__API_KEY", "key-abcdef123456")
    monkeypatch.setenv("QP_EXCHANGE__API_SECRET", secret)
    settings = load_settings()

    assert secret not in repr(settings)
    assert secret not in str(settings)
    assert secret not in settings.model_dump_json()
    assert settings.exchange.api_secret is not None
    assert settings.exchange.api_secret.get_secret_value() == secret


def test_secret_values_are_exposed_only_for_log_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QP_EXCHANGE__API_KEY", "key-abcdef123456")
    monkeypatch.setenv("QP_EXCHANGE__API_SECRET", "secret-abcdef123456")
    settings = load_settings()
    values = settings.secret_values()
    assert "key-abcdef123456" in values
    assert "secret-abcdef123456" in values


def test_settings_are_frozen() -> None:
    settings = load_settings()
    with pytest.raises(ValueError, match="frozen"):
        settings.execution_mode = ExecutionMode.LIVE  # type: ignore[misc]


def test_unknown_configuration_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        Settings(unknown_field=1)  # type: ignore[call-arg]


def test_drawdown_limits_must_be_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QP_RISK__MAX_DAILY_DRAWDOWN_FRACTION", "0.30")
    monkeypatch.setenv("QP_RISK__MAX_TOTAL_DRAWDOWN_FRACTION", "0.10")
    with pytest.raises(ValueError, match="must not be below max_daily_drawdown_fraction"):
        load_settings()


def test_backtest_assumptions_are_recorded_as_decimals() -> None:
    backtest = load_settings().backtest
    assert isinstance(backtest.commission_basis_points, Decimal)
    assert isinstance(backtest.initial_capital, Decimal)
    assert backtest.random_seed == 42
