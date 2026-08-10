"""Operational health: is the session's output worth believing?

**Feed health and session health are separate questions.** One asks whether the venue's
candles reached the process; the other whether the process used them. Reporting only the
first is how a session that refused every bar for a week stayed green.

**Every feed figure judged here is a daily one.** The counters arrive as differences between
two cumulative readings, so three reconnects on Monday leave Tuesday's reconnect check
green. Thresholds are unchanged from Phase 7B; only what they are applied to moved.

**A third subsystem is graded here too: the venue's rulebook.** Trading rules are fetched,
not assumed, and the risk engine refuses to size an order against rules older than its
budget. So a report has to answer both whether they are still fresh and whether the loop
that keeps them fresh is still working — the second turning yellow hours before the first
turns red, which is the difference between a warning and a post-mortem.

Each question is answered independently against a threshold, and the day takes the worst
answer. Health is kept strictly apart from performance — a profitable day that dropped a
third of its candles is a *red* day, because the profit was earned on a history the strategy
never fully saw.

**A check with nothing to measure reports itself skipped, not passed.** Clock drift is the
standing example: nothing in the platform measures it yet, so claiming green would be
asserting something no one checked.

**Two escalation rules, because two kinds of threshold behave differently.** A ratio can be
exceeded by a factor, so yellow becomes red at a multiple of the limit. A counter whose
limit is zero cannot — every multiple of zero is zero — so those escalate by an absolute
step instead.
"""

from __future__ import annotations

from decimal import Decimal

from quantplatform.core.constants import ONE, ZERO
from quantplatform.reporting.config import AlertThresholds
from quantplatform.reporting.models import (
    DailyHealth,
    DailyStatistics,
    HealthCheck,
    HealthCheckName,
    HealthLevel,
)

__all__ = ["evaluate_health"]


def evaluate_health(*, statistics: DailyStatistics, thresholds: AlertThresholds) -> DailyHealth:
    """Grade a day's operational health.

    Args:
        statistics: The day's computed figures.
        thresholds: The limits in force.

    Returns:
        Every check and the worst level among them.
    """
    checks = (
        _feed_stability(statistics, thresholds),
        _ceiling(
            HealthCheckName.GAP_COUNT,
            observed=statistics.daily_gaps,
            limit=thresholds.max_gap_count,
            step=thresholds.red_escalation_count,
            noun="candle gap",
        ),
        _ceiling(
            HealthCheckName.HEARTBEAT_FAILURES,
            observed=statistics.daily_heartbeat_failures,
            limit=thresholds.max_heartbeat_failures,
            step=thresholds.red_escalation_count,
            noun="heartbeat failure",
        ),
        _ceiling(
            HealthCheckName.RUNTIME_EXCEPTIONS,
            observed=statistics.runtime_exceptions,
            limit=thresholds.max_runtime_exceptions,
            step=thresholds.red_escalation_count,
            noun="runtime exception",
        ),
        _ceiling(
            HealthCheckName.RECONNECTS,
            observed=statistics.daily_reconnects,
            limit=thresholds.max_reconnects,
            step=thresholds.red_escalation_count,
            noun="reconnect",
        ),
        _rejection_ratio(statistics, thresholds),
        _ceiling(
            HealthCheckName.MISSING_BARS,
            observed=statistics.missing_bars,
            limit=thresholds.max_missing_bars,
            step=thresholds.red_escalation_count,
            noun="missing bar",
        ),
        _ceiling(
            HealthCheckName.SESSION_INTERRUPTIONS,
            observed=statistics.session_interruptions,
            limit=thresholds.max_session_interruptions,
            step=thresholds.red_escalation_count,
            noun="session interruption",
        ),
        _clock_drift(statistics, thresholds),
        _session_acceptance(statistics, thresholds),
        _symbol_rules_freshness(statistics),
        _symbol_rules_refresh(statistics),
    )
    return DailyHealth(
        level=HealthLevel.worst(tuple(check.level for check in checks)), checks=checks
    )


def _ceiling(
    name: HealthCheckName,
    *,
    observed: int,
    limit: int,
    step: int,
    noun: str,
) -> HealthCheck:
    """Grade a counter that must stay at or below a limit.

    Args:
        name: Which check this is.
        observed: The count for the day.
        limit: Highest tolerated count.
        step: How far past the limit turns yellow into red.
        noun: Singular noun for the message.

    Returns:
        The graded check.
    """
    if observed <= limit:
        return HealthCheck(
            name=name,
            level=HealthLevel.GREEN,
            message=f"{observed} {noun}(s), within the limit of {limit}",
            observed=Decimal(observed),
            threshold=Decimal(limit),
        )
    level = HealthLevel.RED if observed >= limit + step else HealthLevel.YELLOW
    return HealthCheck(
        name=name,
        level=level,
        message=f"{observed} {noun}(s) against a limit of {limit}",
        observed=Decimal(observed),
        threshold=Decimal(limit),
    )


def _feed_stability(statistics: DailyStatistics, thresholds: AlertThresholds) -> HealthCheck:
    """Grade the share of received bars that reached the pipeline."""
    observed = statistics.observed_acceptance_rate
    minimum = thresholds.min_acceptance_rate
    if observed is None:
        return HealthCheck(
            name=HealthCheckName.FEED_STABILITY,
            level=HealthLevel.GREEN,
            message="no bars were received, so acceptance has nothing to measure",
            threshold=minimum,
            skipped=True,
        )
    if observed >= minimum:
        return HealthCheck(
            name=HealthCheckName.FEED_STABILITY,
            level=HealthLevel.GREEN,
            message=f"{_percent(observed)} of received bars reached the pipeline",
            observed=observed,
            threshold=minimum,
        )
    # Halving the required rate is the red line: at that point most of what the venue sent
    # never reached the strategy, and the day describes a different history than the market's.
    floor = minimum / thresholds.red_escalation_factor
    level = HealthLevel.RED if observed < floor else HealthLevel.YELLOW
    return HealthCheck(
        name=HealthCheckName.FEED_STABILITY,
        level=level,
        message=(
            f"only {_percent(observed)} of received bars reached the pipeline, "
            f"against a floor of {_percent(minimum)}"
        ),
        observed=observed,
        threshold=minimum,
    )


def _session_acceptance(statistics: DailyStatistics, thresholds: AlertThresholds) -> HealthCheck:
    """Grade the share of delivered bars the session actually processed.

    The check that was missing. Feed stability asks whether the venue's candles reached the
    process; this asks whether the process *used* them. The two came apart once — a
    contradictory grace period had the feed delivering every candle and the session refusing
    every one — and because nothing measured the second question, a week of trading nothing
    would have reported green.

    Zero processing while bars arrived is always red, never merely yellow. There is no
    configuration under which a session that received work and did none of it is healthy.
    """
    observed = statistics.daily_session_acceptance_rate
    minimum = thresholds.minimum_session_acceptance_rate
    if observed is None:
        return HealthCheck(
            name=HealthCheckName.SESSION_BAR_ACCEPTANCE,
            level=HealthLevel.GREEN,
            message="the session received no bars today, so acceptance has nothing to measure",
            threshold=minimum,
            skipped=True,
        )
    if observed >= minimum:
        return HealthCheck(
            name=HealthCheckName.SESSION_BAR_ACCEPTANCE,
            level=HealthLevel.GREEN,
            message=(
                f"the session processed {_percent(observed)} of the "
                f"{statistics.daily_session_bars_received} bar(s) it received"
            ),
            observed=observed,
            threshold=minimum,
        )
    if observed <= ZERO:
        return HealthCheck(
            name=HealthCheckName.SESSION_BAR_ACCEPTANCE,
            level=HealthLevel.RED,
            message=(
                f"the session processed none of the "
                f"{statistics.daily_session_bars_received} bar(s) it received"
            ),
            observed=observed,
            threshold=minimum,
        )
    floor = minimum / thresholds.red_escalation_factor
    level = HealthLevel.RED if observed < floor else HealthLevel.YELLOW
    return HealthCheck(
        name=HealthCheckName.SESSION_BAR_ACCEPTANCE,
        level=level,
        message=(
            f"the session processed only {_percent(observed)} of the bars it received, "
            f"against a floor of {_percent(minimum)}"
        ),
        observed=observed,
        threshold=minimum,
    )


def _symbol_rules_freshness(statistics: DailyStatistics) -> HealthCheck:
    """Grade the age of the venue's trading rules against the risk engine's own budget.

    The one check here that grades itself against a threshold it did not choose. Every other
    limit on this page is a reporting opinion about what looks unhealthy; this one is a
    statement of fact about what the risk engine *will already have done*. Once the rules
    pass ``stale_after_seconds`` the engine refuses every intent for
    ``symbol_rules_freshness``, and a report showing anything but red would be describing a
    session that had silently stopped trading.

    Red with no yellow, for that reason. There is no partially-stale: either the rules are
    inside the budget and orders flow, or they are outside it and none do.
    """
    name = HealthCheckName.SYMBOL_RULES_FRESHNESS
    limit = statistics.symbol_rules_stale_after_seconds
    observed = statistics.symbol_rules_age_seconds
    if not statistics.symbol_rules_telemetry_available or observed is None or limit <= 0:
        return HealthCheck(
            name=name,
            level=HealthLevel.GREEN,
            message="nothing reported the age of the venue's trading rules",
            skipped=True,
        )
    if observed < limit:
        return HealthCheck(
            name=name,
            level=HealthLevel.GREEN,
            message=(
                f"venue trading rules are {_hours(observed)} old, "
                f"within the {_hours(Decimal(limit))} budget"
            ),
            observed=observed,
            threshold=Decimal(limit),
        )
    return HealthCheck(
        name=name,
        level=HealthLevel.RED,
        message=(
            f"venue trading rules are {_hours(observed)} old, past the "
            f"{_hours(Decimal(limit))} budget; the risk engine is refusing every order"
        ),
        observed=observed,
        threshold=Decimal(limit),
    )


def _symbol_rules_refresh(statistics: DailyStatistics) -> HealthCheck:
    """Grade whether the loop that re-reads the venue's rules is still working.

    Separate from freshness on purpose, and the more useful of the two. Freshness only turns
    red once trading has *already* stopped; this turns yellow the moment refresh starts
    failing, while the rules in force are still perfectly valid. That gap — hours of warning
    before anything breaks — is the entire reason for reporting the mechanism as well as its
    result.

    Graded on consecutive failures rather than the cumulative count, so a single blip on
    Monday does not keep Friday yellow, and a loop that has been failing since Monday cannot
    look healthy on the strength of a hundred earlier successes.
    """
    name = HealthCheckName.SYMBOL_RULES_REFRESH
    if not statistics.symbol_rules_telemetry_available:
        return HealthCheck(
            name=name,
            level=HealthLevel.GREEN,
            message="no symbol-rules refresh was reported for this session",
            skipped=True,
        )
    consecutive = statistics.symbol_rules_consecutive_failures
    if consecutive == 0:
        return HealthCheck(
            name=name,
            level=HealthLevel.GREEN,
            message=(
                f"{statistics.symbol_rules_refresh_successes} of "
                f"{statistics.symbol_rules_refresh_attempts} refresh attempt(s) succeeded, "
                "most recently without error"
            ),
            observed=ZERO,
        )
    reason = statistics.symbol_rules_last_failure_reason or "no reason recorded"
    return HealthCheck(
        name=name,
        level=HealthLevel.YELLOW,
        message=(
            f"{consecutive} consecutive symbol-rules refresh failure(s); the last "
            f"known-good rules are still in force ({reason})"
        ),
        observed=Decimal(consecutive),
    )


def _rejection_ratio(statistics: DailyStatistics, thresholds: AlertThresholds) -> HealthCheck:
    """Grade refused orders as a share of every order decision made.

    Risk and broker refusals are combined here on purpose. Both mean the same thing
    operationally — the session wanted to trade and did not — and separating them at this
    level would let a day sit green while failing half its orders at each of two stages.
    """
    observed = statistics.order_rejection_ratio
    limit = thresholds.max_risk_rejection_ratio
    if observed is None:
        return HealthCheck(
            name=HealthCheckName.ORDER_REJECTION_RATIO,
            level=HealthLevel.GREEN,
            message="no order decisions were made, so rejection has nothing to measure",
            threshold=limit,
            skipped=True,
        )
    if observed <= limit:
        return HealthCheck(
            name=HealthCheckName.ORDER_REJECTION_RATIO,
            level=HealthLevel.GREEN,
            message=f"{_percent(observed)} of order decisions were refused",
            observed=observed,
            threshold=limit,
        )
    # A rejection ratio is bounded above by one, so multiplying the limit is the wrong
    # escalation: with a limit of 0.5 it would put the red line at 100%, and a day that
    # refused nine orders in ten would still read yellow. Escalate through the *remaining*
    # headroom instead, which keeps the red line strictly between the limit and total
    # failure whatever the limit is set to.
    ceiling = limit + (ONE - limit) / thresholds.red_escalation_factor
    level = HealthLevel.RED if observed >= ceiling else HealthLevel.YELLOW
    return HealthCheck(
        name=HealthCheckName.ORDER_REJECTION_RATIO,
        level=level,
        message=(
            f"{_percent(observed)} of order decisions were refused, "
            f"against a limit of {_percent(limit)}"
        ),
        observed=observed,
        threshold=limit,
    )


def _clock_drift(statistics: DailyStatistics, thresholds: AlertThresholds) -> HealthCheck:
    """Grade measured local-versus-venue clock skew."""
    observed = statistics.clock_drift_seconds
    limit = thresholds.max_clock_drift_seconds
    if observed is None:
        return HealthCheck(
            name=HealthCheckName.CLOCK_DRIFT,
            level=HealthLevel.GREEN,
            message="clock drift was not measured during this session",
            threshold=limit,
            skipped=True,
        )
    magnitude = abs(observed)
    if magnitude <= limit:
        return HealthCheck(
            name=HealthCheckName.CLOCK_DRIFT,
            level=HealthLevel.GREEN,
            message=f"clock drift of {magnitude}s is within {limit}s",
            observed=magnitude,
            threshold=limit,
        )
    level = (
        HealthLevel.RED
        if magnitude >= limit * thresholds.red_escalation_factor
        else HealthLevel.YELLOW
    )
    return HealthCheck(
        name=HealthCheckName.CLOCK_DRIFT,
        level=level,
        message=f"clock drift of {magnitude}s exceeds the {limit}s budget",
        observed=magnitude,
        threshold=limit,
    )


def _percent(value: Decimal) -> str:
    """Render a unit-interval ratio as a percentage with one decimal place."""
    if value == ZERO:
        return "0.0%"
    return f"{value * Decimal(100):.1f}%"


def _hours(seconds: Decimal) -> str:
    """Render an age in hours, the unit a rules freshness budget is actually reasoned in."""
    return f"{seconds / Decimal(3600):.1f}h"
