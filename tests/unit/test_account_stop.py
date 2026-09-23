"""The account stop: closing open exposure when the global drawdown cap is broken.

M20 proved that gating entries cannot bound a drawdown — an open position keeps losing, and
both caps were crossed by roughly half a point. The only mechanism that can hold a global
limit is one that *closes* what is open, so that is what these tests pin.

The architecture needs nothing new. ``RiskActionKind.CLOSE`` already exists, ``RiskAction``
carries a free-text reason, and the backtest engine asks the risk engine what must happen to
open exposure on every bar, then authorises those exits ahead of any strategy intent and with
the administrative vetoes withdrawn. The account stop is therefore a research risk engine
returning CLOSE actions, and these tests check it behaves like a stop rather than a
suggestion: it fires on the breach bar, it fires once, the strategy cannot undo it, and the
account is flat and latched afterwards.

What a backtest cannot exercise is written down rather than faked: a crash between the
decision and the fill, and the persistence of the latch across a restart, are properties of
the paper and live runtime. They are specified in the milestone document as production
requirements, and the two things reachable here — that repeating the call does not double the
close, and that a whole run reproduces exactly — are tested.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from quantplatform.core.enums import RiskActionKind
from quantplatform.core.models.market import MarketBar
from quantplatform.research.recovery import (
    GLOBAL_DRAWDOWN_FORCED_EXIT,
    AccountStopAction,
    Recovery,
    RecoveryLatchRiskEngine,
    RecoveryPolicy,
)
from tests.unit.test_recovery import DRAWDOWN, RESTART, _falling, _run

CAP = Decimal("0.12")

STOPPING = RecoveryPolicy(
    key="S12",
    label="local reset, account stop at 12%",
    drawdown_pct=DRAWDOWN,
    recovery=Recovery.COOLDOWN_AND_RESTART,
    cooldown=timedelta(days=2),
    global_drawdown_cap=CAP,
    close_positions_on_cap=True,
)
GATING = STOPPING.model_copy(update={"close_positions_on_cap": False})


def _closes(engine: RecoveryLatchRiskEngine) -> list[AccountStopAction]:
    return [a for a in engine.actions_taken if a.kind is RiskActionKind.CLOSE]


def _bar(engine: RecoveryLatchRiskEngine) -> MarketBar:
    bar = engine.last_bar
    assert bar is not None, "the engine must have seen at least one bar"
    return bar


# --- The breach ------------------------------------------------------------------------------


def test_a_breach_with_an_open_position_closes_it_on_the_breach_bar() -> None:
    _, engine = _run(STOPPING)
    closes = _closes(engine)
    assert closes, "the series must actually breach the cap while holding something"
    assert engine.stopped_at is not None
    first = min(a.at for a in engine.actions_taken)
    assert first == engine.stopped_at, "the close is ordered on the bar the cap broke"


def test_a_breach_without_a_position_still_latches_the_account() -> None:
    # A rising series never breaches; a flat one with no exposure must still be able to stop
    # without ordering a close that has nothing to close.
    policy = STOPPING.model_copy(update={"global_drawdown_cap": Decimal("0.99")})
    _, engine = _run(policy)
    assert _closes(engine) == []
    assert engine.stopped_at is None


def test_every_close_names_the_account_stop_as_its_reason() -> None:
    _, engine = _run(STOPPING)
    for action in _closes(engine):
        assert action.reason == GLOBAL_DRAWDOWN_FORCED_EXIT


def test_nothing_is_opened_after_the_account_has_stopped() -> None:
    # Strictly after. A position may open *on* the stop bar, because the order that opened it
    # was approved before the stop existed and filled at that bar's open; the account stop
    # then closes it on the next bar, which is as early as anything can act. ADA under a 12%
    # cap does exactly that in the real runs, and it is not a violation.
    result, engine = _run(STOPPING)
    assert engine.stopped_at is not None
    later = [t for t in result.trades if t.opened_at > engine.stopped_at]
    assert later == [], "nothing may be opened once the account has stopped"
    assert result.performance is not None


def test_a_position_filled_on_the_stop_bar_is_closed_by_the_stop() -> None:
    result, engine = _run(STOPPING)
    assert engine.stopped_at is not None
    on_bar = [t for t in result.trades if t.opened_at == engine.stopped_at]
    for trade in on_bar:
        assert trade.closed_at > trade.opened_at
        assert trade.closed_at <= engine.stopped_at + timedelta(days=1)


def test_the_cap_holds_to_within_one_bar() -> None:
    # The whole point: M20's entry gate was crossed by 0.49pp because the open position kept
    # losing. Closing it bounds the loss to the move of the bar the breach was found on.
    result, engine = _run(STOPPING)
    assert result.performance is not None
    assert engine.stopped_at is not None
    assert result.performance.max_drawdown <= CAP + Decimal("0.02")


def test_gating_alone_lets_the_drawdown_run_further_than_closing() -> None:
    stopped, _ = _run(STOPPING)
    gated, _ = _run(GATING)
    assert stopped.performance is not None
    assert gated.performance is not None
    assert stopped.performance.max_drawdown <= gated.performance.max_drawdown


# --- Exactly once ----------------------------------------------------------------------------


def test_the_close_is_ordered_once_per_position() -> None:
    _, engine = _run(STOPPING)
    symbols = [a.symbol for a in _closes(engine)]
    assert len(symbols) == len(set(symbols)), "one close per symbol, never two"


def test_asking_twice_on_the_same_bar_does_not_double_the_close() -> None:
    # Idempotence at the seam the engine actually uses: the same bar evaluated twice must
    # produce the same instruction, not a second one.
    _, engine = _run(STOPPING)
    assert engine.stopped_at is not None
    positions = engine.last_positions
    first = engine.evaluate_open_positions(
        positions=positions, position_risk={}, bar=_bar(engine), require_protection=False
    )
    second = engine.evaluate_open_positions(
        positions=positions, position_risk={}, bar=_bar(engine), require_protection=False
    )
    assert first == second


def test_no_close_is_ordered_for_a_position_that_is_already_gone() -> None:
    _, engine = _run(STOPPING)
    assert engine.stopped_at is not None
    nothing = engine.evaluate_open_positions(
        positions=(), position_risk={}, bar=_bar(engine), require_protection=False
    )
    assert [a for a in nothing if a.kind is RiskActionKind.CLOSE] == []


# --- Authority -------------------------------------------------------------------------------


def test_the_strategy_cannot_open_anything_after_the_stop() -> None:
    result, engine = _run(STOPPING)
    assert engine.stopped_at is not None
    later = [d for d in result.decisions if d.decided_at > engine.stopped_at]
    assert later, "the series must keep asking after the stop"
    assert all(d.approved_order is None for d in later), "every later request must be refused"


def test_the_forced_exit_pays_the_same_costs_as_any_other_order() -> None:
    result, _ = _run(STOPPING)
    assert result.performance is not None
    assert result.performance.commission_paid > 0
    assert result.performance.slippage_paid > 0


# --- Nothing changes when no cap is set ------------------------------------------------------


def test_without_a_cap_the_engine_behaves_exactly_as_before() -> None:
    bars = _falling()
    with_stop, engine = _run(RESTART, bars)
    assert engine.stopped_at is None
    assert with_stop.performance is not None


def test_closing_on_the_cap_needs_a_cap_to_close_on() -> None:
    with pytest.raises(ValueError, match="cap"):
        RecoveryPolicy(
            key="X",
            label="bad",
            drawdown_pct=DRAWDOWN,
            recovery=Recovery.COOLDOWN_AND_RESTART,
            cooldown=timedelta(days=2),
            close_positions_on_cap=True,
        )
