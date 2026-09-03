"""Running a whole walk-forward plan the way a person would run one.

The runner existed and was proven directly; nothing let it be reached from the command line.
A plan that can only be driven from a script is a plan a notebook will end up driving instead,
and a notebook leaves no ledger line — which is the one thing this whole harness exists to
prevent.

Exit codes carry the plan's outcome into a pipeline rather than only a terminal: 0 is a clean
plan, 1 is a mistake nobody's fold suffered from, 2 is folds that failed on their own account,
and 3 is the platform losing track of its own state partway through.
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
from quantplatform.config.settings import load_settings
from quantplatform.core.enums import ExecutionMode
from quantplatform.core.models.market import MarketBar
from quantplatform.research.canonical import canonical_json
from quantplatform.research.definition import ExperimentDefinition, StrategySpec
from quantplatform.research.folds import Fold, WalkForwardPlan, WindowSpec
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.result import ExperimentStatus
from quantplatform.storage.repository import SqlAlchemyMarketBarRepository
from quantplatform.storage.session import create_engine, create_session_factory
from tests.factories import ANCHOR, make_bar, make_dataset_spec, make_risk_config

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EMA_PARAMS: tuple[tuple[str, str], ...] = ()
"""ema_trend's METADATA declares fixed ema_20/ema_50 features regardless of params, so a
custom fast/slow period is rejected as a mismatch — the defaults are the only periods this
strategy's declared feature set actually supports."""


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at a migrated throwaway database, as the data commands' tests do."""
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("QP_DATABASE__DSN", f"sqlite+aiosqlite:///{tmp_path / 'walk_forward.db'}")
    monkeypatch.chdir(_PROJECT_ROOT)

    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield
    finally:
        command.downgrade(config, "base")


def _definition(
    *, strategy_id: str = "ema_trend", params: tuple[tuple[str, str], ...] = _EMA_PARAMS
) -> ExperimentDefinition:
    return ExperimentDefinition(
        name="walk-forward-probe",
        dataset=make_dataset_spec(start=ANCHOR, end=ANCHOR + timedelta(hours=16)),
        strategy=StrategySpec(
            strategy_id=strategy_id,
            strategy_version="1.0.0",
            params=params,
        ),
        risk=make_risk_config(),
        backtest=BacktestConfig(
            initial_capital=Decimal(100_000),
            execution_mode=ExecutionMode.BACKTEST,
            assumed_spread_basis_points=Decimal(1),
        ),
    )


def _plan(base_experiment_id: str) -> WalkForwardPlan:
    return WalkForwardPlan(
        base_experiment_id=base_experiment_id,
        folds=(
            Fold(
                index=0,
                train=WindowSpec(start=ANCHOR, end=ANCHOR + timedelta(hours=4)),
                test=WindowSpec(start=ANCHOR + timedelta(hours=4), end=ANCHOR + timedelta(hours=8)),
            ),
            Fold(
                index=1,
                train=WindowSpec(
                    start=ANCHOR + timedelta(hours=8), end=ANCHOR + timedelta(hours=12)
                ),
                test=WindowSpec(
                    start=ANCHOR + timedelta(hours=12), end=ANCHOR + timedelta(hours=16)
                ),
            ),
        ),
    )


def _write(path: Path, model: ExperimentDefinition | WalkForwardPlan) -> None:
    path.write_text(canonical_json(model), encoding="utf-8")


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
    """Seed one closed 1h SPOT BTC/USDT bar for each hour in ``[start, end)``."""
    bars = tuple(make_bar(index=index, close=Decimal(50_000)) for index in range(start, end))
    asyncio.run(_seed(bars))


def _walk_forward_args(plan_path: Path, definition_path: Path, tmp_path: Path) -> list[str]:
    return [
        "research",
        "walk-forward",
        str(plan_path),
        "--definition",
        str(definition_path),
        "--ledger",
        str(tmp_path / "ledger.jsonl"),
        "--results",
        str(tmp_path / "results"),
    ]


def _prepare(tmp_path: Path, definition: ExperimentDefinition) -> tuple[Path, Path]:
    definition_path, plan_path = tmp_path / "definition.json", tmp_path / "plan.json"
    _write(definition_path, definition)
    _write(plan_path, _plan(definition.experiment_id))
    return plan_path, definition_path


def test_every_fold_runs_and_is_persisted_with_lineage(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 16)
    definition = _definition()
    plan_path, definition_path = _prepare(tmp_path, definition)

    outcome = CliRunner().invoke(app, _walk_forward_args(plan_path, definition_path, tmp_path))

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.output)
    assert payload["aborted"] is False
    assert payload["folds_run"] == 4  # train + test, two folds
    entries = ExperimentLedger(tmp_path / "ledger.jsonl").entries()
    assert len(entries) == 4
    assert all(entry.status is ExperimentStatus.SUCCEEDED for entry in entries)
    assert all(entry.plan_id is not None and entry.fold_index is not None for entry in entries)
    assert {entry.fold_index for entry in entries} == {0, 1}


def test_a_mismatched_base_experiment_id_is_rejected_before_anything_runs(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    definition = _definition()
    definition_path, plan_path = tmp_path / "definition.json", tmp_path / "plan.json"
    _write(definition_path, definition)
    _write(plan_path, _plan("0" * 32))

    outcome = CliRunner().invoke(app, _walk_forward_args(plan_path, definition_path, tmp_path))

    assert outcome.exit_code == 1, outcome.output
    ledger_path = tmp_path / "ledger.jsonl"
    assert not ledger_path.exists() or ExperimentLedger(ledger_path).entries() == ()


def test_a_local_failure_does_not_stop_the_plan(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    # No strategy is registered under this id, so every fold fails identically at the
    # composition root — an ordinary, non-fatal failure, not a data-integrity one.
    _seed_hours(0, 16)
    definition = _definition(strategy_id="not_a_registered_strategy", params=())
    plan_path, definition_path = _prepare(tmp_path, definition)

    outcome = CliRunner().invoke(app, _walk_forward_args(plan_path, definition_path, tmp_path))

    assert outcome.exit_code == 2, outcome.output
    entries = ExperimentLedger(tmp_path / "ledger.jsonl").entries()
    assert len(entries) == 4
    assert all(entry.status is ExperimentStatus.FAILED for entry in entries)


def test_a_fatal_error_aborts_and_keeps_the_failing_fold(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    # Only the first fold's bars exist. Fold 0's train and test both succeed; fold 1's train
    # window has nothing to validate against, so the plan aborts there rather than running
    # fold 1's test window against the same broken loader.
    _seed_hours(0, 8)
    definition = _definition()
    plan_path, definition_path = _prepare(tmp_path, definition)

    outcome = CliRunner().invoke(app, _walk_forward_args(plan_path, definition_path, tmp_path))

    assert outcome.exit_code == 3, outcome.output
    entries = ExperimentLedger(tmp_path / "ledger.jsonl").entries()
    # Fold 0's train and test succeeded; fold 1's train was attempted, failed, and was
    # recorded before the plan gave up — the failing fold is evidence, not a silent stop.
    assert len(entries) == 3
    assert entries[0].status is ExperimentStatus.SUCCEEDED
    assert entries[1].status is ExperimentStatus.SUCCEEDED
    assert entries[2].status is ExperimentStatus.FAILED
    assert entries[2].fold_index == 1


def test_an_aborted_plan_prints_no_summary(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 8)
    definition = _definition()
    plan_path, definition_path = _prepare(tmp_path, definition)

    outcome = CliRunner().invoke(app, _walk_forward_args(plan_path, definition_path, tmp_path))

    assert outcome.exit_code == 3, outcome.output
    payload = json.loads(outcome.output)
    assert payload["aborted"] is True
    assert "summary" not in payload
    assert payload["aborted_at_fold"] == 1


def test_a_completed_plan_summarises_without_ranking(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 16)
    definition = _definition()
    plan_path, definition_path = _prepare(tmp_path, definition)

    outcome = CliRunner().invoke(app, _walk_forward_args(plan_path, definition_path, tmp_path))

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.output)
    summary = payload["summary"]
    assert "folds_total" in summary
    assert not {"best", "rank", "top", "winner"} & set(summary)


def test_a_completed_plan_with_local_failures_reports_them_in_the_summary(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001
) -> None:
    _seed_hours(0, 16)
    definition = _definition(strategy_id="not_a_registered_strategy", params=())
    plan_path, definition_path = _prepare(tmp_path, definition)

    outcome = CliRunner().invoke(app, _walk_forward_args(plan_path, definition_path, tmp_path))

    assert outcome.exit_code == 2, outcome.output
    payload = json.loads(outcome.output)
    assert payload["summary"]["folds_failed"] == 2
    assert payload["summary"]["folds_completed"] == 0
