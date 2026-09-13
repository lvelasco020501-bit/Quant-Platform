"""Latch policies for the circuit breakers, measured without changing a line of Risk.

The deployed Risk V2 latches forever after five consecutive losses. M13 showed that halts a
trend strategy within weeks, which leaves nothing to measure: not the strategy, and not
whether the breaker made anything safer. This module lets *other* policies be measured on
exactly the same engine, so the choice between them can rest on evidence.

**How, without touching Risk.** The backtest engine keeps breakers in its own state and hands
them to the risk engine in ``RiskContext.breakers``; the risk engine only ever *reads* them.
:class:`LatchPolicyRiskEngine` is a research-only subclass that overrides one public method,
``assess``, and changes one thing: which consecutive-loss breaker the check is shown. It
removes the engine's own streak breaker and substitutes its own, tripped and released by the
policy. Every other breaker — drawdown, daily loss — passes through untouched, and every other
method is inherited unchanged.

**Fail-closed.** The wrapper can release only the pause it created itself. It never removes a
drawdown or daily-loss breaker, never approves what the underlying engine refuses, and a
forced exit is never blocked by any breaker, exactly as before.

**The same loss the engine counts.** A trade's result is the flat position's lifecycle
``realized_pnl``, read at the open-to-flat transition — the number the engine itself records.
The permanent policy reproducing the deployed engine bit for bit is the test of that claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Self

from pydantic import Field, model_validator

from quantplatform.core.enums import CircuitBreakerReason, RiskOutcome
from quantplatform.core.models.base import DomainModel, Text
from quantplatform.core.models.risk import CircuitBreakerState, RiskContext
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.engine import RiskEvaluationResult, StandardRiskEngine

if TYPE_CHECKING:
    from quantplatform.core.models.orders import OrderIntent

__all__ = [
    "LatchPolicy",
    "LatchPolicyRiskEngine",
    "LatchStats",
    "blocked_time_share",
    "risk_configuration_for",
]

_LATCHED: Final[str] = "circuit breaker is latched"
_STREAK: Final[CircuitBreakerReason] = CircuitBreakerReason.CONSECUTIVE_LOSSES


class LatchPolicy(DomainModel):
    """How the account protects itself after losses, as one comparable unit."""

    key: Text
    label: Text
    streak_limit: int | None = Field(default=None, ge=1)
    """Consecutive losing trades that pause new exposure. ``None``: no streak protection."""

    cooldown: timedelta | None = None
    """How long a streak pause lasts. ``None`` with a streak limit: the pause is permanent."""

    drawdown_latch_pct: Decimal | None = Field(default=None, gt=0, lt=1)
    """Peak-to-trough drawdown that latches new exposure off for good. ``None``: no latch."""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.cooldown is not None and self.streak_limit is None:
            msg = "a cooldown releases a streak pause; a policy with no streak limit has none"
            raise ValueError(msg)
        if self.cooldown is not None and self.cooldown <= timedelta(0):
            msg = "a cooldown must last some time"
            raise ValueError(msg)
        return self


def risk_configuration_for(policy: LatchPolicy, deployed: RiskConfiguration) -> RiskConfiguration:
    """Return the deployed configuration with only this policy's breakers set.

    Everything else — sizing, stops, costs, daily loss — stays exactly as deployed. A streak
    limit is kept in the configuration even when the policy cools it down, because the risk
    engine only enforces a consecutive-loss breaker it has been configured to have.
    """
    update: dict[str, object] = {"max_consecutive_losses": policy.streak_limit}
    if policy.drawdown_latch_pct is None:
        update["latch_total_drawdown"] = False
    else:
        update["latch_total_drawdown"] = True
        update["max_total_drawdown_pct"] = policy.drawdown_latch_pct
    return RiskConfiguration.model_validate({**deployed.model_dump(), **update})


@dataclass
class LatchStats:
    """What a policy did over one run. Read after the run; never consulted during it."""

    assessed: int = 0
    """Fresh decisions the risk engine made (a replayed decision is not counted twice)."""

    blocked: int = 0
    """Of those, decisions refused because some circuit breaker was latched."""

    pauses: list[tuple[datetime, datetime | None]] = field(default_factory=list)
    """Every streak pause: when it began and when it ends (``None`` for permanent)."""

    trips: set[tuple[CircuitBreakerReason, datetime]] = field(default_factory=set)
    """Every other breaker seen tripped, with the instant the engine says it tripped."""


class LatchPolicyRiskEngine(StandardRiskEngine):
    """The standard risk engine, with the consecutive-loss breaker governed by a policy."""

    def __init__(self, *, config: RiskConfiguration, policy: LatchPolicy) -> None:
        """Bind the engine to its limits and the policy that governs its streak breaker."""
        super().__init__(config=config)
        self._policy = policy
        self._stats = LatchStats()
        self._streak = 0
        self._open: dict[str, bool] = {}
        self._paused_since: datetime | None = None
        self._paused_until: datetime | None = None
        self._paused_streak = 0

    @property
    def stats(self) -> LatchStats:
        """Return what the policy has done so far."""
        return self._stats

    def assess(
        self, intent: OrderIntent, context: RiskContext, *, forced_exit: bool = False
    ) -> RiskEvaluationResult:
        """Decide exactly as the standard engine would, under this policy's streak breaker."""
        self._observe(context)
        breakers = tuple(b for b in context.breakers if b.reason is not _STREAK)
        for breaker in breakers:
            if breaker.tripped_at is not None and breaker.reason is not None:
                self._stats.trips.add((breaker.reason, breaker.tripped_at))
        if self._paused_since is not None:
            breakers = (
                *breakers,
                CircuitBreakerState(
                    tripped_at=self._paused_since,
                    reason=_STREAK,
                    consecutive_losses=self._paused_streak,
                    resets_at=self._paused_until,
                ),
            )
        result = super().assess(
            intent, context.model_copy(update={"breakers": breakers}), forced_exit=forced_exit
        )
        if not result.replayed:
            self._stats.assessed += 1
            decision = result.decision
            if decision.outcome is RiskOutcome.REJECTED and any(
                _LATCHED in reason for reason in decision.rejection_reasons
            ):
                self._stats.blocked += 1
        return result

    def _observe(self, context: RiskContext) -> None:
        """Count closed trades, release a pause that has run its course, trip a new one."""
        now = context.as_of
        paused = self._paused_since is not None
        for position in context.snapshot.positions:
            was_open = self._open.get(position.symbol, False)
            # A trade that closed while the account was paused belongs to that episode: the
            # next streak is earned from trades taken after the release, never re-counted.
            if was_open and not position.is_open and not paused:
                self._streak = self._streak + 1 if position.realized_pnl < 0 else 0
            self._open[position.symbol] = position.is_open

        if paused and self._paused_until is not None and now >= self._paused_until:
            self._paused_since = None
            self._paused_until = None
            self._streak = 0

        limit = self._policy.streak_limit
        if self._paused_since is None and limit is not None and self._streak >= limit:
            self._paused_since = now
            cooldown = self._policy.cooldown
            self._paused_until = None if cooldown is None else now + cooldown
            self._paused_streak = self._streak
            self._stats.pauses.append((now, self._paused_until))


def blocked_time_share(stats: LatchStats, *, start: datetime, end: datetime) -> Decimal:
    """Return the share of ``[start, end)`` during which new exposure was latched off.

    The union of every streak pause, every drawdown latch (which never ends) and every
    daily-loss latch (which ends at the next UTC midnight, by that breaker's definition).
    """
    intervals: list[tuple[datetime, datetime]] = [
        (begin, finish if finish is not None else end) for begin, finish in stats.pauses
    ]
    for reason, at in stats.trips:
        if reason is CircuitBreakerReason.DAILY_LOSS_LIMIT:
            midnight = datetime(at.year, at.month, at.day, tzinfo=at.tzinfo) + timedelta(days=1)
            intervals.append((at, midnight))
        else:
            intervals.append((at, end))
    covered = timedelta(0)
    cursor = start
    for begin, finish in sorted((max(b, start), min(f, end)) for b, f in intervals):
        if finish <= cursor:
            continue
        covered += finish - max(begin, cursor)
        cursor = finish
    total = end - start
    if total <= timedelta(0):
        return Decimal(0)
    return Decimal(str(covered.total_seconds())) / Decimal(str(total.total_seconds()))
