"""Recovery rules for the drawdown breaker — how a halted market is allowed to trade again.

M16 found the defect this module exists to fix. Under the permanent drawdown latch, XRP's
breaker tripped once, on 2020-07-29, and that market never traded again: 27 trades, 74% of
its history blocked, minus 3%. The *same* run with fees doubled never tripped at all and traded
146 times for +42%. Nothing about the strategy changed between those two runs — only which
side of a threshold the equity happened to pass on one afternoon, six years ago. A protection
whose ten-year outcome hinges on that is not limiting damage, it is amplifying an accident.

A permanent latch has a second, quieter problem: after it trips, drawdown is still measured
against a peak the account can no longer reach, so *any* reopening reopens straight back into
the latch. Being allowed to trade again and being able to are different things, which is why
one of the rules below moves the high-water mark to wherever the account restarts.

**How, without touching Risk.** Same discipline as :mod:`~quantplatform.research.latch_policy`:
the backtest engine keeps breakers in its own state and hands them to the risk engine, which
only reads them. :class:`RecoveryLatchRiskEngine` overrides one public method, ``assess``, and
substitutes its own drawdown breaker for the engine's — but **only when the policy actually
governs recovery**. Under :attr:`Recovery.PERMANENT` the engine's own breaker passes through
untouched, so every result recorded in M14, M15 and M16 reproduces bit for bit.

**Fail-closed.** The wrapper releases only the latch it created itself. It never removes a
daily-loss or consecutive-loss breaker, never approves what the underlying engine refuses,
and a forced exit is never blocked — closing a position is how risk goes down.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Self

from pydantic import Field, model_validator

from quantplatform.core.enums import CircuitBreakerReason, RiskOutcome
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.core.models.risk import CircuitBreakerState, RiskContext
from quantplatform.research.latch_policy import LatchStats
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.engine import RiskEvaluationResult, StandardRiskEngine

if TYPE_CHECKING:
    from quantplatform.core.models.orders import OrderIntent

__all__ = [
    "Recovery",
    "RecoveryLatchRiskEngine",
    "RecoveryPolicy",
    "next_period_start",
    "risk_configuration_for_recovery",
]

_LATCHED: Final[str] = "circuit breaker is latched"
_DRAWDOWN: Final[CircuitBreakerReason] = CircuitBreakerReason.EXCESSIVE_DRAWDOWN
_STREAK: Final[CircuitBreakerReason] = CircuitBreakerReason.CONSECUTIVE_LOSSES
QUARTER_MONTHS: Final[int] = 3


class Recovery(StrEnum):
    """How a market halted by the drawdown breaker is allowed to trade again."""

    PERMANENT = "permanent: never"
    COOLDOWN = "after a fixed cooldown"
    PERIOD = "at the next calendar quarter"
    COOLDOWN_AND_RESTART = "after a fixed cooldown, measuring from the restart"


def next_period_start(moment: datetime) -> datetime:
    """Return the first instant of the calendar quarter strictly after ``moment``."""
    quarter_index = (moment.month - 1) // QUARTER_MONTHS + 1
    month = quarter_index * QUARTER_MONTHS + 1
    year = moment.year + (1 if month > 12 else 0)  # noqa: PLR2004 — months in a year
    month = 1 if month > 12 else month  # noqa: PLR2004 — months in a year
    return datetime(year, month, 1, tzinfo=moment.tzinfo)


class RecoveryPolicy(DomainModel):
    """A drawdown breaker plus the rule that decides when the market reopens."""

    key: Text
    label: Text
    drawdown_pct: Decimal | None = Field(default=None, gt=0, lt=1)
    """Peak-to-trough drawdown that halts new exposure. ``None``: no drawdown breaker."""

    recovery: Recovery = Recovery.PERMANENT
    cooldown: timedelta | None = None
    """How long the halt lasts, for the rules that wait. Never used to tune a return."""

    streak_limit: int | None = Field(default=None, ge=1)
    """Consecutive losing trades that halt new exposure, for reproducing production."""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        waits = self.recovery in {Recovery.COOLDOWN, Recovery.COOLDOWN_AND_RESTART}
        if waits and self.cooldown is None:
            msg = f"{self.recovery.value} needs a cooldown to wait for"
            raise ValueError(msg)
        if not waits and self.cooldown is not None:
            msg = f"{self.recovery.value} does not wait, so it cannot carry a cooldown"
            raise ValueError(msg)
        if self.cooldown is not None and self.cooldown <= timedelta(0):
            msg = "a cooldown must last some time"
            raise ValueError(msg)
        if self.recovery is not Recovery.PERMANENT and self.drawdown_pct is None:
            msg = "a recovery rule needs a drawdown breaker to recover from"
            raise ValueError(msg)
        return self

    @property
    def governs_drawdown(self) -> bool:
        """Return whether the wrapper decides this breaker, rather than the engine itself.

        Only the rules that reopen a market need to. Leaving the permanent rule to the
        engine is what keeps every earlier milestone reproducible.
        """
        return self.recovery is not Recovery.PERMANENT

    @property
    def restarts_high_water_mark(self) -> bool:
        """Return whether reopening also moves the peak the next drawdown is measured from.

        This is the difference that decides whether a market can actually trade again. The
        drawdown *limit* is not the latch: while equity sits 10% under its peak, the limit
        refuses new exposure whether or not a latch is set. So a rule that only ends the halt
        reopens the market straight back into the same refusal, and only a rule that moves
        the reference lets it trade — which is exactly what :attr:`Recovery.COOLDOWN` is kept
        in the comparison to demonstrate.
        """
        return self.recovery in {Recovery.PERIOD, Recovery.COOLDOWN_AND_RESTART}


def risk_configuration_for_recovery(
    policy: RecoveryPolicy, deployed: RiskConfiguration
) -> RiskConfiguration:
    """Return the deployed configuration with only this policy's breakers armed.

    Sizing, stops, costs and the daily-loss breaker stay exactly as deployed: this milestone
    changes when a halted market reopens, and nothing else.
    """
    update: dict[str, object] = {"max_consecutive_losses": policy.streak_limit}
    if policy.drawdown_pct is None:
        update["latch_total_drawdown"] = False
    else:
        update["latch_total_drawdown"] = True
        update["max_total_drawdown_pct"] = policy.drawdown_pct
    return RiskConfiguration.model_validate({**deployed.model_dump(), **update})


class RecoveryLatchRiskEngine(StandardRiskEngine):
    """The standard risk engine, with the drawdown breaker governed by a recovery rule."""

    def __init__(self, *, config: RiskConfiguration, policy: RecoveryPolicy) -> None:
        """Bind the engine to its limits and the policy that reopens a halted market."""
        super().__init__(config=config)
        self._policy = policy
        self._stats = LatchStats()
        self._peak: Decimal | None = None
        self._latched_since: datetime | None = None
        self._latched_until: datetime | None = None
        self._period_start: datetime | None = None
        self._last_release: datetime | None = None
        self._restarted_at_peak: Decimal | None = None
        """The engine's peak when the reference was last restarted; ``None`` until then."""

    @property
    def stats(self) -> LatchStats:
        """Return what the policy did: every halt, and how often it refused a decision."""
        return self._stats

    def assess(
        self, intent: OrderIntent, context: RiskContext, *, forced_exit: bool = False
    ) -> RiskEvaluationResult:
        """Decide exactly as the standard engine would, under this policy's recovery rule."""
        engine_trip = next(
            (b.tripped_at for b in context.breakers if b.reason is _DRAWDOWN and b.tripped_at),
            None,
        )
        self._observe(context, engine_trip)
        breakers = context.breakers
        if self._policy.governs_drawdown:
            breakers = tuple(b for b in breakers if b.reason is not _DRAWDOWN)
        for breaker in breakers:
            if breaker.tripped_at is not None and breaker.reason is not None:
                self._stats.trips.add((breaker.reason, breaker.tripped_at))
        if self._latched_since is not None:
            breakers = (
                *breakers,
                CircuitBreakerState(
                    tripped_at=self._latched_since, reason=_DRAWDOWN, resets_at=self._latched_until
                ),
            )
        update: dict[str, object] = {"breakers": breakers}
        if self._policy.governs_drawdown and self._peak is not None:
            # The limit check measures against ``context.peak_equity``; a policy that
            # restarts the reference has to be measured against its own, or "reopened"
            # would mean nothing.
            update["peak_equity"] = self._peak
        result = super().assess(intent, context.model_copy(update=update), forced_exit=forced_exit)
        if not result.replayed:
            self._stats.assessed += 1
            decision = result.decision
            if decision.outcome is RiskOutcome.REJECTED and any(
                _LATCHED in reason for reason in decision.rejection_reasons
            ):
                self._stats.blocked += 1
        return result

    def _observe(self, context: RiskContext, engine_trip: datetime | None) -> None:
        """Track the reference peak, reopen a halt that is due, and start a new one."""
        if not self._policy.governs_drawdown:
            self._record_engine_latch(context)
            return
        now, equity = context.as_of, context.snapshot.equity
        self._track_reference(context.peak_equity, equity)
        if self._policy.recovery is Recovery.PERIOD:
            self._roll_period(now, equity)
        self._reopen_if_due(now, equity)
        self._halt_if_breached(now, equity, engine_trip)

    def _track_reference(self, engine_peak: Decimal, equity: Decimal) -> None:
        """Update the peak this policy measures drawdown from.

        Before any restart the engine's own running peak is used verbatim: it is sampled on
        every bar, while this wrapper only ever sees the account when the strategy asks to
        trade, so keeping a private peak here would quietly under-measure every drawdown —
        and a breaker that under-measures is a breaker that never fires.

        After a restart the reference is the equity at the restart, raised by any *new*
        account high since: a new global high is necessarily a high since the restart, so
        that part stays exact. Between restarts, when the account has made no new high, the
        best this wrapper can see is the equity at the moments it was consulted, which is a
        lower bound on the true peak and therefore errs towards halting late rather than
        early. The bound is recorded here so no reader has to infer it.
        """
        if self._restarted_at_peak is None:
            self._peak = engine_peak
            return
        if engine_peak > self._restarted_at_peak:
            self._peak = engine_peak
            return
        self._peak = equity if self._peak is None else max(self._peak, equity)

    def _record_engine_latch(self, context: RiskContext) -> None:
        """Note the engine's own permanent latch, changing nothing about it."""
        if self._stats.pauses:
            return
        for breaker in context.breakers:
            if breaker.reason is _DRAWDOWN and breaker.tripped_at is not None:
                self._stats.pauses.append((breaker.tripped_at, None))
                return

    def _roll_period(self, now: datetime, equity: Decimal) -> None:
        """Start a new quarter: the reference becomes its opening equity, and any halt ends.

        This is what makes the quarterly rule predictable: no market is ever held out for
        longer than the quarter in which it breached.
        """
        if self._period_start is None:
            self._period_start = next_period_start(now)
            return
        if now < self._period_start:
            return
        self._restart_reference(equity)
        self._period_start = next_period_start(now)
        self._end_halt(now)

    def _reopen_if_due(self, now: datetime, equity: Decimal) -> None:
        """End a halt whose wait has run out, moving the reference if the rule says so."""
        if self._latched_since is None or self._latched_until is None or now < self._latched_until:
            return
        self._end_halt(self._latched_until)
        if self._policy.restarts_high_water_mark:
            self._restart_reference(equity)

    def _restart_reference(self, equity: Decimal) -> None:
        """Measure the next drawdown from here, not from a peak the account cannot reach."""
        self._restarted_at_peak = self._peak
        self._peak = equity

    def _end_halt(self, at: datetime) -> None:
        if self._latched_since is None:
            return
        self._stats.pauses[-1] = (self._latched_since, at)
        self._latched_since = None
        self._latched_until = None
        self._last_release = at

    def _halt_if_breached(
        self, now: datetime, equity: Decimal, engine_trip: datetime | None
    ) -> None:
        """Halt new exposure when the drawdown reaches the threshold.

        The trigger is the engine's own breaker, not a drawdown recomputed here. The engine
        evaluates every bar; this wrapper is only consulted when the strategy asks to trade,
        and the difference is not academic: XRP's deepest drawdown was 10.14% against a 10%
        limit, so a wrapper checking at decision points saw 9-point-something and never
        halted a market the engine had already stopped. Recomputing the trigger would have
        measured a breaker that never fires.

        Once the reference has been restarted the engine's breaker is stuck on a peak the
        policy no longer recognises, so from then on the threshold is applied to the
        restarted reference at the moments this wrapper is consulted — late rather than
        early, and recorded as such.
        """
        threshold = self._policy.drawdown_pct
        if self._latched_since is not None or threshold is None:
            return
        fresh = engine_trip is not None and (
            self._last_release is None or engine_trip > self._last_release
        )
        if self._restarted_at_peak is None and fresh:
            now = engine_trip if engine_trip is not None else now
        elif not self._peak or (self._peak - equity) / self._peak < threshold:
            return
        self._latched_since = now
        self._latched_until = (
            next_period_start(now)
            if self._policy.recovery is Recovery.PERIOD
            else now + (self._policy.cooldown or timedelta(0))
        )
        self._stats.pauses.append((self._latched_since, self._latched_until))
