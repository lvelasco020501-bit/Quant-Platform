"""The brains read a session rather than merely measuring it.

Everything here is about the three claims the dashboard makes by existing: that it
interprets honestly, that it never invents, and that it says so when a number is too thin to
mean anything. Those are properties of this module, not of the page, which is exactly why
the interpretation lives in Python where it can be asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantplatform.core.enums import CircuitBreakerReason, StopKind
from quantplatform.core.models.portfolio import Position
from quantplatform.core.models.risk import CircuitBreakerState, PositionRiskState
from quantplatform.core.models.stops import StopSpecification
from quantplatform.reporting.models import (
    DailyAlerts,
    DailyHealth,
    DailyReport,
    DailyStatistics,
    DailySummary,
    HealthLevel,
)
from quantplatform.status.brains import (
    MEANINGFUL_TRADE_SAMPLE,
    Brain,
    Kpi,
    Level,
    Source,
    build_brains,
    summarise,
)
from quantplatform.status.events import ActivityCounts
from quantplatform.status.model import Health, SessionStatus
from tests.factories import SYMBOL, make_bar

STARTED = datetime.now(UTC) - timedelta(hours=6)


def _status(**overrides: object) -> SessionStatus:
    base: dict[str, object] = {
        "health": Health.HEALTHY,
        "running": True,
        "session_id": "s",
        "strategy_id": "breakout",
        "strategy_parameters": {"entry_lookback": "20", "exit_lookback": "10"},
        "execution_mode": "paper",
        "started_at": STARTED,
        "saved_at": STARTED + timedelta(hours=1),
        "restarts": 0,
        "symbols": (SYMBOL,),
        "timeframe": "1h",
        "last_bar": make_bar(close=Decimal("60000")),
        "bars_processed": 5,
        "required_history": 21,
        "quote_asset": "USDT",
        "starting_capital": Decimal("10000"),
        "cash": Decimal("10000"),
        "equity": Decimal("10000"),
        "realized_pnl": Decimal(0),
        "unrealized_pnl": Decimal(0),
        "total_fees": Decimal(0),
        "open_positions": (),
        "position_risk": (),
        "risk_v2_active": True,
        "stop_required": True,
        "risk_per_trade_pct": Decimal("0.01"),
        "breakers": (),
        "report": None,
        "notes": (),
        "state_present": True,
        "lock": None,
    }
    base.update(overrides)
    return SessionStatus(**base)  # type: ignore[arg-type]


def _brains(
    status: SessionStatus, activity: ActivityCounts | None = None, **kwargs: object
) -> tuple[Brain, ...]:
    return build_brains(
        status,
        activity or ActivityCounts(),
        **kwargs,  # type: ignore[arg-type]
    )


def _brain(brains: tuple[Brain, ...], key: str) -> Brain:
    return next(brain for brain in brains if brain.key == key)


def _kpi(brain: Brain, key: str) -> Kpi:
    return next(kpi for kpi in brain.kpis if kpi.key == key)


# --- interpretation, not measurement -------------------------------------------------------


def test_a_warming_up_strategy_says_it_cannot_signal_yet() -> None:
    brains = _brains(_status(bars_processed=9))

    strategy = _brain(brains, "strategy")

    assert strategy.headline == "WARMING UP — 9/21 bars"
    assert strategy.explanation == "Strategy cannot generate valid signals yet."


def test_a_warmed_up_flat_strategy_reads_ready() -> None:
    brains = _brains(_status(bars_processed=21))

    strategy = _brain(brains, "strategy")

    assert strategy.headline == "READY"
    assert strategy.level is Level.GOOD


def test_a_strategy_holding_a_position_says_so() -> None:
    brains = _brains(_status(bars_processed=30, open_positions=(_position(),)))

    assert _brain(brains, "strategy").headline == "IN POSITION"


def test_no_signals_during_warmup_is_explained_as_correct_not_as_a_fault() -> None:
    brains = _brains(_status(bars_processed=5), ActivityCounts(signals=0, bars_seen=5))

    why = _kpi(_brain(brains, "strategy"), "warmup").why

    assert "structurally unable to signal" in why
    assert "not a fault" in why


def _position() -> Position:
    opened = datetime.now(UTC) - timedelta(hours=2)
    return Position(
        symbol=SYMBOL,
        base_asset="BTC",
        quote_asset="USDT",
        quantity=Decimal("0.1"),
        avg_entry_price=Decimal("59000"),
        realized_pnl=Decimal(0),
        fees_paid=Decimal(0),
        opened_at=opened,
        updated_at=opened,
    )


def _risk_state() -> PositionRiskState:
    opened = datetime.now(UTC) - timedelta(hours=2)
    return PositionRiskState(
        symbol=SYMBOL,
        stop=StopSpecification(kind=StopKind.TRAILING, trigger_price=Decimal("57230")),
        quantity=Decimal("0.1"),
        initial_risk_amount=Decimal("177"),
        current_risk_amount=Decimal("177"),
        entry_price=Decimal("59000"),
        opened_at=opened,
    )


# --- nothing is invented -------------------------------------------------------------------


def test_a_kpi_with_no_source_carries_no_value_and_says_why() -> None:
    brains = _brains(_status())

    profit_factor = _kpi(_brain(brains, "strategy"), "profit_factor")

    assert profit_factor.value is None
    assert profit_factor.source is Source.UNAVAILABLE
    assert "daily report" in profit_factor.why


def test_unobservable_stop_movements_are_explained_rather_than_guessed() -> None:
    # Trailing and break-even only move the trigger fed into one stop check, which records
    # a single reason for all of them. Any number here would be fabricated.
    brains = _brains(_status())
    risk = _brain(brains, "risk")

    for key in ("trailing", "break_even"):
        kpi = _kpi(risk, key)
        assert kpi.value is None
        assert kpi.source is Source.UNAVAILABLE
        assert "indistinguishable" in kpi.why


def test_regime_is_declared_absent_rather_than_approximated() -> None:
    regime = _kpi(_brain(_brains(_status()), "market"), "regime")

    assert regime.value is None
    assert "does not classify market regime" in regime.why


def test_latency_is_absent_because_paper_execution_has_no_venue_round_trip() -> None:
    latency = _kpi(_brain(_brains(_status()), "execution"), "latency")

    assert latency.value is None
    assert "simulated locally" in latency.why


def test_every_kpi_without_a_value_is_marked_unavailable() -> None:
    # The invariant that keeps "unknown" from ever looking like a measurement.
    for brain in _brains(_status()):
        for kpi in brain.kpis:
            if kpi.value is None:
                assert kpi.source is Source.UNAVAILABLE, f"{brain.key}.{kpi.key}"
                assert kpi.why, f"{brain.key}.{kpi.key} has no reason"


def test_every_kpi_explains_itself() -> None:
    for brain in _brains(_status()):
        for kpi in brain.kpis:
            assert kpi.meaning.strip(), f"{brain.key}.{kpi.key} has no meaning"
            assert kpi.why.strip(), f"{brain.key}.{kpi.key} has no why"


# --- zero is not unknown -------------------------------------------------------------------


def test_a_real_zero_is_reported_as_a_measurement() -> None:
    brains = _brains(_status(), ActivityCounts(signals=0, intents=0, fills=0, bars_seen=4))

    signals = _kpi(_brain(brains, "strategy"), "signals")

    assert signals.value == "0"
    assert signals.source is Source.LOG_DERIVED


def test_an_absent_count_is_not_reported_as_zero() -> None:
    brains = _brains(_status(), ActivityCounts())

    assert _kpi(_brain(brains, "strategy"), "signals").value is None


# --- low confidence ------------------------------------------------------------------------


def test_a_thin_sample_is_flagged_rather_than_presented_as_performance() -> None:
    status = _status(report=_report(trade_count=2, win_rate=Decimal("0.5")))

    win_rate = _kpi(_brain(_brains(status), "strategy"), "win_rate")

    assert win_rate.value == "50.00%"
    assert win_rate.low_confidence is True
    assert "not evidence" in win_rate.why


def test_a_full_sample_is_not_flagged() -> None:
    status = _status(report=_report(trade_count=MEANINGFUL_TRADE_SAMPLE, win_rate=Decimal("0.5")))

    assert _kpi(_brain(_brains(status), "strategy"), "win_rate").low_confidence is False


def test_zero_trades_yields_no_performance_figure_at_all() -> None:
    status = _status(report=_report(trade_count=0))

    win_rate = _kpi(_brain(_brains(status), "strategy"), "win_rate")

    assert win_rate.value is None
    assert win_rate.low_confidence is False


def _report(**stats: object) -> DailyReport:
    base: dict[str, object] = {
        "opening_equity": Decimal("10000"),
        "daily_equity": Decimal("10000"),
        "daily_pnl": Decimal(0),
    }
    base.update(stats)
    return DailyReport(
        session_id="s",
        strategy_id="breakout",
        day=datetime.now(UTC).date(),
        generated_at=datetime.now(UTC),
        quote_asset="USDT",
        timezone="UTC",
        statistics=DailyStatistics(**base),  # type: ignore[arg-type]
        health=DailyHealth(level=HealthLevel.GREEN),
        alerts=DailyAlerts(),
        summary=DailySummary(headline="ok", profit_line="ok", health_line="ok"),
    )


# --- risk reads as risk --------------------------------------------------------------------


def test_an_armed_risk_engine_says_no_breaker_triggered() -> None:
    risk = _brain(_brains(_status()), "risk")

    assert risk.headline == "ARMED"
    assert risk.level is Level.GOOD
    assert "No breaker triggered" in risk.explanation
    assert "Current exposure: 0%" in risk.explanation


def test_all_three_breakers_are_listed_even_when_none_fired() -> None:
    # A panel that only shows what tripped cannot be used to confirm nothing has.
    risk = _brain(_brains(_status()), "risk")

    labels = [kpi.label for kpi in risk.kpis if kpi.key.startswith("breaker_")]
    assert labels == ["Breaker · Daily Loss", "Breaker · Total Drawdown", "Breaker · Loss Streak"]
    assert all(kpi.value == "OK" for kpi in risk.kpis if kpi.key.startswith("breaker_"))


def test_a_tripped_breaker_halts_and_says_it_will_not_clear_itself() -> None:
    breaker = CircuitBreakerState(
        tripped_at=datetime.now(UTC),
        reason=CircuitBreakerReason.DAILY_LOSS_LIMIT,
        consecutive_losses=0,
        daily_loss=Decimal("310"),
    )
    risk = _brain(_brains(_status(breakers=(breaker,))), "risk")

    assert risk.headline == "TRADING HALTED"
    assert risk.level is Level.DANGER
    assert "will not clear itself" in risk.explanation
    assert _kpi(risk, "breaker_daily_loss_limit").value == "TRIGGERED"


def test_an_unprotected_position_is_the_loudest_thing_on_the_risk_panel() -> None:
    status = _status(open_positions=(_position(),), position_risk=())
    risk = _brain(_brains(status), "risk")

    coverage = _kpi(risk, "stop_coverage")
    assert coverage.value == "UNPROTECTED"
    assert coverage.level is Level.DANGER
    assert risk.headline == "ATTENTION REQUIRED"


def test_a_protected_position_reports_full_coverage() -> None:
    status = _status(open_positions=(_position(),), position_risk=(_risk_state(),))

    coverage = _kpi(_brain(_brains(status), "risk"), "stop_coverage")

    assert coverage.value == "Fully covered"
    assert coverage.level is Level.GOOD


def test_a_flat_session_reports_zero_exposure_rather_than_unknown() -> None:
    exposure = _kpi(_brain(_brains(_status()), "risk"), "exposure")

    assert exposure.value == "0.00%"
    assert exposure.source is Source.STRUCTURED


# --- candles expected versus received ------------------------------------------------------


def test_a_session_that_missed_candles_is_flagged() -> None:
    # Six hours in with one candle processed: five hourly closes went unhandled.
    status = _status(bars_processed=1)

    coverage = _kpi(_brain(_brains(status), "market"), "bar_coverage")

    assert coverage.level is Level.ATTENTION
    assert "have not been processed" in coverage.why


def test_a_session_keeping_up_with_the_feed_is_not_flagged() -> None:
    status = _status(started_at=datetime.now(UTC) - timedelta(minutes=20), bars_processed=0)

    coverage = _kpi(_brain(_brains(status), "market"), "bar_coverage")

    assert coverage.level is Level.GOOD


# --- the summary cannot outrun the panels --------------------------------------------------


def test_the_summary_reports_healthy_when_every_panel_agrees() -> None:
    status = _status(bars_processed=21)
    brains = _brains(status, feed_state="streaming")

    summary = summarise(status, brains)

    assert summary.headline == "HEALTHY"
    assert summary.intervention_required is False
    assert "No intervention required." in summary.sentences
    assert summary.blockers == ()


def test_the_summary_demands_intervention_when_a_breaker_fired() -> None:
    breaker = CircuitBreakerState(
        tripped_at=datetime.now(UTC),
        reason=CircuitBreakerReason.DAILY_LOSS_LIMIT,
        consecutive_losses=0,
        daily_loss=Decimal("310"),
    )
    status = _status(breakers=(breaker,), health=Health.FAILED)
    brains = _brains(status, feed_state="streaming")

    summary = summarise(status, brains)

    assert summary.headline == "ATTENTION REQUIRED"
    assert summary.intervention_required is True
    assert any("breaker" in blocker.lower() for blocker in summary.blockers)


def test_a_stopped_session_demands_intervention() -> None:
    status = _status(running=False, health=Health.STOPPED)

    summary = summarise(status, _brains(status))

    assert summary.intervention_required is True
    assert any("not running" in blocker for blocker in summary.blockers)


def test_the_summary_describes_warmup_when_that_is_the_state() -> None:
    status = _status(bars_processed=9)

    summary = summarise(status, _brains(status, feed_state="streaming"))

    assert any("warming up" in sentence.lower() for sentence in summary.sentences)
    assert "9/21 bars" in " ".join(summary.sentences)


def test_the_summary_says_nothing_the_panels_do_not() -> None:
    # Every sentence must correspond to a finding, so the paragraph at the top cannot
    # assert something the cards below contradict.
    status = _status(bars_processed=21)
    brains = _brains(status, feed_state="streaming")

    summary = summarise(status, brains)

    assert "Risk engine fully armed." in summary.sentences
    assert _brain(brains, "risk").level is Level.GOOD
    assert "No positions open." in summary.sentences
    assert _brain(brains, "portfolio").headline == "FLAT"
