"""The whole path, once, with nothing standing in for anything.

Every layer below has been exercised on its own. What has never been shown is that they line
up: that rules captured into a definition reach the engine, that the engine's result survives
to disk, that the ledger points at evidence which is actually there, that re-running finds it
and agrees — and that when two runs of one question over one dataset under one revision give
two answers, the record says so instead of quietly holding both.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.market import MarketBar
from quantplatform.research.compare import compare
from quantplatform.research.folds import Fold, WalkForwardPlan, WindowSpec
from quantplatform.research.ledger import ExperimentLedger, ReproducibilityVerdict
from quantplatform.research.plan_runner import BarLoader, WalkForwardRunner
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import ExperimentRunner
from quantplatform.research.store import ResultStore
from tests.factories import (
    ANCHOR,
    make_bars,
    make_experiment_definition,
    make_research_factory,
)


def _bars() -> tuple[MarketBar, ...]:
    return make_bars([Decimal(50_000)] * 32)


def test_a_definition_becomes_evidence_that_a_second_run_agrees_with(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results")
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    runner = ExperimentRunner()
    definition = make_experiment_definition(strategy_id="buy_then_sell")
    factory = make_research_factory()

    first = runner.run(
        definition,
        bars=_bars(),
        factory=factory,
        code_revision="abc1234",
    )
    ledger.record(first, store=store)
    second = runner.run(
        definition,
        bars=_bars(),
        factory=factory,
        code_revision="abc1234",
    )
    entry = ledger.record(second, store=store)

    assert first.status is ExperimentStatus.SUCCEEDED
    assert entry.verdict is ReproducibilityVerdict.REPRODUCIBLE
    # The evidence is on disk and readable, not merely hashed.
    assert store.load(entry.attempt_id).performance is not None

    table = compare([store.load(line.attempt_id) for line in ledger.entries()])
    assert [row.experiment_id for row in table.rows] == [
        definition.experiment_id,
        definition.experiment_id,
    ]

    plan = WalkForwardPlan(
        base_experiment_id=definition.experiment_id,
        folds=(
            Fold(
                index=0,
                train=WindowSpec(start=ANCHOR, end=_at(8)),
                test=WindowSpec(start=_at(8), end=_at(16)),
            ),
        ),
    )
    outcome = WalkForwardRunner().run(
        definition,
        plan,
        loader=_window_loader(),
        factory=factory,
        store=store,
        ledger=ledger,
        code_revision="abc1234",
    )

    assert outcome.summarise().folds_total == 1


def test_two_answers_to_one_question_are_recorded_as_a_contradiction(tmp_path: Path) -> None:
    # Same experiment, same bars, same revision, different result. Nothing about the inputs
    # explains it, so the ledger accuses the engine — which is the one situation in which it
    # should.
    store = ResultStore(tmp_path / "results")
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl")
    definition = make_experiment_definition(strategy_id="buy_then_sell")
    runner = ExperimentRunner()

    honest = runner.run(
        definition,
        bars=_bars(),
        factory=make_research_factory(),
        code_revision="abc1234",
    )
    ledger.record(honest, store=store)
    diverged = honest.model_copy(
        update={"performance": honest.performance.model_copy(update={"final_equity": Decimal(1)})}  # type: ignore[union-attr]
    )
    ledger.record(diverged, store=store)

    entries = ledger.entries()
    alarm = entries[-1]
    assert alarm.status is ExperimentStatus.REPRODUCIBILITY_FAILURE
    assert alarm.compared_with == entries[0].entry_id
    # Both results survive: the contradiction is two facts, not a correction.
    assert store.load(entries[0].attempt_id) == honest
    assert store.load(entries[1].attempt_id) == diverged


def _at(hours: int) -> datetime:
    return ANCHOR + timedelta(hours=hours)


def _window_loader() -> BarLoader:
    """A loader holding every bar, slicing by window.

    The first three parameters are unread and still declared: the signature is the port the
    runner calls, and a stand-in that took different arguments would not stand in for it.
    """
    bars = _bars()

    def load(
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        window: WindowSpec,
    ) -> tuple[MarketBar, ...]:
        del symbol, market_type, timeframe
        return tuple(bar for bar in bars if window.contains(bar.open_time))

    return load
