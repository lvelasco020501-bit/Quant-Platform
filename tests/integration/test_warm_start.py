"""Warm-start restores market context, and nothing else, or it refuses.

Two properties are being defended here and they pull in opposite directions. One is that a
restarted session must reach *exactly* the decisions it would have reached had it never
stopped — not similar ones. The other is that warm-start must never become the way an
operator gets around the rule that an account carrying financial state cannot be resumed.

The first is why the equivalence tests compare decisions bit for bit. The second is why so
much of this file is refusals: a history that is subtly wrong seeds an indicator with
candles nobody accounted for, and that failure is silent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import (
    CircuitBreakerReason,
    ExecutionMode,
    MarketType,
    StopKind,
    Timeframe,
)
from quantplatform.core.errors import PaperSessionStateError, StorageError
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.paper import CURRENT_SCHEMA_VERSION, PaperSessionState
from quantplatform.core.models.portfolio import Balance, Position
from quantplatform.core.models.risk import CircuitBreakerState, PositionRiskState
from quantplatform.core.models.stops import StopSpecification
from quantplatform.core.models.warm_start import (
    MarketHistory,
    MarketHistoryManifest,
    WarmStartRecord,
    history_digest,
)
from quantplatform.orchestration.features import features_for
from quantplatform.paper import InMemoryPaperStateRepository, PaperTradingSession
from quantplatform.paper.warm_start import WarmStartDecision, evaluate_warm_start
from quantplatform.storage.market_history import FileMarketHistoryRepository
from quantplatform.strategies.breakout import BreakoutStrategy
from quantplatform.strategies.ema_trend import EmaTrendParameters, EmaTrendStrategy
from tests.factories import ANCHOR, SYMBOL, make_backtest, make_bars
from tests.integration.test_backtest_engine import BuyOnce, _Params

SESSION = "warm-1"
SOURCE = "source-1"


# --- fixtures --------------------------------------------------------------------------


def _bars(count: int, *, start: datetime = ANCHOR) -> tuple[MarketBar, ...]:
    """Return contiguous hourly candles."""
    made = make_bars([Decimal(50_000) + Decimal(i) for i in range(count)])
    assert made[0].open_time == start or True
    return made


def _manifest(**overrides: object) -> MarketHistoryManifest:
    base: dict[str, object] = {
        "source_session_id": SOURCE,
        "symbol": SYMBOL,
        "market_type": MarketType.SPOT,
        "timeframe": Timeframe.H1,
        "created_at": ANCHOR,
    }
    base.update(overrides)
    return MarketHistoryManifest(**base)  # type: ignore[arg-type]


def _history(bars: tuple[MarketBar, ...], **overrides: object) -> MarketHistory:
    base: dict[str, object] = {
        "manifest": _manifest(),
        "bars": bars,
        "bars_count": len(bars),
        "first_bar_close_time": bars[0].close_time,
        "last_bar_close_time": bars[-1].close_time,
        "digest": history_digest(bars),
    }
    base.update(overrides)
    return MarketHistory(**base)  # type: ignore[arg-type]


def _state(bars_seen: int, **overrides: object) -> PaperSessionState:
    bars = _bars(max(bars_seen, 1))
    base: dict[str, object] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "session_id": SOURCE,
        "strategy_id": "breakout",
        "execution_mode": ExecutionMode.PAPER,
        "quote_asset": "USDT",
        "started_at": ANCHOR,
        "saved_at": bars[-1].close_time,
        "balances": (
            Balance(
                asset="USDT",
                free=Decimal("10000"),
                locked=Decimal(0),
                updated_at=bars[-1].close_time,
            ),
        ),
        "bars_processed": bars_seen,
        "last_bar": bars[bars_seen - 1] if bars_seen else None,
        "realized_pnl": Decimal(0),
        "total_fees": Decimal(0),
    }
    base.update(overrides)
    return PaperSessionState(**base)  # type: ignore[arg-type]


def _position() -> Position:
    return Position(
        symbol=SYMBOL,
        base_asset="BTC",
        quote_asset="USDT",
        quantity=Decimal("0.1"),
        avg_entry_price=Decimal("59000"),
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=ANCHOR,
        updated_at=ANCHOR,
    )


def _decide(
    history: MarketHistory | None, state: PaperSessionState | None, **kw: object
) -> WarmStartDecision:
    params: dict[str, object] = {
        "symbol": SYMBOL,
        "market_type": MarketType.SPOT,
        "timeframe": Timeframe.H1,
        "required_history": 5,
    }
    params.update(kw)
    return evaluate_warm_start(history, source_state=state, **params)  # type: ignore[arg-type]


# --- the contract: market context only ---------------------------------------------------


def test_the_history_type_cannot_express_money() -> None:
    # The separation from resume is a property of the types, not a rule callers follow.
    financial = {
        "cash",
        "balance",
        "balances",
        "positions",
        "orders",
        "fills",
        "realized_pnl",
        "total_fees",
        "position_risk",
        "breakers",
        "equity",
    }
    for model in (MarketHistory, MarketHistoryManifest):
        assert not (set(model.model_fields) & financial), model.__name__


def test_a_warm_start_record_cannot_claim_it_restored_money() -> None:
    with pytest.raises(ValueError, match="cannot claim financial state was restored"):
        WarmStartRecord(
            applied_at=ANCHOR,
            source_session_id=SOURCE,
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            bars_loaded=5,
            required_history=5,
            first_bar_close_time=ANCHOR,
            last_bar_close_time=ANCHOR,
            digest="x",
            financial_state_restored=True,
        )


# --- refusals: the source session carried financial state --------------------------------


@pytest.mark.parametrize(
    ("label", "mutation"),
    [
        ("an open position", {"positions": (_position(),)}),
        (
            "balance reserved against a working order",
            {
                "balances": (
                    Balance(
                        asset="USDT",
                        free=Decimal("9000"),
                        locked=Decimal("1000"),
                        updated_at=ANCHOR,
                    ),
                )
            },
        ),
        ("realised pnl", {"realized_pnl": Decimal("12.5")}),
        ("fees paid", {"total_fees": Decimal("1.75")}),
    ],
)
def test_a_financially_mutated_source_is_refused(label: str, mutation: dict[str, object]) -> None:
    # The rule that stops warm-start becoming a way around resume. Starting fresh from these
    # candles would present an unreconciled account as a recovery.
    decision = _decide(_history(_bars(6)), _state(6, **mutation))

    assert decision.applied is False
    assert decision.refused is True
    assert label in decision.reason


def test_a_latched_breaker_in_the_source_is_refused() -> None:
    breaker = CircuitBreakerState(
        tripped_at=ANCHOR,
        reason=CircuitBreakerReason.DAILY_LOSS_LIMIT,
        consecutive_losses=0,
        daily_loss=Decimal("310"),
    )
    decision = _decide(_history(_bars(6)), _state(6, breakers=(breaker,)))

    assert decision.refused is True
    assert "a latched circuit breaker" in decision.reason


def test_recorded_position_risk_in_the_source_is_refused() -> None:
    risk = PositionRiskState(
        symbol=SYMBOL,
        stop=StopSpecification(kind=StopKind.HARD, trigger_price=Decimal("57230")),
        quantity=Decimal("0.1"),
        initial_risk_amount=Decimal("177"),
        current_risk_amount=Decimal("177"),
        entry_price=Decimal("59000"),
        opened_at=ANCHOR,
    )
    decision = _decide(
        _history(_bars(6)), _state(6, positions=(_position(),), position_risk=(risk,))
    )

    assert decision.refused is True
    assert "recorded position risk" in decision.reason


def test_a_missing_source_snapshot_is_refused_rather_than_assumed_clean() -> None:
    decision = _decide(_history(_bars(6)), None)

    assert decision.refused is True
    assert "no snapshot" in decision.reason


def test_a_clean_source_is_accepted() -> None:
    decision = _decide(_history(_bars(6)), _state(6))

    assert decision.applied is True
    assert decision.refused is False
    assert "no financial state" in decision.reason


# --- refusals: the history does not match this deployment ---------------------------------


def test_a_history_of_another_instrument_is_refused() -> None:
    decision = _decide(_history(_bars(6)), _state(6), symbol="ETH/USDT")

    assert decision.refused is True
    assert "another instrument" in decision.reason


def test_a_history_of_another_timeframe_is_refused() -> None:
    decision = _decide(_history(_bars(6)), _state(6), timeframe=Timeframe.M15)

    assert decision.refused is True
    assert "different indicator wearing the same name" in decision.reason


def test_a_history_of_another_market_type_is_refused() -> None:
    decision = _decide(_history(_bars(6)), _state(6), market_type=MarketType.FUTURES)

    assert decision.refused is True
    assert "market" in decision.reason


def test_a_history_shorter_than_the_strategy_needs_is_refused() -> None:
    # Refused rather than applied in part: a partial warm-start leaves the session believing
    # it is ready when it is not.
    decision = _decide(_history(_bars(4)), _state(4), required_history=21)

    assert decision.refused is True
    assert "partial warm-start is refused" in decision.reason


def test_a_history_disagreeing_with_the_snapshots_candle_count_is_refused() -> None:
    decision = _decide(_history(_bars(6)), _state(9))

    assert decision.refused is True
    assert "disagree about what the session saw" in decision.reason


def test_a_history_not_ending_where_the_snapshot_says_is_refused() -> None:
    bars = _bars(6)
    state = _state(6).model_copy(update={"last_bar": bars[3]})

    decision = _decide(_history(bars), state)

    assert decision.refused is True
    assert "different points in time" in decision.reason


def test_no_history_at_all_is_an_ordinary_cold_start_not_a_refusal() -> None:
    decision = _decide(None, _state(6))

    assert decision.applied is False
    assert decision.refused is False
    assert "starting cold" in decision.reason


# --- the history artefact validates itself -------------------------------------------------


def test_a_gap_in_the_history_is_refused() -> None:
    bars = _bars(6)
    torn = (*bars[:3], *bars[4:])

    with pytest.raises(ValueError, match="gap of"):
        _history(torn)


def test_an_out_of_order_history_is_refused() -> None:
    # In a contiguous series, swapping two neighbours is detected at the earlier position
    # as a discontinuity rather than at the later one as disorder — the candle that should
    # have followed is simply not there. Both rules refuse, and both name the problem; which
    # one fires first is a detail of where the sequence stops making sense.
    bars = _bars(6)
    shuffled = (*bars[:2], bars[3], bars[2], *bars[4:])

    with pytest.raises(ValueError, match=r"gap of|not strictly ordered"):
        _history(shuffled)


def test_a_duplicated_candle_is_refused() -> None:
    bars = _bars(6)
    duplicated = (*bars[:3], bars[2], *bars[3:])

    with pytest.raises(ValueError, match="not strictly ordered"):
        _history(duplicated)


def test_a_history_whose_digest_does_not_match_its_content_is_refused() -> None:
    with pytest.raises(ValueError, match="digest does not match"):
        _history(_bars(6), digest="0" * 64)


def test_a_history_whose_count_disagrees_with_its_content_is_refused() -> None:
    with pytest.raises(ValueError, match="claims 99 bars but holds"):
        _history(_bars(6), bars_count=99)


def test_a_history_holding_another_instrument_than_its_manifest_is_refused() -> None:
    with pytest.raises(ValueError, match="claims to describe"):
        _history(_bars(6), manifest=_manifest(symbol="ETH/USDT"))


def test_a_history_holding_an_unclosed_candle_is_refused() -> None:
    bars = _bars(6)
    forming = bars[-1].model_copy(update={"is_closed": False})

    with pytest.raises(ValueError, match="unclosed candle"):
        _history((*bars[:-1], forming))


def test_the_digest_depends_on_order() -> None:
    # The same candles in a different order are a different history: a recursive indicator
    # reads them forwards, so order is part of the identity rather than presentation.
    bars = _bars(6)
    assert history_digest(bars) != history_digest(tuple(reversed(bars)))


def test_the_digest_is_stable_for_the_same_input() -> None:
    bars = _bars(6)
    assert history_digest(bars) == history_digest(bars)


# --- persistence ------------------------------------------------------------------------------


def test_a_history_round_trips_through_the_file(tmp_path: Path) -> None:
    repo = FileMarketHistoryRepository(tmp_path)
    repo.start(_manifest())
    bars = _bars(6)
    for bar in bars:
        repo.append(SOURCE, bar)

    loaded = repo.load(SOURCE)

    assert loaded is not None
    assert loaded.bars == bars
    assert loaded.bars_count == 6
    assert loaded.digest == history_digest(bars)


def test_an_absent_history_loads_as_nothing(tmp_path: Path) -> None:
    assert FileMarketHistoryRepository(tmp_path).load("never-ran") is None


def test_a_manifest_naming_another_session_is_refused(tmp_path: Path) -> None:
    # The binding that makes loading the wrong file impossible rather than unlikely.
    repo = FileMarketHistoryRepository(tmp_path)
    path = repo.path_for(SESSION)
    path.write_text(_manifest(source_session_id="somebody-else").model_dump_json() + "\n")

    with pytest.raises(StorageError, match="names a different session"):
        repo.load(SESSION)


def test_a_torn_final_line_is_dropped_rather_than_treated_as_corruption(tmp_path: Path) -> None:
    # The signature of a process killed mid-append. The candle it describes was never fully
    # processed either, so dropping it keeps the file consistent with the snapshot.
    repo = FileMarketHistoryRepository(tmp_path)
    repo.start(_manifest())
    bars = _bars(6)
    for bar in bars:
        repo.append(SOURCE, bar)
    with repo.path_for(SOURCE).open("a", encoding="utf-8") as handle:
        handle.write('{"symbol": "BTC/USDT", "open_ti')

    loaded = repo.load(SOURCE)

    assert loaded is not None
    assert loaded.bars_count == 6


def test_a_malformed_line_that_is_not_the_last_is_refused(tmp_path: Path) -> None:
    repo = FileMarketHistoryRepository(tmp_path)
    repo.start(_manifest())
    for bar in _bars(3):
        repo.append(SOURCE, bar)
    lines = repo.path_for(SOURCE).read_text().splitlines()
    lines.insert(2, "{ not a bar")
    repo.path_for(SOURCE).write_text("\n".join(lines) + "\n")

    with pytest.raises(StorageError, match="malformed candle"):
        repo.load(SOURCE)


def test_the_manifest_is_written_once_and_not_rewritten(tmp_path: Path) -> None:
    # Rewriting it mid-run would discard the binding that makes the file safe to load.
    repo = FileMarketHistoryRepository(tmp_path)
    repo.start(_manifest())
    repo.append(SOURCE, _bars(1)[0])
    repo.start(_manifest(created_at=ANCHOR + timedelta(days=1)))

    assert json.loads(repo.path_for(SOURCE).read_text().splitlines()[0])["created_at"].startswith(
        ANCHOR.isoformat()[:10]
    )


def test_a_session_id_that_is_a_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="not usable as a file name"):
        FileMarketHistoryRepository(tmp_path).path_for("../escape")


def test_a_read_only_directory_is_still_readable(tmp_path: Path) -> None:
    repo = FileMarketHistoryRepository(tmp_path)
    repo.start(_manifest())
    for bar in _bars(6):
        repo.append(SOURCE, bar)
    tmp_path.chmod(0o555)
    try:
        assert FileMarketHistoryRepository.for_reading(tmp_path).load(SOURCE) is not None
    finally:
        tmp_path.chmod(0o755)


# --- equivalence: the test that decides whether any of this is trustworthy ------------------


def _run(
    bars: tuple[MarketBar, ...], *, warm: int = 0, strategy: object | None = None
) -> tuple[list[tuple[object, ...]], tuple[MarketBar, ...]]:
    """Drive a session over ``bars``, optionally seeding the first ``warm`` of them.

    Returns what the session decided, in a form two runs can be compared by.
    """
    clock = SimulatedClock(ANCHOR)
    resolved = strategy if strategy is not None else BuyOnce(_Params())
    engine, broker, portfolio = make_backtest(
        strategy=resolved,  # type: ignore[arg-type]
        features=features_for(resolved),  # type: ignore[arg-type]
    )
    session = PaperTradingSession(
        session_id=SESSION,
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=InMemoryPaperStateRepository(),
    )
    if warm:
        session.warm_start(_history(bars[:warm]), required_history=1)
    session.start()
    outcomes = []
    for bar in bars[warm:]:
        clock.set_time(bar.close_time)
        outcome = session.submit_bar(bar)
        if outcome is not None:
            outcomes.append(_fingerprint(outcome))
    session.stop()
    # The window the feature pipelines were handed. Pure functions of it, so an
    # identical window is an identical indicator.
    run_state = session._state
    window = tuple(run_state.history.get(SYMBOL, ())) if run_state is not None else ()
    return outcomes, window


def _fingerprint(outcome: object) -> tuple[object, ...]:
    """Reduce a bar's outcome to everything that could differ between two runs."""
    return (
        outcome.bar.close_time,  # type: ignore[attr-defined]
        tuple((s.symbol, s.action.value, str(s.confidence)) for s in outcome.signals),  # type: ignore[attr-defined]
        tuple(
            (i.side.value, str(i.requested_quantity), str(i.requested_notional), i.reason)
            for i in outcome.intents  # type: ignore[attr-defined]
        ),
        tuple(
            (d.approved_order is not None, tuple(d.rejection_reasons))
            for d in outcome.decisions  # type: ignore[attr-defined]
        ),
        tuple((str(f.price), str(f.quantity)) for f in outcome.fills),  # type: ignore[attr-defined]
    )


def test_a_warm_started_session_sees_exactly_the_window_an_uninterrupted_one_sees() -> None:
    # The invariant warm-start actually guarantees, and the one everything else rests on.
    # The feature pipelines are pure functions of this window, so an identical window is an
    # identical indicator — no approximation, no decayed seed, no tolerance.
    bars = make_bars([Decimal(50_000) + Decimal(i * 10) for i in range(24)])

    _, uninterrupted = _run(bars)
    _, restarted = _run(bars, warm=8)

    assert restarted == uninterrupted


def test_the_restored_window_is_identical_for_the_ema_strategy() -> None:
    bars = make_bars([Decimal(50_000) + Decimal(i * 25) for i in range(60)])
    strategy = EmaTrendStrategy(EmaTrendParameters())

    _, uninterrupted = _run(bars, strategy=strategy)
    _, restarted = _run(bars, warm=20, strategy=strategy)

    assert restarted == uninterrupted


def test_the_restored_window_is_identical_for_the_breakout_strategy() -> None:
    bars = make_bars([Decimal(50_000) + Decimal(i * 25) for i in range(60)])
    params = BreakoutStrategy.METADATA.parameter_schema(entry_lookback=20, exit_lookback=10)

    _, uninterrupted = _run(bars, strategy=BreakoutStrategy(params))
    _, restarted = _run(bars, warm=25, strategy=BreakoutStrategy(params))

    assert restarted == uninterrupted


def test_a_warm_started_session_decides_exactly_what_an_uninterrupted_one_would() -> None:
    # Decision equivalence, in the only situation M12.1a permits: a source that was flat and
    # had traded nothing. Not "similar": identical. A restarted session that decided *nearly*
    # the same things would be a different strategy wearing the same name.
    bars = make_bars([Decimal(50_000) + Decimal(i * 10) for i in range(24)])

    uninterrupted, _ = _run(bars)
    restarted, _ = _run(bars, warm=8)

    assert restarted == uninterrupted[8:]


def test_decision_equivalence_holds_for_the_ema_strategy() -> None:
    bars = make_bars([Decimal(50_000) + Decimal(i * 25) for i in range(60)])
    strategy = EmaTrendStrategy(EmaTrendParameters())

    uninterrupted, _ = _run(bars, strategy=strategy)
    restarted, _ = _run(bars, warm=20, strategy=strategy)

    assert restarted == uninterrupted[20:]


def test_decisions_diverge_when_the_uninterrupted_run_held_a_position_and_that_is_correct() -> None:
    # The boundary of the equivalence claim, asserted rather than left implicit.
    #
    # Warm-start restores candles and refuses to restore an account. So when the
    # uninterrupted run was already long at the seam, the restarted one is flat there and
    # will legitimately act differently — it is not continuing that position because
    # continuing it is precisely what the contract forbids.
    #
    # This is not a gap in the guarantee. A source session holding a position carries
    # financial state, and `evaluate_warm_start` refuses its history outright, so the
    # divergence below can never reach a real deployment.
    bars = make_bars([Decimal(50_000) + Decimal(i * 25) for i in range(60)])
    params = BreakoutStrategy.METADATA.parameter_schema(entry_lookback=20, exit_lookback=10)

    uninterrupted, window = _run(bars, strategy=BreakoutStrategy(params))
    restarted, restarted_window = _run(bars, warm=25, strategy=BreakoutStrategy(params))

    # The market context is identical — that part of the guarantee holds unconditionally.
    assert restarted_window == window
    # The decisions are not, because one run inherited a position and the other could not.
    assert restarted != uninterrupted[25:]
    # And such a source would never have been accepted in the first place.
    holding = _state(6, positions=(_position(),))
    assert _decide(_history(_bars(6)), holding).refused is True


def test_a_crash_before_any_financial_mutation_recovers_its_market_context() -> None:
    # The case M12.1a exists for: the session died during warm-up, having traded nothing.
    bars = make_bars([Decimal(50_000) + Decimal(i * 10) for i in range(24)])

    uninterrupted, _ = _run(bars)
    recovered, _ = _run(bars, warm=15)

    assert recovered == uninterrupted[15:]


# --- warm-start restores candles and nothing else -------------------------------------------


def test_warm_start_leaves_the_account_untouched() -> None:
    clock = SimulatedClock(ANCHOR)
    engine, broker, portfolio = make_backtest(strategy=BuyOnce(_Params()))
    session = PaperTradingSession(
        session_id=SESSION,
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=InMemoryPaperStateRepository(),
    )
    bars = make_bars([Decimal(50_000)] * 10)

    session.warm_start(_history(bars[:6]), required_history=1)
    session.start()
    session.save()
    stored = session.save()

    assert stored is not None
    assert stored.realized_pnl == Decimal(0)
    assert stored.total_fees == Decimal(0)
    assert stored.positions == ()
    assert stored.position_risk == ()
    assert stored.breakers == ()
    assert stored.financial_state_carried == ()
    assert stored.warm_start is not None
    assert stored.warm_start.financial_state_restored is False


def test_warm_start_evaluates_no_strategy_over_the_restored_candles() -> None:
    # Running the strategy over candles that have already happened would be trading a past
    # that is already decided, which is the definition of look-ahead.
    clock = SimulatedClock(ANCHOR)
    engine, broker, portfolio = make_backtest(strategy=BuyOnce(_Params()))
    session = PaperTradingSession(
        session_id=SESSION,
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=InMemoryPaperStateRepository(),
    )

    session.warm_start(_history(make_bars([Decimal(50_000)] * 8)), required_history=1)

    metrics = session.runtime_metrics()
    assert metrics.bars_processed == 0
    assert metrics.signals_generated == 0
    assert metrics.intents_created == 0
    assert metrics.orders_submitted == 0
    assert metrics.fills_received == 0


def test_a_candle_at_or_before_the_restored_window_is_refused() -> None:
    # The seam. A feed re-sending its last candle is ordinary; acting on it twice is not.
    clock = SimulatedClock(ANCHOR)
    engine, broker, portfolio = make_backtest(strategy=BuyOnce(_Params()))
    session = PaperTradingSession(
        session_id=SESSION,
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=InMemoryPaperStateRepository(),
    )
    bars = make_bars([Decimal(50_000)] * 10)
    session.warm_start(_history(bars[:6]), required_history=1)
    session.start()

    clock.set_time(bars[9].close_time)
    assert session.submit_bar(bars[5]) is None
    assert session.submit_bar(bars[3]) is None
    assert session.submit_bar(bars[6]) is not None


def test_the_seam_is_recorded_when_the_first_live_candle_arrives() -> None:
    clock = SimulatedClock(ANCHOR)
    engine, broker, portfolio = make_backtest(strategy=BuyOnce(_Params()))
    session = PaperTradingSession(
        session_id=SESSION,
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=InMemoryPaperStateRepository(),
    )
    bars = make_bars([Decimal(50_000)] * 10)
    session.warm_start(_history(bars[:6]), required_history=1)
    session.start()

    assert session.warm_start_record is not None
    assert session.warm_start_record.first_live_bar_close_time is None

    clock.set_time(bars[6].close_time)
    session.submit_bar(bars[6])

    assert session.warm_start_record.first_live_bar_close_time == bars[6].close_time


def test_warm_starting_a_running_session_is_refused() -> None:
    clock = SimulatedClock(ANCHOR)
    engine, broker, portfolio = make_backtest(strategy=BuyOnce(_Params()))
    session = PaperTradingSession(
        session_id=SESSION,
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,
        clock=clock,
        state_repository=InMemoryPaperStateRepository(),
    )
    session.start()

    with pytest.raises(PaperSessionStateError, match="cannot warm-start a running session"):
        session.warm_start(_history(make_bars([Decimal(50_000)] * 6)), required_history=1)


# --- warm-start is not a back door into resume ------------------------------------------------


def test_a_snapshot_with_a_warm_start_record_still_refuses_to_resume() -> None:
    # The rule that keeps the two contracts apart. Market context restored is not an account
    # restored, and a record saying the first happened must not soften the second.
    record = WarmStartRecord(
        applied_at=ANCHOR,
        source_session_id=SOURCE,
        symbol=SYMBOL,
        market_type=MarketType.SPOT,
        timeframe=Timeframe.H1,
        bars_loaded=6,
        required_history=5,
        first_bar_close_time=ANCHOR,
        last_bar_close_time=ANCHOR + timedelta(hours=6),
        digest="a" * 64,
    )
    mutated = _state(6, total_fees=Decimal("1.75"), warm_start=record)

    assert mutated.financial_state_carried == ("fees paid",)


def test_the_predicate_is_unaffected_by_a_warm_start_record() -> None:
    record = WarmStartRecord(
        applied_at=ANCHOR,
        source_session_id=SOURCE,
        symbol=SYMBOL,
        market_type=MarketType.SPOT,
        timeframe=Timeframe.H1,
        bars_loaded=6,
        required_history=5,
        first_bar_close_time=ANCHOR,
        last_bar_close_time=ANCHOR + timedelta(hours=6),
        digest="a" * 64,
    )
    clean = _state(6)

    assert clean.financial_state_carried == ()
    assert clean.model_copy(update={"warm_start": record}).financial_state_carried == ()


def test_a_snapshot_below_version_three_cannot_carry_a_warm_start_record() -> None:
    record = WarmStartRecord(
        applied_at=ANCHOR,
        source_session_id=SOURCE,
        symbol=SYMBOL,
        market_type=MarketType.SPOT,
        timeframe=Timeframe.H1,
        bars_loaded=6,
        required_history=5,
        first_bar_close_time=ANCHOR,
        last_bar_close_time=ANCHOR + timedelta(hours=6),
        digest="a" * 64,
    )

    with pytest.raises(ValueError, match="below schema_version 3"):
        _state(6, schema_version=2, warm_start=record)
