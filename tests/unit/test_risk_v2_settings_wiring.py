"""Risk V2 reachable from `.env`, without disturbing V1 when it is not configured.

`_risk_configuration()` is the one place paper trading turns `RiskSettings` into the engine's
`RiskConfiguration`. Before this, none of the M7/M8 fields existed on `RiskSettings` at all —
a paper session could never run with a stop, a trailing rule, a take-profit or a circuit
breaker no matter what `.env` said, because there was nothing in `.env` to say it with.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.config.settings import load_settings
from quantplatform.core.models.execution_policy import ExecutionPolicy
from quantplatform.orchestration.paper import _risk_configuration


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy()


def test_risk_settings_default_to_no_v2_configuration() -> None:
    settings = load_settings()
    risk = settings.risk

    assert risk.risk_per_trade_pct is None
    assert risk.max_position_exposure_pct is None
    assert risk.min_stop_distance_bps is None
    assert risk.max_stop_distance_bps is None
    assert risk.initial_stop_distance_bps is None
    assert risk.trailing_activation_bps is None
    assert risk.trailing_distance_bps is None
    assert risk.break_even_activation_bps is None
    assert risk.take_profit_distance_bps is None
    assert risk.max_holding_bars is None
    assert risk.max_daily_loss_pct is None
    assert risk.max_consecutive_losses is None
    assert risk.latch_total_drawdown is False


def test_unconfigured_v2_produces_v1_risk_configuration() -> None:
    settings = load_settings()

    config = _risk_configuration(settings, _policy())

    assert config.risk_v2_active is False
    assert config.stop_required is False
    assert config.risk_budget is None
    assert config.initial_stop_distance_bps is None
    assert config.trailing_activation_bps is None
    assert config.take_profit_distance_bps is None
    assert config.max_holding_bars is None
    assert config.max_daily_loss_pct is None
    assert config.max_consecutive_losses is None
    assert config.latch_total_drawdown is False


def test_a_partial_risk_budget_from_env_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three of the four budget fields, deliberately missing one — a partial budget from
    # .env is exactly as ambiguous as a partial one constructed in code, and refused the
    # same way: silently defaulting the fourth would invent a number nobody configured.
    monkeypatch.setenv("QP_RISK__RISK_PER_TRADE_PCT", "0.01")
    monkeypatch.setenv("QP_RISK__MAX_POSITION_EXPOSURE_PCT", "0.5")
    monkeypatch.setenv("QP_RISK__MIN_STOP_DISTANCE_BPS", "50")
    # max_stop_distance_bps intentionally not set

    with pytest.raises(
        Exception,
        match=r"risk_per_trade_pct.*max_position_exposure_pct.*"
        r"min_stop_distance_bps.*max_stop_distance_bps|all four",
    ):
        load_settings()


def test_the_full_v2_configuration_is_reachable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QP_RISK__RISK_PER_TRADE_PCT", "0.01")
    monkeypatch.setenv("QP_RISK__MAX_POSITION_EXPOSURE_PCT", "0.5")
    monkeypatch.setenv("QP_RISK__MIN_STOP_DISTANCE_BPS", "50")
    monkeypatch.setenv("QP_RISK__MAX_STOP_DISTANCE_BPS", "1000")
    monkeypatch.setenv("QP_RISK__INITIAL_STOP_DISTANCE_BPS", "300")
    monkeypatch.setenv("QP_RISK__TRAILING_ACTIVATION_BPS", "300")
    monkeypatch.setenv("QP_RISK__TRAILING_DISTANCE_BPS", "200")
    monkeypatch.setenv("QP_RISK__BREAK_EVEN_ACTIVATION_BPS", "150")
    monkeypatch.setenv("QP_RISK__TAKE_PROFIT_DISTANCE_BPS", "600")
    monkeypatch.setenv("QP_RISK__MAX_HOLDING_BARS", "168")
    monkeypatch.setenv("QP_RISK__MAX_DAILY_LOSS_PCT", "0.03")
    monkeypatch.setenv("QP_RISK__MAX_CONSECUTIVE_LOSSES", "5")
    monkeypatch.setenv("QP_RISK__LATCH_TOTAL_DRAWDOWN", "true")

    settings = load_settings()
    config = _risk_configuration(settings, _policy())

    assert config.risk_v2_active is True
    assert config.stop_required is True
    assert config.risk_budget is not None
    assert config.risk_budget.risk_per_trade_pct == Decimal("0.01")
    assert config.risk_budget.max_position_exposure_pct == Decimal("0.5")
    assert config.risk_budget.min_stop_distance_bps == Decimal(50)
    assert config.risk_budget.max_stop_distance_bps == Decimal(1000)
    assert config.initial_stop_distance_bps == Decimal(300)
    assert config.trailing_activation_bps == Decimal(300)
    assert config.trailing_distance_bps == Decimal(200)
    assert config.break_even_activation_bps == Decimal(150)
    assert config.take_profit_distance_bps == Decimal(600)
    assert config.max_holding_bars == 168
    assert config.max_daily_loss_pct == Decimal("0.03")
    assert config.max_consecutive_losses == 5
    assert config.latch_total_drawdown is True
    # max_total_drawdown_pct is unchanged (0.20 default) and satisfies the latch precondition
    assert config.max_total_drawdown_pct == Decimal("0.20")


def test_a_risk_budget_without_a_stop_distance_is_refused_by_the_engines_own_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RiskSettings only refuses a *partial* budget; a complete budget with no stop distance
    # is a decision RiskConfiguration itself already refuses (M7a), and that refusal must
    # still surface here rather than being swallowed by the translation.
    monkeypatch.setenv("QP_RISK__RISK_PER_TRADE_PCT", "0.01")
    monkeypatch.setenv("QP_RISK__MAX_POSITION_EXPOSURE_PCT", "0.5")
    monkeypatch.setenv("QP_RISK__MIN_STOP_DISTANCE_BPS", "50")
    monkeypatch.setenv("QP_RISK__MAX_STOP_DISTANCE_BPS", "1000")
    # initial_stop_distance_bps intentionally not set

    settings = load_settings()
    with pytest.raises(ValueError, match="initial_stop_distance_bps"):
        _risk_configuration(settings, _policy())


def test_v1_only_settings_produce_the_same_config_as_before_this_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing about the existing V1 fields changed shape or default.
    monkeypatch.setenv("QP_RISK__MAX_DAILY_DRAWDOWN_FRACTION", "0.05")
    monkeypatch.setenv("QP_RISK__MAX_TOTAL_DRAWDOWN_FRACTION", "0.20")

    settings = load_settings()
    config = _risk_configuration(settings, _policy())

    assert config.max_daily_drawdown_pct == Decimal("0.05")
    assert config.max_total_drawdown_pct == Decimal("0.20")
    assert config.risk_v2_active is False
