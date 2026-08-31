"""Running a real experiment the way a person would run one.

Everything below this point has been exercised through stand-ins: a factory that ignored the
definition, an engine wired by a test helper. That proves the seams fit and nothing about
whether the composition root behind them works. These tests run the real factory, over real
bars, and check that what lands in the ledger describes what actually happened.

If an experiment is not runnable from the command line it will be run from a notebook, and a
notebook leaves no record — which is the whole thing the ledger exists to prevent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from typer.testing import CliRunner

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.cli.main import app
from quantplatform.core.enums import ExecutionMode
from quantplatform.features import NullFeaturePipeline
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.definition import (
    DatasetSpec,
    ExperimentDefinition,
    StrategySpec,
)
from quantplatform.research.digest import bars_digest
from quantplatform.research.ledger import ExperimentLedger, ReproducibilityVerdict, verify
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import ExperimentRunner
from quantplatform.strategies.registry import StrategyRegistry
from tests.factories import ANCHOR, SYMBOL, make_bars, make_risk_config, make_symbol_rules
from tests.integration.test_backtest_engine import _WARMUP_BARS, BuyThenSell

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the CLI at a migrated throwaway database, as the data commands' tests do."""
    for key in [name for name in os.environ if name.startswith("QP_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("QP_DATABASE__DSN", f"sqlite+aiosqlite:///{tmp_path / 'research.db'}")
    monkeypatch.chdir(_PROJECT_ROOT)

    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield
    finally:
        command.downgrade(config, "base")


def _definition() -> ExperimentDefinition:
    return ExperimentDefinition(
        name="ema-benchmark",
        dataset=DatasetSpec(
            symbol=SYMBOL,
            timeframe="1h",
            start=ANCHOR,
            end=ANCHOR.replace(year=2027),
            source="fixture",
        ),
        strategy=StrategySpec(strategy_id="buy_then_sell", strategy_version="1.0.0", params=()),
        risk=make_risk_config(),
        backtest=BacktestConfig(
            initial_capital=Decimal(100_000),
            execution_mode=ExecutionMode.BACKTEST,
            # The risk engine refuses to trade on a metric it was told to require and never
            # given. Only a run through the real factory surfaces that: a stand-in engine is
            # wired by a helper that already reconciled the two configurations.
            assumed_spread_basis_points=Decimal(1),
        ),
    )


def _factory() -> ExperimentEngineFactory:
    registry = StrategyRegistry()
    registry.register(BuyThenSell)
    return ExperimentEngineFactory(
        registry=registry,
        features_for=lambda _: NullFeaturePipeline(),
        symbols={SYMBOL: make_symbol_rules()},
        quote_asset="USDT",
    )


def _bars() -> tuple[object, ...]:
    return make_bars([Decimal(50_000)] * (_WARMUP_BARS + 4))


# --- The real composition root -------------------------------------------------------------------


def test_the_real_factory_runs_an_experiment_end_to_end() -> None:
    result = ExperimentRunner().run(
        _definition(),
        bars=_bars(),  # type: ignore[arg-type]
        factory=_factory(),
        code_revision="abc1234",
    )

    assert result.status is ExperimentStatus.SUCCEEDED
    assert result.code_revision == "abc1234"
    assert result.bars_digest == bars_digest(_bars())  # type: ignore[arg-type]
    performance = result.performance
    assert performance is not None
    assert performance.trades.count >= 1
    assert performance.turnover is not None
    assert performance.time_in_market is not None


def test_each_experiment_gets_an_untouched_account() -> None:
    # Two runs through one factory. If the portfolio or the broker were shared, the second
    # would inherit the first's positions and balances, and every comparison the harness
    # exists to support would be measuring the order the experiments happened to run in.
    factory = _factory()
    runner = ExperimentRunner()

    first = runner.run(_definition(), bars=_bars(), factory=factory)  # type: ignore[arg-type]
    second = runner.run(_definition(), bars=_bars(), factory=factory)  # type: ignore[arg-type]

    assert first.performance is not None
    assert second.performance is not None
    assert first.performance.initial_equity == second.performance.initial_equity
    assert first.performance.final_equity == second.performance.final_equity


def test_two_identical_real_runs_verify_as_reproducible(tmp_path: Path) -> None:
    # The golden this whole milestone was built to make possible.
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    runner = ExperimentRunner()
    factory = _factory()

    ledger.record(
        runner.run(_definition(), bars=_bars(), factory=factory, code_revision="abc1234")  # type: ignore[arg-type]
    )
    ledger.record(
        runner.run(_definition(), bars=_bars(), factory=factory, code_revision="abc1234")  # type: ignore[arg-type]
    )

    first, second = ledger.entries()
    assert verify(first, second) is ReproducibilityVerdict.REPRODUCIBLE


# --- The command line ----------------------------------------------------------------------------


def test_running_a_definition_from_the_command_line_records_it(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001 - the fixture is the environment, not an input
) -> None:
    # A strategy the *real* registry holds, because that is what the command resolves
    # against. Over an empty database the run has no bars, which the engine supports and
    # which is exactly the plumbing this test is about: definition in, ledger line out.
    definition = tmp_path / "experiment.json"
    definition.write_text(
        _definition()
        .model_copy(
            update={
                "strategy": StrategySpec(
                    strategy_id="ema_trend", strategy_version="1.0.0", params=()
                )
            }
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    ledger_path = tmp_path / "ledger.jsonl"

    outcome = CliRunner().invoke(
        app,
        ["research", "run", str(definition), "--ledger", str(ledger_path)],
    )

    assert outcome.exit_code == 0, outcome.output
    (entry,) = ExperimentLedger(ledger_path).entries()
    assert entry.status is ExperimentStatus.SUCCEEDED
    assert entry.bars_digest is not None


def test_a_definition_that_cannot_be_read_fails_the_command_rather_than_the_ledger(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "experiment.json"
    broken.write_text(json.dumps({"name": "incomplete"}), encoding="utf-8")
    ledger_path = tmp_path / "ledger.jsonl"

    outcome = CliRunner().invoke(
        app, ["research", "run", str(broken), "--ledger", str(ledger_path)]
    )

    assert outcome.exit_code != 0
    assert ExperimentLedger(ledger_path).entries() == ()
