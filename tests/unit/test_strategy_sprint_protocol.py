"""The M13 sprint protocol: declared before a single result was seen.

What makes a strategy sprint honest is not the number of strategies tried but whether the
thing being judged was chosen before or after looking. So the canonical parameters, the
neighbours used to test fragility, the in-sample/out-of-sample split and the verdict
thresholds all live in code, and these tests pin them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from quantplatform.orchestration.research import load_definition
from quantplatform.research.definition import ExperimentRole
from quantplatform.research.sprint import (
    CANDIDATES,
    IN_SAMPLE_WINDOW,
    MEANINGFUL_TRADE_SAMPLE,
    OUT_OF_SAMPLE_WINDOW,
    WALK_FORWARD_FOLDS,
    Evidence,
    Family,
    RiskScenario,
    Scorecard,
    Verdict,
    definition_for,
    deployed_definition_for,
    judge,
    narrowed,
    research_risk_variant,
    stress_scenarios,
)
from quantplatform.strategies.research import build_research_registry

DEPLOYED = Path("var/research/m10c/def_breakout_v2.json")


def test_every_candidate_and_every_neighbour_can_be_built() -> None:
    registry = build_research_registry()
    for candidate in CANDIDATES:
        registry.create(candidate.strategy_id, dict(candidate.params))
        for neighbour in candidate.neighbours:
            registry.create(candidate.strategy_id, dict(neighbour))


def test_each_research_candidate_has_two_neighbours_on_the_same_parameters() -> None:
    for candidate in CANDIDATES:
        if candidate.family is Family.BENCHMARK:
            assert candidate.neighbours == ()
            continue
        assert len(candidate.neighbours) == 2
        keys = {key for key, _ in candidate.params}
        for neighbour in candidate.neighbours:
            assert {key for key, _ in neighbour} == keys
            assert neighbour != candidate.params


def test_both_benchmarks_are_in_the_sprint() -> None:
    benchmarks = {c.strategy_id for c in CANDIDATES if c.family is Family.BENCHMARK}
    assert benchmarks == {"ema_trend", "breakout"}


def test_all_three_research_families_are_covered() -> None:
    assert {c.family for c in CANDIDATES} == set(Family)


def test_in_sample_and_out_of_sample_are_disjoint_and_contiguous() -> None:
    assert IN_SAMPLE_WINDOW.end == OUT_OF_SAMPLE_WINDOW.start
    assert IN_SAMPLE_WINDOW.start == datetime(2025, 9, 1, tzinfo=UTC)
    assert OUT_OF_SAMPLE_WINDOW.end == datetime(2026, 9, 1, tzinfo=UTC)


def test_walk_forward_test_windows_do_not_overlap() -> None:
    for earlier, later in pairwise(WALK_FORWARD_FOLDS):
        assert later.test.start >= earlier.test.end


# --- Definitions ---------------------------------------------------------------------------------


def test_a_candidate_definition_is_built_and_fully_validated() -> None:
    # Built from the deployed definition, whose symbol rules carry computed fields. Rebuilding
    # through model_dump used to trip over them; this is the path the runner takes.
    base = load_definition(DEPLOYED)
    candidate = next(c for c in CANDIDATES if c.strategy_id == "momentum_roc")
    full = definition_for(candidate, base=base, strategy_version="0.1.0")

    assert full.strategy.strategy_id == "momentum_roc"
    assert full.strategy.params == candidate.params
    assert full.risk.max_consecutive_losses is None
    assert full.role is ExperimentRole.IN_SAMPLE
    assert full.dataset == base.dataset


def test_narrowing_changes_only_the_window_and_the_claim() -> None:
    base = load_definition(DEPLOYED)
    candidate = next(c for c in CANDIDATES if c.strategy_id == "momentum_roc")
    full = definition_for(candidate, base=base, strategy_version="0.1.0")
    oos = narrowed(full, OUT_OF_SAMPLE_WINDOW, ExperimentRole.OUT_OF_SAMPLE)

    assert (oos.dataset.start, oos.dataset.end) == (
        OUT_OF_SAMPLE_WINDOW.start,
        OUT_OF_SAMPLE_WINDOW.end,
    )
    assert oos.role is ExperimentRole.OUT_OF_SAMPLE
    assert oos.strategy == full.strategy
    assert oos.risk == full.risk
    assert oos.experiment_id != full.experiment_id


def test_narrowing_still_enforces_the_frozen_benchmark_rule() -> None:
    # model_copy alone would skip this validator. The round trip is what makes it run.
    base = load_definition(DEPLOYED)
    ema = next(c for c in CANDIDATES if c.strategy_id == "ema_trend")
    full = definition_for(ema, base=base, strategy_version="1.0.0")

    assert full.role is ExperimentRole.BENCHMARK
    with pytest.raises(ValueError, match="frozen benchmark"):
        narrowed(full, OUT_OF_SAMPLE_WINDOW, ExperimentRole.OUT_OF_SAMPLE)


# --- Two risk scenarios, never confused -------------------------------------------------------


def test_the_research_variant_is_never_labelled_risk_v2() -> None:
    assert RiskScenario.DEPLOYED.label == "DEPLOYED RISK V2"
    assert "not Risk V2" in RiskScenario.RESEARCH_VARIANT.label


def test_the_deployed_scenario_runs_the_paper_configuration_unchanged() -> None:
    base = load_definition(DEPLOYED)
    candidate = next(c for c in CANDIDATES if c.strategy_id == "momentum_roc")
    deployed = deployed_definition_for(candidate, base=base, strategy_version="0.1.0")
    research = definition_for(candidate, base=base, strategy_version="0.1.0")

    assert deployed.risk == base.risk
    assert deployed.risk.max_consecutive_losses == 5
    assert deployed.risk.latch_total_drawdown is True
    assert deployed.strategy == research.strategy
    assert deployed.dataset == research.dataset
    assert deployed.experiment_id != research.experiment_id
    assert "deployed" in deployed.name


# --- The research risk variant --------------------------------------------------------------------


def test_the_research_profile_only_unlatches_the_two_latching_breakers() -> None:
    # Under the deployed profile the consecutive-loss breaker latched in the first weeks and
    # refused 3986 of 4008 decisions for the rest of the year. Everything else — sizing, stop,
    # break-even, trailing, take-profit, time stop, daily loss, costs — is left exactly as
    # deployed, so a result here still describes the overlay a paper session would run.
    deployed = load_definition(DEPLOYED).risk
    research = research_risk_variant(deployed)

    assert research.max_consecutive_losses is None
    assert research.latch_total_drawdown is False
    unchanged = research.model_dump(exclude={"max_consecutive_losses", "latch_total_drawdown"})
    assert unchanged == deployed.model_dump(
        exclude={"max_consecutive_losses", "latch_total_drawdown"}
    )


def test_stress_scenarios_only_move_costs() -> None:
    base = load_definition(DEPLOYED)
    base = base.model_copy(update={"risk": research_risk_variant(base.risk)})
    scenarios = stress_scenarios(base)

    assert len(scenarios) == 3
    for scenario in scenarios:
        risk = scenario.risk if scenario.risk is not None else base.risk
        assert risk.model_dump(exclude={"execution_policy"}) == base.risk.model_dump(
            exclude={"execution_policy"}
        )
        assert risk.execution_policy.fee.basis_points >= base.risk.execution_policy.fee.basis_points


# --- The verdict ----------------------------------------------------------------------------------


def _card(**overrides: object) -> Scorecard:
    values: dict[str, object] = {
        "total_return": Decimal("0.08"),
        "max_drawdown": Decimal("0.06"),
        "profit_factor": Decimal("1.4"),
        "expectancy": Decimal("12"),
        "trades": 60,
        "fees": Decimal("40"),
        "slippage": Decimal("20"),
        "win_rate": Decimal("0.45"),
        "average_win": Decimal("60"),
        "average_loss": Decimal("30"),
        "time_in_market": Decimal("0.3"),
        "max_consecutive_losses": 5,
    }
    values.update(overrides)
    return Scorecard(**values)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> Evidence:
    values: dict[str, object] = {
        "full": _card(),
        "out_of_sample_return": Decimal("0.02"),
        "walk_forward_positive_share": Decimal("1"),
        "walk_forward_median_return": Decimal("0.02"),
        "stress_worst_return": Decimal("0.03"),
        "neighbours_min_profit_factor": Decimal("1.2"),
        "benchmark_best_return": Decimal("-0.01"),
        "deployed_return": Decimal("0.03"),
    }
    values.update(overrides)
    return Evidence(**values)  # type: ignore[arg-type]


def test_a_losing_strategy_is_rejected_whatever_else_it_shows() -> None:
    assert judge(_evidence(full=_card(total_return=Decimal("-0.01")))) is Verdict.REJECT
    assert judge(_evidence(full=_card(profit_factor=Decimal("0.9")))) is Verdict.REJECT
    assert judge(_evidence(full=_card(trades=0))) is Verdict.REJECT


def test_a_positive_result_on_too_few_trades_is_only_weak() -> None:
    assert judge(_evidence(full=_card(trades=MEANINGFUL_TRADE_SAMPLE - 1))) is Verdict.WEAK


@pytest.mark.parametrize(
    "overrides",
    [
        {"out_of_sample_return": Decimal("-0.001")},
        {"walk_forward_positive_share": Decimal("0.25")},
        {"walk_forward_median_return": Decimal("-0.001")},
        {"stress_worst_return": Decimal("-0.001")},
        {"neighbours_min_profit_factor": Decimal("0.95")},
    ],
)
def test_failing_any_robustness_check_caps_the_verdict_at_weak(
    overrides: dict[str, object],
) -> None:
    assert judge(_evidence(**overrides)) is Verdict.WEAK


def test_a_deep_drawdown_caps_the_verdict_at_weak() -> None:
    assert judge(_evidence(full=_card(max_drawdown=Decimal("0.18")))) is Verdict.WEAK


def test_robust_but_not_beating_the_benchmarks_is_promising_not_a_candidate() -> None:
    assert judge(_evidence(benchmark_best_return=Decimal("0.20"))) is Verdict.PROMISING


def test_a_paper_candidate_must_clear_every_bar() -> None:
    assert judge(_evidence()) is Verdict.PAPER_CANDIDATE
    assert judge(_evidence(walk_forward_positive_share=Decimal("0.5"))) is Verdict.PROMISING
    assert judge(_evidence(full=_card(profit_factor=Decimal("1.15")))) is Verdict.PROMISING


def test_return_alone_never_earns_a_better_verdict() -> None:
    # The same huge return with a fragile neighbourhood is still only weak.
    fragile = _evidence(
        full=_card(total_return=Decimal("0.90")), neighbours_min_profit_factor=Decimal("0.7")
    )
    assert judge(fragile) is Verdict.WEAK


def test_a_strategy_that_only_works_with_relaxed_breakers_is_not_a_paper_candidate() -> None:
    # Everything else clears the bar, but under the configuration paper would actually run it
    # lost money or never got going. The research variant is for studying edge, not for
    # promoting a strategy past the risk policy it would have to live under.
    assert judge(_evidence(deployed_return=Decimal("-0.004"))) is Verdict.PROMISING
    assert judge(_evidence(deployed_return=Decimal(0))) is Verdict.PROMISING


def test_an_unknown_deployed_result_cannot_earn_paper_candidate() -> None:
    assert judge(_evidence(deployed_return=None)) is Verdict.PROMISING


def test_the_scorecard_flags_a_thin_sample() -> None:
    assert _card(trades=12).low_sample is True
    assert _card(trades=MEANINGFUL_TRADE_SAMPLE).low_sample is False
