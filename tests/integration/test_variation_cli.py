"""Sensitivity, stress and regimes from the command line — minimal, but real.

Each command runs through the real composition root, over a real (if throwaway) database,
the same way `qp research run` and `qp research walk-forward` already do. What is being
proved here is the plumbing: a plan file in, a ledger line and a distribution out — never a
ranking, never a selection.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from typer.testing import CliRunner

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.cli.main import app
from quantplatform.cli.research import regimes_app, sensitivity_app, stress_app
from quantplatform.config.settings import load_settings
from quantplatform.core.enums import CommissionModel, ExecutionMode
from quantplatform.core.models.market import MarketBar
from quantplatform.research.canonical import canonical_json
from quantplatform.research.definition import ExperimentDefinition, StrategySpec
from quantplatform.research.folds import WindowSpec
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.regime import RegimeEpisode, RegimePlan
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.sensitivity import SensitivityPlan, SensitivityVariation
from quantplatform.research.stress import StressPlan, StressScenario
from quantplatform.storage.repository import SqlAlchemyMarketBarRepository
from quantplatform.storage.session import create_engine, create_session_factory
from quantplatform.strategies.registry import StrategyRegistry
from tests.factories import (
    ANCHOR,
    make_bar,
    make_dataset_spec,
    make_execution_policy,
    make_risk_config,
)
from tests.integration.test_backtest_engine import BuyThenSell

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EMA_DEFAULT_PARAMS: tuple[tuple[str, str], ...] = ()


@pytest.fixture
def buy_then_sell_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the CLI's real registry for one holding a strategy that tolerates varying params.

    ``ema_trend`` — the only strategy the real registry ships — declares its EMA periods as
    fixed metadata (``ema_20``/``ema_50``); any other period is refused as a mismatch between
    what was configured and what the strategy declared it reads. That refusal is correct
    strategy-side behaviour, and it means ``ema_trend`` cannot demonstrate "different params
    produce different, *successful* runs" — so these specific tests swap in a strategy whose
    params it genuinely does not care about, the same way ``tests/factories.py``'s
    ``make_research_factory`` already does for the non-CLI research tests.
    """

    def _registry() -> StrategyRegistry:
        registry = StrategyRegistry()
        registry.register(BuyThenSell)
        return registry

    import quantplatform.cli.research as research_cli  # noqa: PLC0415

    monkeypatch.setattr(research_cli, "build_default_registry", _registry)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at a migrated throwaway database, as the other CLI tests do."""
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("QP_DATABASE__DSN", f"sqlite+aiosqlite:///{tmp_path / 'variation.db'}")
    monkeypatch.chdir(_PROJECT_ROOT)

    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield
    finally:
        command.downgrade(config, "base")


def _definition(
    *,
    strategy_id: str = "ema_trend",
    params: tuple[tuple[str, str], ...] = _EMA_DEFAULT_PARAMS,
) -> ExperimentDefinition:
    return ExperimentDefinition(
        name="variation-probe",
        dataset=make_dataset_spec(start=ANCHOR, end=ANCHOR + timedelta(hours=8)),
        strategy=StrategySpec(strategy_id=strategy_id, strategy_version="1.0.0", params=params),
        risk=make_risk_config(),
        backtest=BacktestConfig(
            initial_capital=Decimal(100_000),
            execution_mode=ExecutionMode.BACKTEST,
            assumed_spread_basis_points=Decimal(1),
        ),
    )


def _write(path: Path, model: object) -> None:
    path.write_text(canonical_json(model), encoding="utf-8")  # type: ignore[arg-type]


async def _seed(bars: tuple[MarketBar, ...]) -> None:
    settings = load_settings()
    engine = create_engine(settings.database)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await SqlAlchemyMarketBarRepository(session).add_bars(bars)
            await session.commit()
    finally:
        await engine.dispose()


def _seed_hours(start: int, end: int) -> None:
    bars = tuple(make_bar(index=index, close=Decimal(50_000)) for index in range(start, end))
    asyncio.run(_seed(bars))


# --- Sensitivity -----------------------------------------------------------------------------


def test_sensitivity_run_then_report(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
    buy_then_sell_registry: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 8)
    definition = _definition(strategy_id="buy_then_sell")
    definition_path, plan_path = tmp_path / "definition.json", tmp_path / "plan.json"
    _write(definition_path, definition)
    plan = SensitivityPlan(
        base_experiment_id=definition.experiment_id,
        variations=(
            SensitivityVariation(params=(("probe", "1"),)),
            SensitivityVariation(params=(("probe", "2"),)),
        ),
    )
    _write(plan_path, plan)
    ledger_path, results_path = tmp_path / "ledger.jsonl", tmp_path / "results"

    run_outcome = CliRunner().invoke(
        app,
        [
            "research",
            "sensitivity",
            "run",
            str(plan_path),
            "--definition",
            str(definition_path),
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )
    assert run_outcome.exit_code == 0, run_outcome.output
    payload = json.loads(run_outcome.output)
    assert payload["variations_run"] == 2
    assert payload["summary"]["count_total"] == 2

    report_outcome = CliRunner().invoke(
        app,
        [
            "research",
            "sensitivity",
            "report",
            "--plan-id",
            payload["plan_id"],
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )
    assert report_outcome.exit_code == 0, report_outcome.output
    report = json.loads(report_outcome.output)
    assert report == payload["summary"]


def test_sensitivity_run_makes_a_local_failure_visible(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 8)
    definition = _definition()
    definition_path, plan_path = tmp_path / "definition.json", tmp_path / "plan.json"
    _write(definition_path, definition)
    plan = SensitivityPlan(
        base_experiment_id=definition.experiment_id,
        variations=(
            # fast >= slow: EmaTrendParameters refuses this, a real, non-fatal, local failure.
            SensitivityVariation(params=(("fast_period", "20"), ("slow_period", "10"))),
        ),
    )
    _write(plan_path, plan)
    ledger_path = tmp_path / "ledger.jsonl"

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "sensitivity",
            "run",
            str(plan_path),
            "--definition",
            str(definition_path),
            "--ledger",
            str(ledger_path),
            "--results",
            str(tmp_path / "results"),
        ],
    )

    assert outcome.exit_code == 2, outcome.output
    entries = ExperimentLedger(ledger_path).entries()
    failed = [entry for entry in entries if entry.status is ExperimentStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].derived_from == definition.experiment_id


def test_sensitivity_entries_are_recorded_in_declared_order(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
    buy_then_sell_registry: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 8)
    definition = _definition(strategy_id="buy_then_sell")
    definition_path, plan_path = tmp_path / "definition.json", tmp_path / "plan.json"
    _write(definition_path, definition)
    declared = [
        (("probe", "1"),),
        (("probe", "2"),),
        (("probe", "3"),),
    ]
    plan = SensitivityPlan(
        base_experiment_id=definition.experiment_id,
        variations=tuple(SensitivityVariation(params=params) for params in declared),
    )
    _write(plan_path, plan)
    ledger_path = tmp_path / "ledger.jsonl"

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "sensitivity",
            "run",
            str(plan_path),
            "--definition",
            str(definition_path),
            "--ledger",
            str(ledger_path),
            "--results",
            str(tmp_path / "results"),
        ],
    )

    assert outcome.exit_code == 0, outcome.output
    variation_entries = [
        entry for entry in ExperimentLedger(ledger_path).entries() if entry.derived_from
    ]
    expected_ids = [
        definition.model_copy(
            update={"strategy": definition.strategy.model_copy(update={"params": params})}
        ).experiment_id
        for params in declared
    ]
    assert [entry.experiment_id for entry in variation_entries] == expected_ids


# --- Stress ------------------------------------------------------------------------------------


def test_stress_run_then_report(tmp_path: Path, cli_env: None) -> None:  # noqa: ARG001
    _seed_hours(0, 8)
    definition = _definition()
    definition_path, plan_path = tmp_path / "definition.json", tmp_path / "plan.json"
    _write(definition_path, definition)
    stressed = definition.risk.model_copy(
        update={
            "execution_policy": make_execution_policy(
                fee_model=CommissionModel.BASIS_POINTS, fee_basis_points=Decimal(30)
            )
        }
    )
    plan = StressPlan(
        base_experiment_id=definition.experiment_id,
        scenarios=(StressScenario(risk=stressed),),
    )
    _write(plan_path, plan)
    ledger_path, results_path = tmp_path / "ledger.jsonl", tmp_path / "results"

    run_outcome = CliRunner().invoke(
        app,
        [
            "research",
            "stress",
            "run",
            str(plan_path),
            "--definition",
            str(definition_path),
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )
    assert run_outcome.exit_code == 0, run_outcome.output
    payload = json.loads(run_outcome.output)
    assert payload["scenarios_run"] == 1
    assert payload["baseline_status"] == "succeeded"

    report_outcome = CliRunner().invoke(
        app,
        [
            "research",
            "stress",
            "report",
            "--plan-id",
            payload["plan_id"],
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )
    assert report_outcome.exit_code == 0, report_outcome.output
    assert json.loads(report_outcome.output) == payload["summary"]


# --- Regimes -------------------------------------------------------------------------------------


def test_regimes_run_with_a_prebuilt_plan_then_report(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 8)
    definition = _definition()
    definition_path, plan_path = tmp_path / "definition.json", tmp_path / "plan.json"
    _write(definition_path, definition)
    plan = RegimePlan(
        base_experiment_id=definition.experiment_id,
        labeller_id="fixture",
        episodes=(
            RegimeEpisode(
                label="first_half",
                window=WindowSpec(start=ANCHOR, end=ANCHOR + timedelta(hours=4)),
            ),
            RegimeEpisode(
                label="second_half",
                window=WindowSpec(
                    start=ANCHOR + timedelta(hours=4), end=ANCHOR + timedelta(hours=8)
                ),
            ),
        ),
    )
    _write(plan_path, plan)
    ledger_path, results_path = tmp_path / "ledger.jsonl", tmp_path / "results"

    run_outcome = CliRunner().invoke(
        app,
        [
            "research",
            "regimes",
            "run",
            str(plan_path),
            "--definition",
            str(definition_path),
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )
    assert run_outcome.exit_code == 0, run_outcome.output
    payload = json.loads(run_outcome.output)
    assert payload["episodes_run"] == 2
    assert set(payload["summary"]) == {"first_half", "second_half"}

    report_outcome = CliRunner().invoke(
        app,
        [
            "research",
            "regimes",
            "report",
            "--plan-id",
            payload["plan_id"],
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )
    assert report_outcome.exit_code == 0, report_outcome.output
    assert json.loads(report_outcome.output) == payload["summary"]


# --- Anti-selection --------------------------------------------------------------------------


def test_no_command_offers_a_selection_or_ranking() -> None:
    banned = {"best", "rank", "top", "winner", "select"}
    for sub_app in (sensitivity_app, regimes_app, stress_app):
        names = {command.name for command in sub_app.registered_commands}
        assert names <= {"run", "report"}
        assert not names & banned
