"""Phase 6 unit tests: session clock, state persistence and the runner loop."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import ExecutionMode
from quantplatform.core.errors import PaperSessionStateError
from quantplatform.core.interfaces import PaperMarketDataFeed, PaperStateRepository
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.telemetry import ZERO_FEED_METRICS, FeedMetricsSnapshot
from quantplatform.paper.clock import SessionClock
from quantplatform.paper.results import RuntimeMetrics, SessionStatus
from quantplatform.paper.state import InMemoryPaperStateRepository, restore_balances
from tests.factories import ANCHOR, SYMBOL, make_balance, make_bar, make_position

# --- Session clock ---------------------------------------------------------------------------


def test_the_session_clock_reads_only_the_injected_clock() -> None:
    clock = SimulatedClock(ANCHOR)
    session_clock = SessionClock(clock)

    assert session_clock.now() == ANCHOR
    clock.advance(timedelta(hours=2))
    assert session_clock.now() == ANCHOR + timedelta(hours=2)


def test_the_start_instant_is_recorded_once_and_survives_restarts() -> None:
    clock = SimulatedClock(ANCHOR)
    session_clock = SessionClock(clock)

    first = session_clock.mark_start()
    clock.advance(timedelta(hours=5))
    second = session_clock.mark_start()

    assert first == second == ANCHOR


def test_a_recovered_start_instant_can_be_adopted() -> None:
    clock = SimulatedClock(ANCHOR + timedelta(days=1))
    session_clock = SessionClock(clock)

    session_clock.adopt_start(ANCHOR)

    assert session_clock.started_at == ANCHOR


def test_uptime_is_measured_monotonically_not_by_subtracting_timestamps() -> None:
    clock = SimulatedClock(ANCHOR)
    session_clock = SessionClock(clock)
    session_clock.mark_start()

    clock.advance(timedelta(seconds=90))

    assert session_clock.uptime_seconds() == pytest.approx(90.0)


def test_uptime_is_zero_before_the_session_starts() -> None:
    assert SessionClock(SimulatedClock(ANCHOR)).uptime_seconds() == 0.0


def test_a_bar_is_not_final_until_the_clock_passes_its_close() -> None:
    clock = SimulatedClock(ANCHOR)
    session_clock = SessionClock(clock)
    bar = make_bar(index=0)

    assert session_clock.is_bar_final(bar) is False
    assert session_clock.seconds_until_final(bar) == 3_600.0

    clock.set_time(bar.close_time)

    assert session_clock.is_bar_final(bar) is True
    assert session_clock.seconds_until_final(bar) == 0.0


def test_the_grace_period_forgives_a_clock_running_behind_the_venue() -> None:
    # One platform-wide rule: `now >= close_time - grace`. The grace is subtracted, so a
    # candle the venue has already closed is accepted even if our clock lags. Adding it
    # instead rejected every live candle for ever, because a venue publishes each closed
    # candle once and a refusal never advances the anchor.
    clock = SimulatedClock(ANCHOR)
    session_clock = SessionClock(clock, close_grace_seconds=30)
    bar = make_bar(index=0)

    clock.set_time(bar.close_time - timedelta(seconds=30))
    assert session_clock.is_bar_final(bar) is True
    assert session_clock.seconds_until_final(bar) == 0.0


def test_a_candle_arriving_before_the_tolerance_window_is_still_refused() -> None:
    # Grace widens the window; it does not remove it.
    clock = SimulatedClock(ANCHOR)
    session_clock = SessionClock(clock, close_grace_seconds=30)
    bar = make_bar(index=0)

    clock.set_time(bar.close_time - timedelta(seconds=31))

    assert session_clock.is_bar_final(bar) is False
    assert session_clock.seconds_until_final(bar) == pytest.approx(1.0)


def test_raising_the_grace_can_never_hide_a_valid_candle() -> None:
    # The regression that mattered: a larger tolerance must only ever accept more, never
    # fewer. Any grace at which a venue-closed candle stops being final is a broken rule.
    bar = make_bar(index=0)
    for grace in (0, 1, 2, 5, 30, 3_600):
        clock = SimulatedClock(ANCHOR)
        clock.set_time(bar.close_time)
        assert SessionClock(clock, close_grace_seconds=grace).is_bar_final(bar) is True


# --- State repository -------------------------------------------------------------------------


def _state(**overrides: object) -> PaperSessionState:
    defaults: dict[str, object] = {
        "session_id": "session-1",
        "strategy_id": "silent",
        "execution_mode": ExecutionMode.PAPER,
        "quote_asset": "USDT",
        "started_at": ANCHOR,
        "saved_at": ANCHOR + timedelta(hours=1),
        "balances": (make_balance(free=Decimal(10_000)),),
    }
    return PaperSessionState(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_the_in_memory_repository_satisfies_the_port() -> None:
    assert isinstance(InMemoryPaperStateRepository(), PaperStateRepository)


def test_a_saved_state_can_be_loaded_back() -> None:
    repository = InMemoryPaperStateRepository()
    state = _state()

    repository.save(state)

    assert repository.load("session-1") == state
    assert len(repository) == 1


def test_loading_an_unknown_session_returns_none() -> None:
    assert InMemoryPaperStateRepository().load("nope") is None


def test_saving_twice_replaces_rather_than_duplicates() -> None:
    repository = InMemoryPaperStateRepository()
    repository.save(_state())
    repository.save(_state(bars_processed=3, last_bar=make_bar(index=0)))

    stored = repository.load("session-1")
    assert stored is not None
    assert stored.bars_processed == 3
    assert len(repository) == 1


def test_deleting_is_safe_when_nothing_was_stored() -> None:
    repository = InMemoryPaperStateRepository()
    repository.delete("nope")
    assert len(repository) == 0


def test_state_refuses_a_snapshot_taken_before_the_session_started() -> None:
    with pytest.raises(ValueError, match="must not precede"):
        _state(saved_at=ANCHOR - timedelta(hours=1))


def test_state_refuses_processed_bars_without_recording_the_last_one() -> None:
    with pytest.raises(ValueError, match="must record the last one"):
        _state(bars_processed=5)


def test_state_refuses_an_unclosed_last_bar() -> None:
    with pytest.raises(ValueError, match="must be closed"):
        _state(bars_processed=1, last_bar=make_bar(index=0, is_closed=False))


def test_restoring_balances_keeps_only_the_quote_asset() -> None:
    state = _state(
        balances=(
            make_balance(asset="USDT", free=Decimal(10_000)),
            make_balance(asset="BTC", free=Decimal(0)),
        )
    )
    restored = restore_balances(state)

    assert [balance.asset for balance in restored] == ["USDT"]


def test_restoring_refuses_a_snapshot_holding_an_open_position() -> None:
    # The portfolio engine is flat-start by construction; seeding an open position would
    # bypass the invariant keeping a position and its base balance in lockstep.
    state = _state(
        bars_processed=1,
        last_bar=make_bar(index=0),
        positions=(make_position(quantity=Decimal("0.5")),),
    )

    with pytest.raises(PaperSessionStateError, match="flat-start"):
        restore_balances(state)


# --- Runtime metrics ---------------------------------------------------------------------------


def test_acceptance_rate_is_undefined_before_any_bar_arrives() -> None:
    assert RuntimeMetrics().acceptance_rate is None


def test_acceptance_rate_reports_the_share_that_reached_the_pipeline() -> None:
    metrics = RuntimeMetrics(bars_received=4, bars_processed=3, bars_rejected=1)
    assert metrics.acceptance_rate == Decimal("0.75")


def test_session_status_knows_whether_it_ever_started() -> None:
    assert (
        SessionStatus(running=False, started_at=None, stopped_at=None, restarts=0).has_started
        is False
    )
    assert (
        SessionStatus(running=True, started_at=ANCHOR, stopped_at=None, restarts=0).has_started
        is True
    )


# --- Feed port ----------------------------------------------------------------------------------


class _RecordedFeed:
    """A feed double that replays a fixed list of bars."""

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        self._bars = bars
        self.closed = False

    @property
    def symbols(self) -> tuple[str, ...]:
        return (SYMBOL,)

    def closed_bars(self) -> Iterator[MarketBar]:
        yield from self._bars

    def close(self) -> None:
        self.closed = True


def test_a_feed_double_satisfies_the_port() -> None:
    assert isinstance(_RecordedFeed(()), PaperMarketDataFeed)


# --- Telemetry baseline -------------------------------------------------------------------------


def test_state_carries_no_feed_baseline_until_a_day_is_reported() -> None:
    assert _state().feed_baseline is None


def test_a_stored_feed_baseline_survives_the_repository() -> None:
    # The path a restart actually takes. A JSON round trip of the whole state is not
    # exercised here because `Balance` publishes a computed `total` that dumps but is not
    # an input field — a pre-existing property of the portfolio model, unrelated to
    # telemetry, and the repository port stores objects rather than text.
    baseline = FeedMetricsSnapshot(
        reconnect_count=3, detected_gaps=1, candles_received=90, candles_accepted=88
    )
    repository = InMemoryPaperStateRepository()

    repository.save(_state(feed_baseline=baseline))

    restored = repository.load("session-1")
    assert restored is not None
    assert restored.feed_baseline == baseline


def test_a_feed_baseline_round_trips_through_json_on_its_own() -> None:
    baseline = FeedMetricsSnapshot(reconnect_count=3, candles_received=9, candles_accepted=9)

    assert FeedMetricsSnapshot.model_validate_json(baseline.model_dump_json()) == baseline


def test_the_baseline_field_is_frozen_like_the_rest_of_the_state() -> None:
    stored = _state(feed_baseline=ZERO_FEED_METRICS)

    with pytest.raises(ValueError, match="frozen"):
        stored.feed_baseline = None  # type: ignore[misc]
