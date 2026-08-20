"""Phase 7A unit tests: configuration, backoff, feed clock, parsing and sequence rules.

Everything here runs against a simulated clock and literal frames. Nothing opens a socket,
which is the point of the transport port: a dropped connection and a fifteen-minute hole in
the candle series are ordinary test inputs rather than things that have to be arranged with
a real venue.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import DataQualityIssue, MarketType, Timeframe
from quantplatform.core.errors import (
    DataIntegrityError,
    DataProviderError,
    DomainValidationError,
    MarketDataSubscriptionError,
    OutOfOrderDataError,
)
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.marketdata.clock import FeedClock
from quantplatform.marketdata.config import MarketDataConfiguration
from quantplatform.marketdata.models import CandleOutcome, FeedMetrics, GapReport
from quantplatform.marketdata.reconnect import BackoffSchedule, ReconnectPolicy
from quantplatform.marketdata.validation import CandleParser, CandleSequenceValidator
from tests.factories import ANCHOR, SYMBOL, make_bar, make_kline_frame, make_subscription_ack

_VENUE_SYMBOL = "BTCUSDT"


def _parser(**overrides: object) -> CandleParser:
    defaults: dict[str, object] = {
        "symbols": {_VENUE_SYMBOL: SYMBOL},
        "timeframe": Timeframe.H1,
        "market_type": MarketType.SPOT,
        "source": "binance_spot_ws",
    }
    return CandleParser(**{**defaults, **overrides})  # type: ignore[arg-type]


def _sequence(clock: SimulatedClock, *, grace: float = 0.0) -> CandleSequenceValidator:
    return CandleSequenceValidator(
        timeframe=Timeframe.H1, clock=FeedClock(clock, close_grace_seconds=grace)
    )


# --- Feed clock -------------------------------------------------------------------------------


def test_the_feed_clock_reads_only_the_injected_clock() -> None:
    clock = SimulatedClock(ANCHOR)
    feed_clock = FeedClock(clock)

    assert feed_clock.now() == ANCHOR
    clock.advance(timedelta(hours=3))
    assert feed_clock.now() == ANCHOR + timedelta(hours=3)


def test_silence_is_measured_only_after_the_first_frame() -> None:
    clock = SimulatedClock(ANCHOR)
    feed_clock = FeedClock(clock)

    assert feed_clock.seconds_since_frame() is None
    # A connection that has not yet spoken is not a dead one; reconnecting from here would
    # loop against a venue that is merely slow to send its first update.
    assert feed_clock.is_heartbeat_expired(1.0) is False

    feed_clock.mark_frame()
    clock.advance(timedelta(seconds=30))

    assert feed_clock.seconds_since_frame() == pytest.approx(30.0)


def test_a_silent_stream_expires_its_heartbeat() -> None:
    clock = SimulatedClock(ANCHOR)
    feed_clock = FeedClock(clock)
    feed_clock.mark_frame()

    clock.advance(timedelta(seconds=59))
    assert feed_clock.is_heartbeat_expired(60.0) is False

    clock.advance(timedelta(seconds=1))
    assert feed_clock.is_heartbeat_expired(60.0) is True


def test_forgetting_frames_stops_the_heartbeat_window() -> None:
    clock = SimulatedClock(ANCHOR)
    feed_clock = FeedClock(clock)
    feed_clock.mark_frame()
    clock.advance(timedelta(hours=1))

    feed_clock.forget_frames()

    assert feed_clock.seconds_since_frame() is None
    assert feed_clock.is_heartbeat_expired(1.0) is False


def test_a_candle_is_not_final_until_the_clock_reaches_its_close() -> None:
    clock = SimulatedClock(ANCHOR)
    feed_clock = FeedClock(clock)
    bar = make_bar(index=0)

    assert feed_clock.is_bar_final(bar) is False
    assert feed_clock.seconds_until_final(bar) == 3_600.0

    clock.set_time(bar.close_time)

    assert feed_clock.is_bar_final(bar) is True
    assert feed_clock.seconds_until_final(bar) == 0.0


def test_the_grace_period_forgives_our_clock_running_behind_the_venue() -> None:
    # The direction matters. A venue publishes each closed candle once; demanding that our
    # clock pass close + grace would drop a candle that arrived promptly and then report the
    # hole we just made as a gap. Subtracting only ever admits a candle the venue has
    # already declared closed.
    clock = SimulatedClock(ANCHOR)
    feed_clock = FeedClock(clock, close_grace_seconds=5.0)
    bar = make_bar(index=0)

    clock.set_time(bar.close_time - timedelta(seconds=3))

    assert feed_clock.is_bar_final(bar) is True


# --- Backoff schedule -------------------------------------------------------------------------


def test_delays_grow_exponentially_and_stop_at_the_ceiling() -> None:
    schedule = BackoffSchedule(
        initial_delay_seconds=1.0, max_delay_seconds=8.0, multiplier=2.0, max_attempts=6
    )

    assert [schedule.delay_for(attempt) for attempt in range(1, 7)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        8.0,
        8.0,
    ]


def test_a_multiplier_of_one_gives_a_constant_delay() -> None:
    schedule = BackoffSchedule(initial_delay_seconds=2.5, multiplier=1.0)

    assert schedule.delay_for(1) == schedule.delay_for(9) == 2.5


def test_the_schedule_reports_whether_an_attempt_is_within_budget() -> None:
    schedule = BackoffSchedule(max_attempts=3)

    assert schedule.permits(1) is True
    assert schedule.permits(3) is True
    assert schedule.permits(4) is False
    assert schedule.permits(0) is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"initial_delay_seconds": 0.0}, "strictly positive"),
        ({"multiplier": 0.5}, "at least 1"),
        ({"initial_delay_seconds": 10.0, "max_delay_seconds": 5.0}, "must not be below"),
        ({"max_attempts": 0}, "at least 1"),
    ],
)
def test_an_unusable_schedule_is_refused(overrides: dict[str, float], message: str) -> None:
    with pytest.raises(DomainValidationError, match=message):
        BackoffSchedule(**overrides)  # type: ignore[arg-type]


def test_asking_for_a_zeroth_attempt_is_refused() -> None:
    with pytest.raises(DomainValidationError, match="at least 1"):
        BackoffSchedule().delay_for(0)


def test_the_policy_keeps_handing_out_delays_past_the_nominal_budget() -> None:
    # A short-lived outage and a days-long one look the same for the first few attempts;
    # what must never happen is the policy refusing to keep going once the nominal budget
    # (still used for the *is_exhausted* signal) is spent. Retrying forever is the point —
    # DataGapError, the watchdog and an operator are what may end a session now, not this.
    policy = ReconnectPolicy(
        BackoffSchedule(
            initial_delay_seconds=1.0, max_delay_seconds=4.0, multiplier=2.0, max_attempts=3
        )
    )

    assert [policy.next_delay() for _ in range(3)] == [1.0, 2.0, 4.0]
    assert policy.attempts == 3
    assert policy.is_exhausted is True

    # Past the nominal budget: no exception, delay stays clamped at the ceiling.
    assert policy.next_delay() == 4.0
    assert policy.next_delay() == 4.0
    assert policy.attempts == 5


def test_delay_for_does_not_overflow_after_thousands_of_attempts() -> None:
    # An outage lasting long enough for attempt numbers to reach the thousands must not
    # crash the process computing a delay curve whose only use, at that point, is to be
    # clamped away by the ceiling regardless.
    schedule = BackoffSchedule(initial_delay_seconds=1.0, max_delay_seconds=60.0, multiplier=2.0)

    assert schedule.delay_for(2_000) == 60.0
    assert schedule.delay_for(100_000) == 60.0


def test_a_proven_connection_restores_the_full_retry_budget() -> None:
    policy = ReconnectPolicy(BackoffSchedule(max_attempts=2))
    policy.next_delay()
    policy.next_delay()

    policy.reset()

    assert policy.attempts == 0
    assert policy.is_exhausted is False


def test_the_policy_defaults_to_a_conservative_schedule() -> None:
    assert ReconnectPolicy().schedule == BackoffSchedule()


# --- Configuration ----------------------------------------------------------------------------


def test_the_default_configuration_describes_a_public_binance_spot_stream() -> None:
    config = MarketDataConfiguration()

    assert config.websocket_url.startswith("wss://")
    assert config.symbols == (SYMBOL,)
    assert config.market_type is MarketType.SPOT
    assert config.venue_symbol(SYMBOL) == _VENUE_SYMBOL
    assert config.stream_name(SYMBOL) == "btcusdt@kline_1h"


def test_configuration_is_frozen() -> None:
    config = MarketDataConfiguration()

    with pytest.raises(ValueError, match="frozen"):
        config.websocket_url = "wss://elsewhere"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://stream.binance.com/ws", "must use ws or wss"),
        ("wss:///ws", "must name a host"),
        ("wss://user:pass@stream.binance.com/ws", "must not embed credentials"),
        ("wss://stream.binance.com/ws?timestamp=1", "must not carry a query string"),
        ("wss://stream.binance.com/ws/listenKey", "listenkey"),
        ("wss://stream.binance.com/api/v3/ws", "/api/"),
        ("wss://stream.binance.com/ws/userDataStream", "userdatastream"),
    ],
)
def test_an_endpoint_that_is_not_a_public_market_stream_is_refused(url: str, message: str) -> None:
    # This is the one place a config edit could turn a read-only component into something
    # else, so the check is a safety boundary rather than tidiness.
    with pytest.raises(ValueError, match=message):
        MarketDataConfiguration(websocket_url=url)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbols": ()}, "at least one symbol"),
        ({"symbols": (SYMBOL, SYMBOL)}, "must not repeat"),
        ({"market_type": MarketType.FUTURES}, "only spot"),
        ({"heartbeat_timeout_seconds": 5.0, "receive_timeout_seconds": 5.0}, "must exceed"),
        (
            {"reconnect_initial_delay_seconds": 30.0, "reconnect_max_delay_seconds": 10.0},
            "must not be below",
        ),
    ],
)
def test_an_incoherent_configuration_is_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MarketDataConfiguration(**overrides)  # type: ignore[arg-type]


def test_configuration_rejects_unknown_fields() -> None:
    # Notably including a credential field: configuration cannot smuggle one in.
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        MarketDataConfiguration(api_key="secret")  # type: ignore[call-arg]


# --- Parsing ----------------------------------------------------------------------------------


def test_a_well_formed_frame_becomes_the_bar_it_describes() -> None:
    expected = make_bar(index=3, close=Decimal(51_000), open_price=Decimal(50_000))

    parsed = _parser().parse(make_kline_frame(expected))

    assert parsed is not None
    assert parsed.symbol == SYMBOL
    assert parsed.open_time == expected.open_time
    assert parsed.close_time == expected.close_time
    assert (parsed.open, parsed.high, parsed.low, parsed.close) == (
        expected.open,
        expected.high,
        expected.low,
        expected.close,
    )
    assert parsed.volume == expected.volume
    assert parsed.is_closed is True
    assert parsed.source == "binance_spot_ws"
    assert parsed.market_type is MarketType.SPOT


def test_the_combined_stream_envelope_is_unwrapped() -> None:
    bar = make_bar(index=1)

    parsed = _parser().parse(make_kline_frame(bar, wrapped=True))

    assert parsed is not None
    assert parsed.open_time == bar.open_time


def test_a_forming_candle_is_parsed_and_reported_as_open() -> None:
    parsed = _parser().parse(make_kline_frame(make_bar(index=0), is_closed=False))

    assert parsed is not None
    assert parsed.is_closed is False


@pytest.mark.parametrize(
    "frame",
    [
        make_subscription_ack(),
        json.dumps({"result": ["btcusdt@kline_1h"], "id": 7}),
        json.dumps({"e": "aggTrade", "s": _VENUE_SYMBOL, "p": "50000"}),
        json.dumps({"ping": 1}),
    ],
    ids=["ack", "list-ack", "other-event", "ping"],
)
def test_a_frame_carrying_no_candle_is_not_an_error(frame: str) -> None:
    # Acknowledgements and unrelated events are ordinary traffic. A feed that died on them
    # would not survive its own subscription.
    assert _parser().parse(frame) is None


@pytest.mark.parametrize(
    "frame",
    ["not json at all", "{", '"a string"', "[1, 2, 3]"],
    ids=["text", "truncated", "str", "list"],
)
def test_a_frame_that_is_not_a_json_object_is_a_provider_failure(frame: str) -> None:
    with pytest.raises(DataProviderError):
        _parser().parse(frame)


def test_a_candle_missing_required_fields_is_rejected() -> None:
    frame = make_kline_frame(make_bar(index=0), drop=("o", "v"))

    with pytest.raises(DataIntegrityError, match="missing required fields") as caught:
        _parser().parse(frame)
    assert caught.value.details["missing"] == ["o", "v"]


def test_a_candle_for_an_unsubscribed_instrument_is_a_wiring_fault() -> None:
    frame = make_kline_frame(make_bar(index=0), venue_symbol="ETHUSDT")

    with pytest.raises(MarketDataSubscriptionError, match="never subscribed"):
        _parser().parse(frame)


def test_a_candle_at_the_wrong_interval_is_rejected() -> None:
    frame = make_kline_frame(make_bar(index=0), overrides={"i": "15m"})

    with pytest.raises(DataIntegrityError, match="interval does not match") as caught:
        _parser().parse(frame)
    assert caught.value.details["issue"] == DataQualityIssue.UNEXPECTED_TIMEFRAME.value


def test_a_candle_whose_duration_contradicts_its_timeframe_is_rejected() -> None:
    # The venue's own close timestamp is never trusted to build the bar, but it is checked:
    # a mismatch means the frame does not span the interval that was subscribed to.
    bar = make_bar(index=0)
    frame = make_kline_frame(bar, overrides={"T": 1_700_000_000_000})

    with pytest.raises(DataIntegrityError, match="duration does not match"):
        _parser().parse(frame)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"v": "-1"}, "failed domain validation"),
        ({"o": "0", "h": "0", "l": "0", "c": "0"}, "failed domain validation"),
        ({"c": "-50000"}, "failed domain validation"),
        ({"h": "1", "l": "99999"}, "failed domain validation"),
        ({"c": "not-a-number"}, "not a valid decimal"),
        ({"o": 50000.5}, "not a valid decimal"),
        ({"t": "1700000000000"}, "timestamp is not an integer"),
        ({"x": "true"}, "closed flag is not a boolean"),
        ({"n": -4}, "not a non-negative integer"),
        ({"T": True}, "timestamp is not an integer"),
    ],
    ids=[
        "negative-volume",
        "zero-price",
        "negative-price",
        "inverted-high-low",
        "non-numeric",
        "binary-float",
        "string-timestamp",
        "string-flag",
        "negative-trade-count",
        "bool-timestamp",
    ],
)
def test_an_impossible_candle_raises_rather_than_being_repaired(
    overrides: dict[str, object], message: str
) -> None:
    # Nothing is coerced or clamped. A repaired candle is indistinguishable downstream from
    # a real one, and every decision made from it would be fiction that looks like history.
    frame = make_kline_frame(make_bar(index=0), overrides=overrides)

    with pytest.raises(DataIntegrityError, match=message):
        _parser().parse(frame)


def test_a_non_object_candle_field_is_rejected() -> None:
    frame = json.dumps({"e": "kline", "s": _VENUE_SYMBOL, "k": [1, 2, 3]})

    with pytest.raises(DataIntegrityError, match="not an object"):
        _parser().parse(frame)


def test_the_symbol_map_can_be_extended_and_trimmed() -> None:
    parser = _parser()
    frame = make_kline_frame(make_bar(index=0, symbol="ETH/USDT"), venue_symbol="ETHUSDT")

    parser.register(venue_symbol="ETHUSDT", symbol="ETH/USDT")
    parsed = parser.parse(frame)
    assert parsed is not None
    assert parsed.symbol == "ETH/USDT"

    parser.forget("ETHUSDT")
    with pytest.raises(MarketDataSubscriptionError):
        parser.parse(frame)
    assert set(parser.symbols) == {_VENUE_SYMBOL}


def test_forgetting_an_unknown_symbol_is_harmless() -> None:
    parser = _parser()
    parser.forget("NOPEUSDT")
    assert set(parser.symbols) == {_VENUE_SYMBOL}


def test_an_optional_quote_volume_may_be_absent() -> None:
    frame = make_kline_frame(make_bar(index=0), overrides={"q": None, "n": None})

    parsed = _parser().parse(frame)

    assert parsed is not None
    assert parsed.quote_volume is None
    assert parsed.trade_count is None


# --- Sequence rules ---------------------------------------------------------------------------


def test_the_first_closed_candle_is_accepted() -> None:
    clock = SimulatedClock(ANCHOR)
    bar = make_bar(index=0)
    clock.set_time(bar.close_time)

    admission = _sequence(clock).admit(bar)

    assert admission.outcome is CandleOutcome.ACCEPTED
    assert admission.is_deliverable is True


def test_a_candle_the_venue_has_not_closed_is_never_delivered() -> None:
    clock = SimulatedClock(ANCHOR)
    bar = make_bar(index=0, is_closed=False)
    clock.set_time(bar.close_time + timedelta(hours=5))

    assert _sequence(clock).admit(bar).outcome is CandleOutcome.FORMING


def test_a_candle_whose_interval_has_not_elapsed_is_never_delivered() -> None:
    # Both signals must agree. The venue's flag alone would trust a provider that mislabels
    # a forming candle; the clock alone would trust an aggregation window we cannot see into.
    clock = SimulatedClock(ANCHOR)

    assert _sequence(clock).admit(make_bar(index=0)).outcome is CandleOutcome.FORMING


def test_consecutive_candles_are_accepted_in_order() -> None:
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)

    for index in range(3):
        bar = make_bar(index=index)
        clock.set_time(bar.close_time)
        assert sequence.admit(bar).outcome is CandleOutcome.ACCEPTED

    last = sequence.last_accepted(SYMBOL)
    assert last is not None
    assert last.open_time == make_bar(index=2).open_time
    assert sequence.tracked_symbols == (SYMBOL,)


def test_a_republished_candle_is_suppressed_rather_than_traded_twice() -> None:
    # Expected after a reconnect: venues resend the most recent candle. Raising here would
    # make reconnection itself fatal.
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    bar = make_bar(index=0)
    clock.set_time(bar.close_time)
    sequence.admit(bar)

    assert sequence.admit(bar).outcome is CandleOutcome.DUPLICATE


def test_a_venue_revising_a_delivered_candle_is_refused() -> None:
    # Unlike a verbatim repeat, this cannot be reconciled by replacing the bar: the
    # decisions it produced have already happened.
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    original = make_bar(index=0, close=Decimal(50_000))
    clock.set_time(original.close_time)
    sequence.admit(original)

    revised = make_bar(index=0, close=Decimal(50_500))

    with pytest.raises(DataIntegrityError, match="revised a candle") as caught:
        sequence.admit(revised)
    assert caught.value.details["issue"] == DataQualityIssue.REVISED_BAR.value


def test_a_candle_that_predates_the_last_one_is_refused() -> None:
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    second = make_bar(index=1)
    clock.set_time(second.close_time)
    sequence.admit(second)

    with pytest.raises(OutOfOrderDataError, match="predates"):
        sequence.admit(make_bar(index=0))


def test_a_missing_candle_is_detected_and_described_exactly() -> None:
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    first = make_bar(index=0)
    clock.set_time(first.close_time)
    sequence.admit(first)

    later = make_bar(index=4)
    clock.set_time(later.close_time)
    admission = sequence.admit(later)

    assert admission.outcome is CandleOutcome.GAP
    assert admission.is_deliverable is False
    gap = admission.gap
    assert gap is not None
    assert gap.missing_bars == 3
    assert gap.expected_open_time == first.close_time
    assert gap.received_open_time == later.open_time
    assert gap.detected_at == later.close_time
    assert gap.issue is DataQualityIssue.MISSING_BAR
    assert "3 missing 1h candle(s)" in gap.describe()


def test_a_gap_does_not_move_the_continuity_anchor() -> None:
    # The hole is reported, not absorbed. Recording the later candle as the new anchor would
    # quietly convert a detected gap into an accepted one.
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    first = make_bar(index=0)
    clock.set_time(first.close_time)
    sequence.admit(first)

    later = make_bar(index=4)
    clock.set_time(later.close_time)
    sequence.admit(later)

    anchor = sequence.last_accepted(SYMBOL)
    assert anchor is not None
    assert anchor.open_time == first.open_time


def test_an_overlapping_candle_is_refused() -> None:
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    hourly = make_bar(index=0, timeframe=Timeframe.H1)
    clock.set_time(hourly.close_time + timedelta(hours=1))
    sequence.admit(hourly)

    overlapping = make_bar(index=1, timeframe=Timeframe.M15)

    with pytest.raises(DataIntegrityError, match="overlaps"):
        sequence.admit(overlapping)


def test_resynchronising_lets_the_next_candle_start_a_fresh_series() -> None:
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    first = make_bar(index=0)
    clock.set_time(first.close_time)
    sequence.admit(first)

    sequence.resynchronize(SYMBOL)
    assert sequence.last_accepted(SYMBOL) is None

    later = make_bar(index=9)
    clock.set_time(later.close_time)
    assert sequence.admit(later).outcome is CandleOutcome.ACCEPTED


def test_resynchronising_everything_clears_every_symbol() -> None:
    clock = SimulatedClock(ANCHOR)
    sequence = _sequence(clock)
    bar = make_bar(index=0)
    clock.set_time(bar.close_time)
    sequence.admit(bar)

    sequence.resynchronize()

    assert sequence.tracked_symbols == ()


# --- Metrics ----------------------------------------------------------------------------------


def test_metrics_are_replaced_rather_than_mutated() -> None:
    original = FeedMetrics()

    updated = original.record(bars_emitted=2, frames_received=5)

    assert original.bars_emitted == 0
    assert updated.bars_emitted == 2
    assert updated.frames_received == 5


def test_recording_an_unknown_metric_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown feed metric"):
        FeedMetrics().record(orders_submitted=1)


def test_acceptance_rate_is_undefined_before_any_candle_is_parsed() -> None:
    assert FeedMetrics().acceptance_rate is None


def test_acceptance_rate_reports_the_share_that_reached_the_pipeline() -> None:
    metrics = FeedMetrics(candles_parsed=4, bars_emitted=1, forming_suppressed=3)

    assert metrics.acceptance_rate == Decimal("0.25")
    assert metrics.total_suppressed == 3


def test_a_clean_run_is_one_with_no_gap_reconnect_or_heartbeat_loss() -> None:
    assert FeedMetrics(frames_received=100, bars_emitted=10).is_clean is True
    assert FeedMetrics(reconnects=1).is_clean is False
    assert FeedMetrics(gaps_detected=1).is_clean is False
    assert FeedMetrics(heartbeat_timeouts=1).is_clean is False


def test_a_gap_report_is_immutable() -> None:
    report = GapReport(
        symbol=SYMBOL,
        timeframe=Timeframe.H1,
        expected_open_time=ANCHOR,
        received_open_time=ANCHOR + timedelta(hours=2),
        missing_bars=2,
        detected_at=ANCHOR,
    )

    with pytest.raises((AttributeError, TypeError)):
        report.missing_bars = 0  # type: ignore[misc]


# --- Health snapshot ----------------------------------------------------------------------------


def test_metrics_map_onto_the_boundary_vocabulary() -> None:
    # The one place the feed's internal names become the operational names a report speaks.
    metrics = FeedMetrics(
        frames_received=100,
        control_frames=4,
        candles_parsed=90,
        bars_emitted=70,
        forming_suppressed=15,
        duplicates_suppressed=5,
        gaps_detected=2,
        heartbeat_timeouts=1,
        reconnects=3,
        malformed_frames=6,
    )

    snapshot = metrics.health_snapshot()

    assert snapshot.reconnect_count == 3
    assert snapshot.heartbeat_timeouts == 1
    assert snapshot.detected_gaps == 2
    assert snapshot.candles_received == 90
    assert snapshot.candles_accepted == 70
    assert snapshot.candles_rejected == 20
    assert snapshot.duplicate_candles == 5
    assert snapshot.malformed_frames == 6
    assert snapshot.rejected_frames == 26


def test_a_snapshot_of_an_untouched_feed_is_clean_and_undefined() -> None:
    snapshot = FeedMetrics().health_snapshot()

    assert snapshot.is_clean is True
    assert snapshot.acceptance_rate is None


def test_a_snapshot_reports_the_share_the_feed_delivered() -> None:
    snapshot = FeedMetrics(candles_parsed=8, bars_emitted=6, forming_suppressed=2).health_snapshot()

    assert snapshot.acceptance_rate == Decimal("0.75")


def test_a_malformed_frame_makes_a_snapshot_unclean() -> None:
    assert FeedMetrics(malformed_frames=1).health_snapshot().is_clean is False


def test_a_snapshot_is_frozen() -> None:
    snapshot = FeedMetrics().health_snapshot()

    with pytest.raises(ValueError, match="frozen"):
        snapshot.reconnect_count = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"candles_received": 1, "candles_accepted": 2}, "cannot exceed candles received"),
        ({"candles_received": 4, "candles_rejected": 3}, "cannot be fewer than rejected candles"),
        ({"rejected_frames": 1, "malformed_frames": 2}, "cannot exceed rejected frames"),
        (
            {
                "candles_received": 4,
                "candles_rejected": 1,
                "rejected_frames": 1,
                "duplicate_candles": 2,
            },
            "cannot exceed rejected candles",
        ),
    ],
)
def test_an_impossible_snapshot_is_refused(overrides: dict[str, int], message: str) -> None:
    # A snapshot that cannot describe a real history would make a report say something the
    # feed never observed.
    with pytest.raises(ValueError, match=message):
        FeedMetricsSnapshot(**overrides)
