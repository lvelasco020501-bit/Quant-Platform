"""When Risk V2 is on, and what being on obliges.

Until now V2 was *emergent*: `_derive_stop` looked at `initial_stop_distance_bps` and
`_requested_quantity` looked at `risk_budget`, so a configuration carrying one but not the
other was V2 in name and V1 in behaviour — sizing by notional, deriving no stop, protecting
nothing, and saying so nowhere. A half-configured risk engine that silently degrades to the
regime it was configured to replace is the failure this file forecloses.

V2 becomes one declared fact with two obligations: a budget requires a stop distance, and a
budget requires every entry to carry a stop. V1 — no budget — is untouched, which is what
keeps the week-5 benchmark comparable to everything measured against it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.models.risk import RiskBudget
from quantplatform.risk.config import RiskConfiguration
from tests.factories import make_intent, make_risk_context, make_risk_engine

_BUDGET = RiskBudget(
    risk_per_trade_pct=Decimal("0.01"),
    max_position_exposure_pct=Decimal("1"),
    min_stop_distance_bps=Decimal(1),
    max_stop_distance_bps=Decimal(10_000),
)


# --- What "V2 is active" means ------------------------------------------------------------------


def test_a_budget_without_a_stop_distance_does_not_construct() -> None:
    # The half-configured case, refused at construction rather than at the first entry. A
    # budget with nowhere to measure risk from cannot size anything, and the engine's
    # fallback would be to size by notional — V1 behaviour under a V2 configuration, which
    # is the misreport this refusal exists to prevent.
    with pytest.raises(ValueError, match="initial_stop_distance_bps"):
        RiskConfiguration(risk_budget=_BUDGET)


def test_a_budget_with_a_stop_distance_reports_v2_active() -> None:
    config = RiskConfiguration(risk_budget=_BUDGET, initial_stop_distance_bps=Decimal(200))

    assert config.risk_v2_active is True


def test_no_budget_reports_v2_inactive_even_with_a_stop_distance() -> None:
    # A stop distance alone is legal and is not V2: it derives a level without sizing
    # against it. V2 is the budget, because the budget is what makes the stop load-bearing.
    config = RiskConfiguration(initial_stop_distance_bps=Decimal(200))

    assert config.risk_v2_active is False


# --- What being active obliges ------------------------------------------------------------------


def test_v2_requires_a_stop_on_every_entry_without_being_asked() -> None:
    # `require_stop_on_entry` defaults to False and nothing in a V2 configuration sets it.
    # Leaving it that way would let a V2 account open an unprotected position, which is the
    # one thing the whole milestone exists to make impossible.
    config = RiskConfiguration(risk_budget=_BUDGET, initial_stop_distance_bps=Decimal(200))

    assert config.stop_required is True


def test_v1_does_not_require_a_stop_on_entry() -> None:
    assert RiskConfiguration().stop_required is False


def test_an_explicit_requirement_stands_on_its_own_under_v1() -> None:
    # The flag is still honoured where it is set. V2 adds an obligation; it does not take
    # ownership of one that already existed.
    assert RiskConfiguration(require_stop_on_entry=True).stop_required is True


def test_every_entry_approved_under_v2_carries_a_stop() -> None:
    # The obligation on the real path, stated as the property that matters rather than as
    # the flag that produces it: under V2 there is no such thing as an approved entry with
    # nothing protecting it. A test on the flag alone would pass while the path that reads
    # it changed underneath.
    engine = make_risk_engine(risk_budget=_BUDGET, initial_stop_distance_bps=Decimal(200))

    decision = engine.assess(make_intent(), make_risk_context()).decision

    assert decision.approved_order is not None
    assert decision.approved_order.protective_stop is not None


def test_a_v1_entry_may_be_approved_with_no_stop_at_all() -> None:
    # The control that keeps the previous test meaningful, and the golden that keeps every
    # completed run of this platform comparable to what comes next.
    engine = make_risk_engine()

    decision = engine.assess(make_intent(), make_risk_context()).decision

    assert decision.approved_order is not None
    assert decision.approved_order.protective_stop is None
