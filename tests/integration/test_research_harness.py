"""Running an experiment, and recording every one that ran.

The harness exists so that comparing two strategies stops being a conversation. Two things
make that possible and neither is the running: the identifier that says two runs were the
same experiment, and the ledger that says how many times we looked.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.errors import StrategyError
from quantplatform.core.models.market import MarketBar
from quantplatform.research.definition import (
    DatasetSpec,
    ExperimentDefinition,
    StrategySpec,
)
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.result import ExperimentStatus, result_hash
from quantplatform.research.runner import ExperimentRunner
from tests.factories import (
    ANCHOR,
    SYMBOL,
    make_backtest,
    make_backtest_config,
    make_bars,
    make_risk_config,
)
from tests.integration.test_backtest_engine import _WARMUP_BARS, BuyThenSell, _Params


def _definition(name: str = "ema-benchmark") -> ExperimentDefinition:
    return ExperimentDefinition(
        name=name,
        dataset=DatasetSpec(
            symbol=SYMBOL,
            timeframe="1h",
            start=ANCHOR,
            end=ANCHOR.replace(year=2027),
            source="fixture",
        ),
        strategy=StrategySpec(strategy_id="buy_then_sell", strategy_version="1.0.0", params=()),
        risk=make_risk_config(),
        backtest=make_backtest_config(),
    )


def _bars() -> tuple[MarketBar, ...]:
    return make_bars([Decimal(50_000)] * (_WARMUP_BARS + 4))


def _factory(definition: ExperimentDefinition) -> BacktestEngine:  # noqa: ARG001
    """Stand in for the composition root, which is the only thing allowed to wire an engine.

    The definition is unread here and still part of the signature: satisfying the protocol
    is the point, and a stand-in that took different arguments would prove nothing about
    the seam it stands in for.
    """
    engine, _, _ = make_backtest(strategy=BuyThenSell(_Params()))
    return engine


def _exploding_factory(definition: ExperimentDefinition) -> BacktestEngine:  # noqa: ARG001
    raise StrategyError("the strategy could not be constructed")


# --- The runner ---------------------------------------------------------------------------------


def test_the_runner_uses_the_factory_it_is_given_and_composes_nothing() -> None:
    seen: list[ExperimentDefinition] = []

    def recording(definition: ExperimentDefinition) -> BacktestEngine:
        seen.append(definition)
        return _factory(definition)

    ExperimentRunner().run(_definition(), bars=_bars(), factory=recording)

    assert seen == [_definition()]


def test_two_runs_of_one_definition_produce_the_same_reproducible_hash() -> None:
    # The golden. Everything else the harness offers rests on this being true, and it is the
    # one property that cannot be established by reading the code.
    runner = ExperimentRunner()

    first = runner.run(_definition(), bars=_bars(), factory=_factory)
    second = runner.run(_definition(), bars=_bars(), factory=_factory)

    assert first.experiment_id == second.experiment_id
    assert result_hash(first) == result_hash(second)


def test_a_completed_run_reports_every_required_metric() -> None:
    result = ExperimentRunner().run(_definition(), bars=_bars(), factory=_factory)

    assert result.status is ExperimentStatus.SUCCEEDED
    performance = result.performance
    assert performance is not None
    for name in (
        "total_return",
        "realized_pnl",
        "unrealized_pnl",
        "max_drawdown",
        "commission_paid",
        "slippage_paid",
        "turnover",
        "time_in_market",
    ):
        assert getattr(performance, name) is not None, name
    assert performance.trades.count >= 1


def test_a_run_that_blows_up_becomes_a_failed_result_rather_than_an_exception() -> None:
    result = ExperimentRunner().run(_definition(), bars=_bars(), factory=_exploding_factory)

    assert result.status is ExperimentStatus.FAILED
    assert result.error is not None
    assert result.performance is None


# --- The ledger ---------------------------------------------------------------------------------


def test_the_ledger_appends_and_never_replaces(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    runner = ExperimentRunner()

    ledger.record(runner.run(_definition(), bars=_bars(), factory=_factory))
    ledger.record(runner.run(_definition(), bars=_bars(), factory=_factory))

    assert len(ledger.entries()) == 2


def test_running_the_same_experiment_twice_stays_visible_as_twice(tmp_path: Path) -> None:
    # The whole anti-overfit mechanism, and it is a counter rather than a prohibition. Nobody
    # can be stopped from looking again; what can be arranged is that looking again is on the
    # record, so a result reached on the twentieth attempt cannot be presented as the first.
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    runner = ExperimentRunner()
    for _ in range(3):
        ledger.record(runner.run(_definition(), bars=_bars(), factory=_factory))

    identifier = _definition().experiment_id
    assert ledger.attempts(identifier) == 3


def test_a_failed_experiment_is_recorded_like_any_other(tmp_path: Path) -> None:
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    runner = ExperimentRunner()

    ledger.record(runner.run(_definition(), bars=_bars(), factory=_exploding_factory))

    (entry,) = ledger.entries()
    assert entry.status is ExperimentStatus.FAILED


def test_the_ledger_offers_no_way_to_ask_which_experiment_was_best(tmp_path: Path) -> None:
    # Deliberately absent. A `best()` would put the argmax of whichever metric it chose one
    # keystroke away, and the distance between "the harness can rank these" and "the harness
    # says this one is good" is exactly one habit.
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")

    assert not hasattr(ledger, "best")
    assert not hasattr(ledger, "rank")
    assert not hasattr(ledger, "top")


def test_the_ledger_reads_back_what_a_previous_process_wrote(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ExperimentLedger(path).record(
        ExperimentRunner().run(_definition(), bars=_bars(), factory=_factory)
    )

    assert len(ExperimentLedger(path).entries()) == 1


def test_a_benchmark_is_never_labelled_out_of_sample(tmp_path: Path) -> None:
    # EMA20/50 was watched throughout week 5. It is the yardstick every later strategy is
    # measured against and it is not clean out-of-sample evidence for anything, so the
    # vocabulary that would let a reader believe otherwise does not exist yet.
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    ledger.record(ExperimentRunner().run(_definition(), bars=_bars(), factory=_factory))

    (entry,) = ledger.entries()
    assert not hasattr(entry, "out_of_sample")
    assert Sequence is not None
    assert pytest is not None
