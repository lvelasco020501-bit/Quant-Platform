"""The 4h risk configuration the B2 paper session would run, held to what research measured.

B2's evidence was produced under a risk configuration that has never been deployed: production
runs Risk V2 at 1h, and M15 declared a conversion to 4h that M22, M23 and M24 then used for
every run. A paper session at 4h must run that conversion or it is not running the thing that
was validated.

``deploy/paper-b2-btc-4h.env`` is not hand-written; its numbers were generated from
:func:`risk_for_timeframe`. The load-bearing test here does not compare strings: it lets the
platform build its own :class:`RiskConfiguration` from the file, exactly as
``orchestration.paper`` does at startup, and compares that object **field by field** with the
conversion research used.

That distinction earned its keep. The first version of the file was written with
``RiskConfiguration``'s field names, and settings expose ``RiskSettings``, whose names differ —
``max_volatility`` is ``max_realized_volatility_fraction``, the risk budget is flat rather than
nested, and ``market_buy_buffer_bps`` is not exposed at all. A string comparison would have
passed something the platform refuses to load.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.config.settings import Settings
from quantplatform.core.enums import ExecutionMode, Timeframe
from quantplatform.orchestration.paper import _execution_policy, _risk_configuration
from quantplatform.research.m15 import risk_for_timeframe
from quantplatform.research.m22 import _base
from quantplatform.research.m23 import CANDIDATE
from quantplatform.risk.config import RiskConfiguration
from quantplatform.strategies.registry import build_default_registry

ENV_FILE = Path(__file__).resolve().parents[2] / "deploy" / "paper-b2-btc-4h.env"

COMPARED: tuple[str, ...] = (
    "initial_stop_distance_bps",
    "take_profit_distance_bps",
    "break_even_activation_bps",
    "max_holding_bars",
    "max_volatility",
    "max_total_drawdown_pct",
    "max_daily_drawdown_pct",
    "max_daily_loss_pct",
    "max_consecutive_losses",
    "latch_total_drawdown",
    "max_spread_bps",
    "max_open_positions",
    "max_open_orders",
    "max_orders_per_day",
    "max_orders_per_hour",
    "max_portfolio_exposure_pct",
)
"""Everything the settings layer exposes and research pinned. ``market_buy_buffer_bps`` and
``trailing_*`` are absent on purpose: settings do not expose the first, and research never set
the second, so a test asserting either would be inventing a requirement."""


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(_env_file=str(ENV_FILE))


@pytest.fixture(scope="module")
def built(settings: Settings) -> RiskConfiguration:
    """Return the risk configuration the platform itself builds from this file."""
    return _risk_configuration(settings, _execution_policy(settings))


@pytest.fixture(scope="module")
def validated() -> RiskConfiguration:
    """Return the configuration every M22, M23 and M24 run was measured under."""
    return risk_for_timeframe(_base().risk, Timeframe.H4)


# --- The numbers are the converted ones, not new ones -------------------------------------------


@pytest.mark.parametrize("field", COMPARED)
def test_the_built_configuration_matches_what_research_measured(
    field: str, built: RiskConfiguration, validated: RiskConfiguration
) -> None:
    assert getattr(built, field) == getattr(validated, field), field


def test_the_risk_budget_matches_too(
    built: RiskConfiguration, validated: RiskConfiguration
) -> None:
    assert built.risk_budget is not None
    assert validated.risk_budget is not None
    for field in (
        "risk_per_trade_pct",
        "max_position_exposure_pct",
        "min_stop_distance_bps",
        "max_stop_distance_bps",
    ):
        assert getattr(built.risk_budget, field) == getattr(validated.risk_budget, field), field


def test_the_fraction_of_equity_risked_did_not_scale_with_the_bar(
    built: RiskConfiguration,
) -> None:
    # A fraction of equity is not a price distance. Had it scaled with the bar, every position
    # would be twice the size the evidence was produced with.
    assert built.risk_budget is not None
    assert built.risk_budget.risk_per_trade_pct == Decimal("0.01")


def test_the_time_stop_is_seven_days_expressed_in_4h_bars(built: RiskConfiguration) -> None:
    assert built.max_holding_bars == 42
    assert built.max_holding_bars * 4 == 168


def test_the_latching_breakers_are_left_latching(built: RiskConfiguration) -> None:
    # The research variant released these to measure the rule on its own. A paper session is
    # not that experiment: it runs what production would run.
    assert built.latch_total_drawdown is True
    assert built.max_consecutive_losses == 5


def test_the_costs_are_the_ones_the_backtests_paid(settings: Settings) -> None:
    policy = _execution_policy(settings)
    assert policy.fee.basis_points == Decimal(10)
    assert policy.slippage.basis_points == Decimal(5)


# --- The session is the one that was validated --------------------------------------------------


def test_the_strategy_and_parameters_are_exactly_m23_s(settings: Settings) -> None:
    assert settings.paper.strategy_id == CANDIDATE.candidate.strategy_id
    assert settings.paper.strategy_params == dict(CANDIDATE.candidate.params)
    assert settings.paper.strategy_params == {
        "entry_lookback": "40",
        "exit_lookback": "20",
        "trend_period": "400",
    }


def test_the_market_and_timeframe_are_the_validated_ones(settings: Settings) -> None:
    assert settings.market.symbol == "BTC/USDT"
    assert settings.market.timeframe is Timeframe.H4
    assert settings.paper.timeframe is Timeframe.H4
    assert settings.paper.symbols == ("BTC/USDT",)


def test_the_symbol_is_inside_the_allow_list_the_validator_checks(settings: Settings) -> None:
    assert settings.market.symbol in settings.risk.allowed_symbols
    assert "ETH/USDT" not in settings.risk.allowed_symbols, "this session is BTC only"


def test_the_strategy_is_one_paper_may_actually_resolve(settings: Settings) -> None:
    assert settings.paper.strategy_id in build_default_registry()


# --- Paper only, and isolated -------------------------------------------------------------------


def test_the_session_is_paper_and_live_is_disabled(settings: Settings) -> None:
    assert settings.execution_mode is ExecutionMode.PAPER
    assert settings.live_trading_enabled is False


def test_the_session_has_directories_of_its_own(settings: Settings) -> None:
    # SessionLock holds one session per state directory. Sharing var/state/ with anything else
    # is how two live processes once spent eighteen hours writing over each other.
    paper = settings.paper
    for label, directory in (
        ("state", paper.state_directory),
        ("logs", paper.log_directory),
        ("reports", paper.reports_directory),
    ):
        path = str(directory)
        assert paper.session_id in path, f"{label} must be scoped to this session, got {path}"
        assert not path.startswith("var/state"), f"{label} must not share the old shared tree"


def test_the_session_id_names_the_strategy_market_and_timeframe(settings: Settings) -> None:
    assert settings.paper.session_id == "paper-b2-btc-4h-w1"


def test_the_capital_is_the_one_every_backtest_used(settings: Settings) -> None:
    assert settings.backtest.initial_capital == Decimal(10_000)
