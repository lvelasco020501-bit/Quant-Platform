"""Actually running a plan, fold by fold, and keeping every one of them.

The models and the summary existed; nothing walked the folds. Lineage was a parameter nobody
passed, so a plan was a contract with no execution behind it.

Two policies shape the runner and they pull in opposite directions. A fold that fails is a
fold, and abandoning the plan on the first one would turn ten observations into one — so the
run continues. But a failure of *integrity* says the platform can no longer describe its own
state, and continuing would produce nine more folds whose meaning nobody could defend. The
difference is typed, not guessed from a bare `Exception`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.errors import DataIntegrityError, StrategyError
from quantplatform.core.models.market import MarketBar
from quantplatform.research.definition import ExperimentDefinition, ExperimentRole
from quantplatform.research.folds import Fold, WalkForwardPlan, WindowSpec
from quantplatform.research.ledger import ExperimentLedger
from quantplatform.research.plan_runner import (
    BarLoader,
    WalkForwardOutcome,
    WalkForwardRunner,
)
from quantplatform.research.result import ExperimentStatus
from quantplatform.research.runner import BacktestFactory
from quantplatform.research.store import ResultStore
from tests.factories import (
    ANCHOR,
    make_bars,
    make_experiment_definition,
    make_research_factory,
)
from tests.integration.test_backtest_engine import _WARMUP_BARS

_HOUR_BARS = _WARMUP_BARS + 4


def _plan(base_id: str, folds: int = 2) -> WalkForwardPlan:
    step = 4
    return WalkForwardPlan(
        base_experiment_id=base_id,
        folds=tuple(
            Fold(
                index=index,
                train=WindowSpec(start=_at(index * 2 * step), end=_at(index * 2 * step + step)),
                test=WindowSpec(
                    start=_at(index * 2 * step + step), end=_at((index + 1) * 2 * step)
                ),
            )
            for index in range(folds)
        ),
    )


def _at(hours: int) -> datetime:
    return ANCHOR + timedelta(hours=hours)


def _loader(bars: Sequence[MarketBar]) -> BarLoader:
    """A loader holding every bar, slicing by window.

    The first three parameters are unread and still declared: the signature is the port the
    runner calls, and a stand-in that took different arguments would not stand in for it.
    """

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


def _run(
    tmp_path: Path, *, factory: BacktestFactory | None = None, folds: int = 2
) -> WalkForwardOutcome:
    base = make_experiment_definition(strategy_id="buy_then_sell")
    bars = make_bars([Decimal(50_000)] * 32)
    return WalkForwardRunner().run(
        base,
        _plan(base.experiment_id, folds),
        loader=_loader(bars),
        factory=factory if factory is not None else make_research_factory(),
        store=ResultStore(tmp_path / "results"),
        ledger=ExperimentLedger(tmp_path / "ledger.jsonl"),
        code_revision="abc1234",
    )


def test_every_fold_in_the_plan_is_run(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    assert len(outcome.folds) == 4


def test_each_fold_records_where_it_came_from(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    for run in outcome.folds:
        assert run.entry.plan_id == outcome.plan_id
        assert run.entry.fold_index is not None


def test_the_two_windows_of_a_fold_claim_the_two_roles(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    roles = [run.result.definition.role for run in outcome.folds]
    assert roles.count(ExperimentRole.WALK_FORWARD_TRAIN) == 2
    assert roles.count(ExperimentRole.WALK_FORWARD_TEST) == 2


def test_a_fold_that_fails_does_not_end_the_plan(tmp_path: Path) -> None:
    # Each window is an independent observation. Stopping at the first failure would turn a
    # plan into a single fold and report the rest as though they had never been attempted.
    calls = {"n": 0}

    def flaky(definition: ExperimentDefinition) -> BacktestEngine:
        calls["n"] += 1
        if calls["n"] == 2:
            raise StrategyError("this fold could not be built")
        return make_research_factory()(definition)

    outcome = _run(tmp_path, factory=flaky)

    statuses = [run.result.status for run in outcome.folds]
    assert statuses.count(ExperimentStatus.FAILED) == 1
    assert len(outcome.folds) == 4


def test_a_failure_of_integrity_stops_the_plan(tmp_path: Path) -> None:
    # The exception to the exception. An integrity error says the platform cannot describe
    # its own state; carrying on would manufacture folds nobody could defend, and the
    # summary they fed would look exactly as trustworthy as a real one.
    def corrupt(definition: ExperimentDefinition) -> BacktestEngine:
        del definition
        raise DataIntegrityError("the position could not be reconciled")

    outcome = _run(tmp_path, factory=corrupt)

    assert outcome.aborted is True
    assert len(outcome.folds) < 4


def test_an_aborted_plan_refuses_to_summarise_itself(tmp_path: Path) -> None:
    def corrupt(definition: ExperimentDefinition) -> BacktestEngine:
        del definition
        raise DataIntegrityError("the position could not be reconciled")

    outcome = _run(tmp_path, factory=corrupt)

    with pytest.raises(ValueError, match="aborted"):
        outcome.summarise()


def test_a_completed_plan_summarises_only_once_every_fold_has_run(tmp_path: Path) -> None:
    outcome = _run(tmp_path)

    summary = outcome.summarise()
    assert summary.folds_total == 2
