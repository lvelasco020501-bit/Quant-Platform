"""Latches that stop the account opening, and never stop it closing.

Three conditions say the same thing in different ways: today went badly enough, the account
is far enough below its high, or the last several attempts all lost. Each halts new entries
and none of them liquidates anything — a breaker that closed positions on a threshold would
turn a decline into a realised loss at the worst possible moment, which is a strategy nobody
researched rather than a risk control.

What separates a breaker from the drawdown checks that already existed is the **latch**. The
instantaneous check refuses an order while the condition holds and passes again the moment
equity ticks up; a breaker, once tripped, stays tripped. The thresholds are the same fields.
Only the persistence of the verdict is new.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import (
    CircuitBreakerReason,
    OrderSide,
    RiskCheckCode,
    RiskCheckSeverity,
    RiskOutcome,
)
from quantplatform.core.models.risk import CircuitBreakerState, RiskContext
from quantplatform.risk.config import RiskConfiguration
from tests.factories import (
    ANCHOR,
    make_balance,
    make_intent,
    make_position,
    make_risk_context,
    make_risk_engine,
    make_snapshot,
)


def _tripped(reason: CircuitBreakerReason) -> CircuitBreakerState:
    return CircuitBreakerState(tripped_at=ANCHOR, reason=reason)


_DRAWDOWN = _tripped(CircuitBreakerReason.EXCESSIVE_DRAWDOWN)


def _holding_context(**overrides: object) -> RiskContext:
    """A context whose account holds the position an exit would sell."""
    return make_risk_context(
        snapshot=make_snapshot(
            positions=(make_position(quantity=Decimal("0.1")),),
            balances=(
                make_balance(free=Decimal(5_000)),
                make_balance(asset="BTC", free=Decimal("0.1")),
            ),
        ),
        **overrides,  # type: ignore[arg-type]
    )


# --- Configuration: a latch may not inherit its threshold ----------------------------------------


def test_the_total_drawdown_latch_is_off_by_default() -> None:
    assert RiskConfiguration().latch_total_drawdown is False


def test_latching_on_an_inherited_threshold_does_not_construct() -> None:
    # `max_total_drawdown_pct` carries a default, unlike the other two breaker thresholds,
    # which are None until configured. Letting the latch read it would arm a breaker at a
    # number nobody chose — implicit financial behaviour, and the reason this refuses.
    with pytest.raises(ValueError, match="max_total_drawdown_pct"):
        RiskConfiguration(latch_total_drawdown=True)


def test_latching_on_an_explicit_threshold_constructs() -> None:
    config = RiskConfiguration(latch_total_drawdown=True, max_total_drawdown_pct=Decimal("0.30"))

    assert config.latch_total_drawdown is True
    assert config.max_total_drawdown_pct == Decimal("0.30")


def test_choosing_the_default_value_deliberately_is_still_a_choice() -> None:
    # The refusal is about the *decision*, not the number. An operator who states the
    # platform's default explicitly has decided; one who never mentions it has not.
    config = RiskConfiguration(
        latch_total_drawdown=True, max_total_drawdown_pct=RiskConfiguration().max_total_drawdown_pct
    )

    assert config.latch_total_drawdown is True


def test_no_second_drawdown_threshold_exists() -> None:
    # The breaker and the instantaneous check read one field, so the rule that a latched
    # threshold may never sit below its instantaneous equivalent holds by identity and
    # cannot drift. A second number would be one more thing to justify with no evidence.
    assert not any(
        name != "max_total_drawdown_pct" and "total_drawdown" in name and name.endswith("_pct")
        for name in RiskConfiguration.model_fields
    )


# --- Authority: breakers gate risk being added, never risk being removed -------------------------


def test_a_tripped_breaker_blocks_a_new_entry() -> None:
    engine = make_risk_engine(latch_total_drawdown=True, max_total_drawdown_pct=Decimal("0.30"))

    decision = engine.assess(make_intent(), make_risk_context(breakers=(_DRAWDOWN,))).decision

    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskCheckCode.MAX_TOTAL_DRAWDOWN_BREAKER in {
        check.code for check in decision.checks if check.blocks
    }


def test_a_tripped_breaker_does_not_block_a_strategic_exit() -> None:
    # Expressed by side rather than by `forced_exit`, because reducing exposure is never the
    # thing a breaker protects against. An account that may not close while halted is an
    # account the halt has trapped.
    engine = make_risk_engine(latch_total_drawdown=True, max_total_drawdown_pct=Decimal("0.30"))
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(intent, _holding_context(breakers=(_DRAWDOWN,))).decision

    assert decision.outcome is not RiskOutcome.REJECTED


def test_a_tripped_breaker_does_not_block_a_forced_exit() -> None:
    engine = make_risk_engine(latch_total_drawdown=True, max_total_drawdown_pct=Decimal("0.30"))
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(
        intent, _holding_context(breakers=(_DRAWDOWN,)), forced_exit=True
    ).decision

    assert decision.outcome is not RiskOutcome.REJECTED


def test_a_breaker_is_recorded_as_advisory_on_an_exit_rather_than_hidden() -> None:
    engine = make_risk_engine(latch_total_drawdown=True, max_total_drawdown_pct=Decimal("0.30"))
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(intent, _holding_context(breakers=(_DRAWDOWN,))).decision

    check = next(c for c in decision.checks if c.code is RiskCheckCode.MAX_TOTAL_DRAWDOWN_BREAKER)
    assert check.severity is RiskCheckSeverity.ADVISORY
    assert check.passed is False


def test_an_unconfigured_breaker_never_blocks_anything() -> None:
    # V1. No latch, no thresholds, and a context that somehow carries a tripped state still
    # trades exactly as it always did: a breaker nobody configured has no authority.
    engine = make_risk_engine()

    decision = engine.assess(make_intent(), make_risk_context(breakers=(_DRAWDOWN,))).decision

    assert decision.outcome is not RiskOutcome.REJECTED


def test_an_untripped_breaker_blocks_nothing() -> None:
    engine = make_risk_engine(latch_total_drawdown=True, max_total_drawdown_pct=Decimal("0.30"))

    decision = engine.assess(make_intent(), make_risk_context()).decision

    assert decision.outcome is not RiskOutcome.REJECTED


# --- The instantaneous drawdown check loses its veto over exits too -----------------------------


def test_an_extreme_drawdown_no_longer_blocks_a_protective_exit() -> None:
    # The defect this milestone found: `total_drawdown` is blocking on every intent, so a
    # 90% drawdown refused the very stop-out that drawdown exists to make survivable. It is
    # the same inversion the frequency limit and the spread guard already had corrected.
    engine = make_risk_engine(max_total_drawdown_pct=Decimal("0.10"))
    intent = make_intent(side=OrderSide.SELL, quantity=Decimal("0.1"))

    decision = engine.assess(
        intent, _holding_context(peak_equity=Decimal(100_000)), forced_exit=True
    ).decision

    assert decision.outcome is not RiskOutcome.REJECTED


def test_an_extreme_drawdown_still_blocks_a_new_entry() -> None:
    engine = make_risk_engine(max_total_drawdown_pct=Decimal("0.10"))

    decision = engine.assess(
        make_intent(), make_risk_context(peak_equity=Decimal(100_000))
    ).decision

    assert decision.outcome is RiskOutcome.REJECTED


# --- State that cannot describe itself ----------------------------------------------------------


def test_two_latches_for_the_same_reason_fail_loudly() -> None:
    # Each reason latches independently so that a daily reset cannot clear a structural one.
    # Two entries for one reason would make "is this reason tripped, and since when" have two
    # answers, and the reset would then depend on which was read first.
    with pytest.raises(ValueError, match="at most once"):
        make_risk_context(breakers=(_DRAWDOWN, _DRAWDOWN))
