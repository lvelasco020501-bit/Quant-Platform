"""Which strategies may reach a paper session, and on what evidence.

The paper runner resolves strategies through :func:`build_default_registry`. That registry is
therefore the only door into a session, and this file is the list of who has a key.

Every entry below names the milestone that admitted it. That is not decoration: promotion is
the one decision in this codebase that cannot be undone by deleting a file, because a session
that ran is a session that ran. A strategy appearing here without a line explaining how it
got in is the failure these tests exist to make loud.

The test that matters most is
:func:`test_the_paper_registry_is_exactly_the_promoted_list`. It compares the registry to a
literal, so adding a strategy anywhere in the platform cannot quietly make it runnable — the
only way is to edit this file, on purpose, in a commit someone reviews.
"""

from __future__ import annotations

from quantplatform.core.enums import Timeframe
from quantplatform.strategies.breakout_trend import TrendFilteredBreakoutStrategy
from quantplatform.strategies.registry import BUILTIN_STRATEGIES, build_default_registry
from quantplatform.strategies.research import (
    MULTI_TIMEFRAME_BENCHMARKS,
    RESEARCH_STRATEGIES,
    build_research_registry,
)

PROMOTED: dict[str, str] = {
    "ema_trend": "shipped from the start; frozen as the benchmark and never re-promoted",
    "breakout": "admitted at M9c.3b, once the harness could compare two rules honestly",
    "breakout_trend": (
        "admitted after M23 returned PAPER CANDIDATE on BTC under a declared out-of-sample "
        "window, and M24 re-ran it against regime_trend through one protocol and one builder"
    ),
}
"""The allow-list. One line of provenance per strategy, or it does not belong here."""


# --- The door -----------------------------------------------------------------------------------


def test_the_paper_registry_is_exactly_the_promoted_list() -> None:
    registry = build_default_registry()
    assert {strategy.METADATA.strategy_id for strategy in BUILTIN_STRATEGIES} == set(PROMOTED)
    assert len(registry) == len(PROMOTED)
    for strategy_id in PROMOTED:
        assert strategy_id in registry


def test_every_promoted_strategy_states_how_it_got_in() -> None:
    for strategy_id, provenance in PROMOTED.items():
        assert provenance.strip(), f"{strategy_id} has no provenance"
        assert len(provenance) > 40, f"{strategy_id}'s provenance says too little"


def test_nothing_that_was_only_researched_can_reach_paper() -> None:
    registry = build_default_registry()
    for strategy_class in (*RESEARCH_STRATEGIES, *MULTI_TIMEFRAME_BENCHMARKS):
        assert strategy_class.METADATA.strategy_id not in registry
        assert strategy_class not in BUILTIN_STRATEGIES


def test_a_promoted_strategy_is_not_listed_as_research_as_well() -> None:
    # Listing it twice would make build_research_registry() raise on a duplicate id, and
    # would also leave two places claiming to decide where the rule may run.
    researched = {strategy.METADATA.strategy_id for strategy in RESEARCH_STRATEGIES}
    assert researched.isdisjoint(PROMOTED)


# --- One implementation, not a copy ---------------------------------------------------------------


def test_research_measures_the_same_class_paper_would_run() -> None:
    # The promoted rule was moved out of strategies/research.py, not duplicated. If these two
    # ever became different objects, every number M22 through M24 recorded would describe a
    # strategy that is not the one a session runs.
    assert build_research_registry().get("breakout_trend") is TrendFilteredBreakoutStrategy
    assert build_default_registry().get("breakout_trend") is TrendFilteredBreakoutStrategy


def test_the_promoted_rule_still_derives_its_contract_from_its_parameters() -> None:
    # This is what let M22 run it at 40/20/400 at all: the class-level metadata describes the
    # canonical configuration, and an instance declares the features its own numbers call for.
    strategy = build_default_registry().create(
        "breakout_trend", {"entry_lookback": 40, "exit_lookback": 20, "trend_period": 400}
    )
    assert strategy.metadata.required_features == (
        "donchian_high_40",
        "donchian_low_20",
        "sma_400",
    )
    # 400, not 401: sma_400 needs exactly its window, while the donchian names need one bar
    # more than theirs (41 and 21). The maximum is the sma's.
    assert strategy.metadata.required_history == 400


def test_the_promoted_rule_supports_the_timeframe_it_was_validated_on() -> None:
    assert Timeframe.H4 in TrendFilteredBreakoutStrategy.METADATA.supported_timeframes


def test_the_promoted_rule_is_long_only_spot() -> None:
    metadata = TrendFilteredBreakoutStrategy.METADATA
    assert metadata.allows_short is False
    assert metadata.operates_intrabar is False
