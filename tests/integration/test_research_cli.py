"""Running a real experiment the way a person would run one.

Everything below this point has been exercised through stand-ins: a factory that ignored the
definition, an engine wired by a test helper. That proves the seams fit and nothing about
whether the composition root behind them works. These tests run the real factory, over real
bars, and check that what lands in the ledger describes what actually happened.

If an experiment is not runnable from the command line it will be run from a notebook, and a
notebook leaves no record — which is the whole thing the ledger exists to prevent.
"""

from __future__ import annotations

import asyncio
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
from quantplatform.config.settings import load_settings
from quantplatform.core.enums import ExecutionMode
from quantplatform.core.models.market import MarketBar
from quantplatform.features import NullFeaturePipeline
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.canonical import canonical_json
from quantplatform.research.compare import COMPARISON_METRICS
from quantplatform.research.definition import (
    ExperimentDefinition,
    StrategySpec,
)
from quantplatform.research.digest import bars_digest
from quantplatform.research.ledger import ExperimentLedger, ReproducibilityVerdict, verify
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.store import ResultStore
from quantplatform.storage.repository import SqlAlchemyMarketBarRepository
from quantplatform.storage.session import create_engine, create_session_factory
from quantplatform.strategies.registry import StrategyRegistry
from tests.factories import (
    make_bars,
    make_dataset_spec,
    make_risk_config,
)
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
        dataset=make_dataset_spec(),
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
        registry=registry, features_for=lambda _: NullFeaturePipeline(), quote_asset="USDT"
    )


def _bars() -> tuple[MarketBar, ...]:
    return make_bars([Decimal(50_000)] * (_WARMUP_BARS + 4))


async def _seed(bars: tuple[MarketBar, ...]) -> None:
    """Write bars into the database the CLI's own commands read from."""
    settings = load_settings()
    engine = create_engine(settings.database)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await SqlAlchemyMarketBarRepository(session).add_bars(bars)
            await session.commit()
    finally:
        await engine.dispose()


# --- The real composition root -------------------------------------------------------------------


def test_the_real_factory_runs_an_experiment_end_to_end() -> None:
    result = ExperimentRunner().run(
        _definition(),
        bars=_bars(),
        factory=_factory(),
        code_revision="abc1234",
    )

    assert result.status is ExperimentStatus.SUCCEEDED
    assert result.code_revision == "abc1234"
    assert result.bars_digest == bars_digest(_bars())
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

    first = runner.run(_definition(), bars=_bars(), factory=factory)
    second = runner.run(_definition(), bars=_bars(), factory=factory)

    assert first.performance is not None
    assert second.performance is not None
    assert first.performance.initial_equity == second.performance.initial_equity
    assert first.performance.final_equity == second.performance.final_equity


def test_two_identical_real_runs_verify_as_reproducible(tmp_path: Path) -> None:
    # The golden this whole milestone was built to make possible.
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    runner = ExperimentRunner()
    factory = _factory()

    ledger.record(runner.run(_definition(), bars=_bars(), factory=factory, code_revision="abc1234"))
    ledger.record(runner.run(_definition(), bars=_bars(), factory=factory, code_revision="abc1234"))

    first, second = ledger.entries()
    assert verify(first, second) is ReproducibilityVerdict.REPRODUCIBLE


# --- The command line ----------------------------------------------------------------------------


def test_running_a_definition_from_the_command_line_records_it(
    tmp_path: Path,
    cli_env: None,  # noqa: ARG001 - the fixture is the environment, not an input
) -> None:
    # A strategy the *real* registry holds, because that is what the command resolves
    # against. Real bars are seeded so the dataset the definition names is one the loader can
    # actually validate — an empty database is now a DatasetMismatchError rather than a
    # trivial success, which is exactly the plumbing this test is about: definition in,
    # bars in, ledger line out.
    asyncio.run(_seed(_bars()))
    definition = tmp_path / "experiment.json"
    definition.write_text(
        canonical_json(
            _definition().model_copy(
                update={
                    "strategy": StrategySpec(
                        strategy_id="ema_trend", strategy_version="1.0.0", params=()
                    )
                }
            )
        ),
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


# --- M9c.3a: the two commands that read the record rather than adding to it ---------------------


def _recorded(tmp_path: Path, *, diverge: bool = False) -> tuple[Path, Path]:
    """Record one experiment twice, optionally with a second answer to the same question."""
    ledger_path, results_path = tmp_path / "ledger.jsonl", tmp_path / "results"
    ledger = ExperimentLedger(ledger_path)
    store = ResultStore(results_path)
    first = ExperimentRunner().run(
        _definition(),
        bars=_bars(),
        factory=_factory(),
        code_revision="abc1234",
    )
    ledger.record(first, store=store)
    second = (
        first.model_copy(
            update={
                "performance": first.performance.model_copy(  # type: ignore[union-attr]
                    update={"final_equity": Decimal(1)}
                )
            }
        )
        if diverge
        else ExperimentRunner().run(
            _definition(),
            bars=_bars(),
            factory=_factory(),
            code_revision="abc1234",
        )
    )
    ledger.record(second, store=store)
    return ledger_path, results_path


def test_verify_reports_a_clean_record_and_succeeds(tmp_path: Path) -> None:
    ledger_path, _ = _recorded(tmp_path)

    outcome = CliRunner().invoke(app, ["research", "verify", "--ledger", str(ledger_path)])

    assert outcome.exit_code == 0, outcome.output
    assert "reproducible" in outcome.output


def test_verify_fails_the_command_when_an_experiment_contradicted_itself(
    tmp_path: Path,
) -> None:
    # So it can stand in a pipeline rather than only in a terminal: a contradiction nobody
    # runs the command to notice is a contradiction that shapes decisions first.
    ledger_path, _ = _recorded(tmp_path, diverge=True)

    outcome = CliRunner().invoke(app, ["research", "verify", "--ledger", str(ledger_path)])

    assert outcome.exit_code != 0
    assert "reproducibility_failure" in outcome.output


def test_compare_returns_the_experiments_in_the_order_they_were_asked_for(
    tmp_path: Path,
) -> None:
    ledger_path, results_path = _recorded(tmp_path)
    other = _definition().model_copy(update={"name": "second"})
    ExperimentLedger(ledger_path).record(
        ExperimentRunner().run(other, bars=_bars(), factory=_factory()),
        store=ResultStore(results_path),
    )

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "compare",
            other.experiment_id,
            _definition().experiment_id,
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.index("second") < outcome.output.index("ema-benchmark")


def test_compare_reports_every_metric_including_the_ones_it_could_not_compute(
    tmp_path: Path,
) -> None:
    ledger_path, results_path = _recorded(tmp_path)

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "compare",
            _definition().experiment_id,
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
            "--json",
        ],
    )

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.output)
    (row,) = payload["rows"]
    assert set(row["metrics"]) == set(COMPARISON_METRICS)


def test_compare_refuses_to_answer_about_a_run_that_never_happened(tmp_path: Path) -> None:
    ledger_path, results_path = _recorded(tmp_path)

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "compare",
            "0" * 32,
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )

    assert outcome.exit_code != 0


# --- M9c.3a.1: verify's two audiences, compare by attempt, and a fail-closed provider ------------


def test_verify_default_output_is_human_readable(tmp_path: Path) -> None:
    ledger_path, _ = _recorded(tmp_path)

    outcome = CliRunner().invoke(app, ["research", "verify", "--ledger", str(ledger_path)])

    assert outcome.exit_code == 0, outcome.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(outcome.output)
    assert "2 entries" in outcome.output
    assert "reproducible" in outcome.output


def test_verify_json_matches_the_original_machine_readable_payload(tmp_path: Path) -> None:
    ledger_path, _ = _recorded(tmp_path)

    outcome = CliRunner().invoke(
        app, ["research", "verify", "--ledger", str(ledger_path), "--json"]
    )

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.output)
    assert set(payload) == {"entries", "verdicts", "reproducibility_failures"}
    assert payload["entries"] == 2
    assert payload["verdicts"] == {"reproducible": 1}
    assert payload["reproducibility_failures"] == []


def test_verify_json_also_fails_the_command_on_contradiction(tmp_path: Path) -> None:
    ledger_path, _ = _recorded(tmp_path, diverge=True)

    outcome = CliRunner().invoke(
        app, ["research", "verify", "--ledger", str(ledger_path), "--json"]
    )

    assert outcome.exit_code != 0
    payload = json.loads(outcome.output)
    assert len(payload["reproducibility_failures"]) == 1


def test_compare_resolves_an_explicit_attempt_selector(tmp_path: Path) -> None:
    ledger_path, results_path = _recorded(tmp_path)
    first_entry, _second_entry = ExperimentLedger(ledger_path).entries()

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "compare",
            f"attempt:{first_entry.attempt_id}",
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
            "--json",
        ],
    )

    assert outcome.exit_code == 0, outcome.output
    (row,) = json.loads(outcome.output)["rows"]
    assert row["experiment_id"] == first_entry.experiment_id


def test_compare_mixes_experiment_and_attempt_selectors_preserving_one_order(
    tmp_path: Path,
) -> None:
    ledger_path, results_path = _recorded(tmp_path)
    first_entry, _second_entry = ExperimentLedger(ledger_path).entries()
    other = _definition().model_copy(update={"name": "second"})
    ExperimentLedger(ledger_path).record(
        ExperimentRunner().run(other, bars=_bars(), factory=_factory()),
        store=ResultStore(results_path),
    )

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "compare",
            f"experiment:{other.experiment_id}",
            f"attempt:{first_entry.attempt_id}",
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output.index("second") < outcome.output.index("ema-benchmark")


def test_compare_refuses_an_attempt_with_no_ledger_line(tmp_path: Path) -> None:
    ledger_path, results_path = _recorded(tmp_path)
    orphan = ResultStore(results_path).save(
        ExperimentRunner().run(_definition(), bars=_bars(), factory=_factory())
    )

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "compare",
            f"attempt:{orphan}",
            "--ledger",
            str(ledger_path),
            "--results",
            str(results_path),
        ],
    )

    assert outcome.exit_code != 0


def test_capture_rules_requires_a_market_type(tmp_path: Path) -> None:
    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "capture-rules",
            "--symbol",
            "BTC/USDT",
            "--out",
            str(tmp_path / "rules.json"),
        ],
    )

    assert outcome.exit_code != 0
    assert "market-type" in outcome.output.lower() or "market_type" in outcome.output.lower()


def test_capture_rules_refuses_an_unimplemented_market_type_without_touching_the_network(
    tmp_path: Path,
) -> None:
    out = tmp_path / "rules.json"

    outcome = CliRunner().invoke(
        app,
        [
            "research",
            "capture-rules",
            "--symbol",
            "BTC/USDT",
            "--market-type",
            "futures",
            "--out",
            str(out),
        ],
    )

    assert outcome.exit_code != 0
    assert "futures" in outcome.output.lower()
    assert not out.exists()
