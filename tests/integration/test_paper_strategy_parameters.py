"""Paper trading can carry typed strategy parameters, and ``check`` proves it can.

Two defects motivated this file, both found when a VPS smoke test refused to start.

``build_paper_deployment`` constructed every strategy as ``registry.create(id, {})`` — an
empty mapping, hardcoded, with no configuration path into it. ``ema_trend`` survived that
only because its periods default to 20/50; ``breakout`` declares no defaults on purpose, so
it could not be constructed at all.

Worse, ``validate_startup`` asked only whether the identifier was *in* the registry. A
membership test is not a construction, so ``paper check`` reported READY_FOR_PAPER_RUN for a
deployment that could never build its own strategy. The check has to fail wherever the run
would, which means both must construct through one resolver rather than two code paths that
agree by coincidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from quantplatform.cli.paper import _overrides, _strategy_params
from quantplatform.config.settings import Settings, load_settings
from quantplatform.core.errors import StrategyParameterError
from quantplatform.core.models.execution_policy import ExecutionPolicy
from quantplatform.core.models.market import SymbolRules
from quantplatform.orchestration import paper as paper_orchestration
from quantplatform.orchestration.paper import (
    _risk_configuration,
    build_paper_deployment,
    validate_startup,
)
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.breakout import BreakoutParameters, BreakoutStrategy
from quantplatform.strategies.ema_trend import EmaTrendParameters, EmaTrendStrategy
from quantplatform.strategies.registry import StrategyRegistry, build_default_registry
from tests.factories import SYMBOL, make_symbol_rules

FROZEN_BREAKOUT_PARAMS = {"entry_lookback": "20", "exit_lookback": "10"}
"""The operational pair frozen in M10b. Written as strings because that is what an
environment variable carries; the strategy's own schema is what turns them into integers."""


@pytest.fixture
def directories(tmp_path: Path) -> dict[str, Path]:
    return {
        "reports_directory": tmp_path / "reports",
        "state_directory": tmp_path / "state",
        "log_directory": tmp_path / "logs",
    }


def _settings(directories: dict[str, Path], **paper: object) -> Settings:
    """Settings pointed at a temporary tree, running a real registered strategy."""
    return load_settings(
        paper={
            "session_id": "hotfix-session",
            "strategy_id": "breakout",
            "symbols": (SYMBOL,),
            **{key: str(value) for key, value in directories.items()},
            **paper,
        }
    )


def _rules() -> dict[str, SymbolRules]:
    return {SYMBOL: make_symbol_rules()}


def _registry() -> StrategyRegistry:
    return build_default_registry()


# --- check refuses what run could not build -------------------------------------------------


def test_breakout_without_parameters_is_refused_by_startup_validation(
    directories: dict[str, Path],
) -> None:
    # The exact failure the VPS hit, now caught by `paper check` instead of by the session
    # forty minutes into a run.
    settings = _settings(directories, strategy_params={})

    with pytest.raises(StrategyParameterError):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


def test_breakout_without_parameters_is_refused_before_any_feed_is_constructed(
    directories: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # "Fails before the feed" is the whole point: a socket opened against a deployment that
    # cannot build its strategy is a connection nobody can use. Proven by watching the feed
    # class itself rather than by trusting the ordering of two statements.
    built: list[object] = []

    class _RecordingFeed:
        def __init__(self, *args: object, **kwargs: object) -> None:
            built.append(self)

    monkeypatch.setattr(paper_orchestration, "BinanceSpotMarketDataFeed", _RecordingFeed)
    settings = _settings(directories, strategy_params={})

    with pytest.raises(StrategyParameterError):
        build_paper_deployment(settings, symbol_rules=_rules(), registry=_registry())

    assert built == []


def test_breakout_starts_with_its_frozen_lookbacks(directories: dict[str, Path]) -> None:
    settings = _settings(directories, strategy_params=FROZEN_BREAKOUT_PARAMS)

    strategy = validate_startup(settings, symbol_rules=_rules(), registry=_registry())

    assert isinstance(strategy, BreakoutStrategy)
    parameters = strategy.parameters
    assert isinstance(parameters, BreakoutParameters)
    assert parameters.entry_lookback == 20
    assert parameters.exit_lookback == 10


def test_an_unknown_strategy_parameter_is_refused(directories: dict[str, Path]) -> None:
    # The strategy schemas already forbid extras; what this proves is that a typo in .env
    # reaches that refusal instead of being dropped on the way.
    settings = _settings(
        directories, strategy_params={**FROZEN_BREAKOUT_PARAMS, "entry_lookbak": "20"}
    )

    with pytest.raises(StrategyParameterError):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


def test_an_invalid_strategy_parameter_value_is_refused(directories: dict[str, Path]) -> None:
    settings = _settings(
        directories, strategy_params={"entry_lookback": "1", "exit_lookback": "10"}
    )

    with pytest.raises(StrategyParameterError):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


def test_a_non_numeric_strategy_parameter_is_refused(directories: dict[str, Path]) -> None:
    settings = _settings(
        directories, strategy_params={"entry_lookback": "twenty", "exit_lookback": "10"}
    )

    with pytest.raises(StrategyParameterError):
        validate_startup(settings, symbol_rules=_rules(), registry=_registry())


# --- the benchmark is economically untouched ------------------------------------------------


def test_ema_trend_still_resolves_with_no_parameters_configured(
    directories: dict[str, Path],
) -> None:
    # ema_trend is the frozen benchmark. It resolved from an empty mapping before this change
    # and must still resolve to exactly 20/50, or every comparison drawn against it moves.
    settings = _settings(directories, strategy_id="ema_trend")

    strategy = validate_startup(settings, symbol_rules=_rules(), registry=_registry())

    assert isinstance(strategy, EmaTrendStrategy)
    parameters = strategy.parameters
    assert isinstance(parameters, EmaTrendParameters)
    assert parameters.fast_period == 20
    assert parameters.slow_period == 50


def test_ema_trend_accepts_its_defaults_stated_explicitly(directories: dict[str, Path]) -> None:
    settings = _settings(
        directories,
        strategy_id="ema_trend",
        strategy_params={"fast_period": "20", "slow_period": "50"},
    )

    strategy = validate_startup(settings, symbol_rules=_rules(), registry=_registry())

    assert isinstance(strategy, EmaTrendStrategy)
    parameters = strategy.parameters
    assert isinstance(parameters, EmaTrendParameters)
    assert parameters.fast_period == 20
    assert parameters.slow_period == 50


# --- one resolver, not two --------------------------------------------------------------------


def test_check_and_run_construct_the_strategy_exactly_once_through_one_resolver(
    directories: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The original defect was two paths that happened to agree: validation asked a question
    # about the registry, wiring built from it separately. Counting construction proves the
    # deployment now takes the instance validation already built, so `check` cannot pass on a
    # strategy `run` would fail to construct.
    registry = _registry()
    calls: list[tuple[str, dict[str, Any]]] = []
    original = registry.create

    def _counting_create(
        strategy_id: str, parameters: Mapping[str, Any] | None = None
    ) -> BaseStrategy:
        calls.append((strategy_id, dict(parameters or {})))
        return original(strategy_id, parameters)

    monkeypatch.setattr(registry, "create", _counting_create)
    settings = _settings(directories, strategy_params=FROZEN_BREAKOUT_PARAMS)

    build_paper_deployment(settings, symbol_rules=_rules(), registry=registry)

    assert len(calls) == 1
    assert calls[0] == ("breakout", FROZEN_BREAKOUT_PARAMS)


def test_an_injected_strategy_still_bypasses_registry_resolution(
    directories: dict[str, Path],
) -> None:
    # A pre-built strategy is the documented seam for a test or a bespoke composition root.
    # It must keep working, and it must not be second-guessed by parameters meant for the
    # registry path.
    settings = _settings(directories, strategy_params={})
    injected = BreakoutStrategy(
        BreakoutStrategy.METADATA.parameter_schema(entry_lookback=20, exit_lookback=10)
    )

    deployment = build_paper_deployment(
        settings, symbol_rules=_rules(), strategy=injected, registry=_registry()
    )

    assert deployment.settings.paper.strategy_id == "breakout"


# --- the command-line surface -------------------------------------------------------------------


def test_repeated_param_options_become_the_configured_mapping() -> None:
    assert _strategy_params(["entry_lookback=20", "exit_lookback=10"]) == FROZEN_BREAKOUT_PARAMS


def test_a_param_option_without_a_value_separator_is_refused() -> None:
    with pytest.raises(ValueError, match="KEY=VALUE"):
        _strategy_params(["entry_lookback"])


def test_a_param_option_given_twice_is_refused() -> None:
    # Refused rather than resolved by precedence: silently keeping one of two conflicting
    # values discards something the operator typed, and either choice is a guess.
    with pytest.raises(ValueError, match="more than once"):
        _strategy_params(["entry_lookback=20", "entry_lookback=30"])


def test_param_options_reach_configuration_as_strategy_params() -> None:
    overrides = _overrides(
        symbols=None,
        timeframe=None,
        reports_dir=None,
        state_dir=None,
        log_dir=None,
        session_id=None,
        strategy="breakout",
        max_bars=None,
        params=["entry_lookback=20", "exit_lookback=10"],
    )

    assert overrides["strategy_params"] == FROZEN_BREAKOUT_PARAMS


def test_no_param_options_leave_configuration_untouched() -> None:
    # Absent rather than empty, so configuration keeps whatever .env declared instead of
    # being overwritten with nothing by a command that never mentioned parameters.
    overrides = _overrides(
        symbols=None,
        timeframe=None,
        reports_dir=None,
        state_dir=None,
        log_dir=None,
        session_id=None,
        strategy="breakout",
        max_bars=None,
        params=None,
    )

    assert "strategy_params" not in overrides


# --- Risk V2 wiring is untouched by any of this ------------------------------------------------


def test_risk_v2_wiring_is_unchanged_by_the_strategy_parameter_work(
    directories: dict[str, Path],
) -> None:
    settings = load_settings(
        paper={
            "session_id": "hotfix-session",
            "strategy_id": "breakout",
            "strategy_params": FROZEN_BREAKOUT_PARAMS,
            "symbols": (SYMBOL,),
            **{key: str(value) for key, value in directories.items()},
        },
        risk={
            "risk_per_trade_pct": "0.01",
            "max_position_exposure_pct": "0.5",
            "min_stop_distance_bps": "50",
            "max_stop_distance_bps": "1000",
            "initial_stop_distance_bps": "300",
            "break_even_activation_bps": "150",
            "trailing_activation_bps": "300",
            "trailing_distance_bps": "200",
            "take_profit_distance_bps": "600",
            "max_holding_bars": 168,
            "max_daily_loss_pct": "0.03",
            "max_consecutive_losses": 5,
            "latch_total_drawdown": True,
        },
    )

    config = _risk_configuration(settings, ExecutionPolicy())

    assert config.risk_v2_active is True
    assert config.stop_required is True
    assert config.risk_budget is not None
    assert config.initial_stop_distance_bps is not None
