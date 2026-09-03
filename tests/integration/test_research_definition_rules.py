"""Pinning the venue's rules into the experiment instead of fetching them again.

Paper trading asks the venue for its trading rules at startup, which is right: it is about to
send real orders and needs today's tick size. A backtest over 2024 asking for today's rules is
a different thing entirely — the same experiment re-run six months later would be sized
against different filters, produce a different result, and be reported as irreproducible with
identical bars and identical code. The engine would stand accused of a change made by Binance.

So the rules are captured once, by the same provider paper uses, and become part of the
experiment. Two runs under different rules are two experiments, which is the truth.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.features import NullFeaturePipeline
from quantplatform.orchestration.research import ExperimentEngineFactory
from quantplatform.research.definition import DatasetSpec, experiment_id
from quantplatform.strategies.registry import StrategyRegistry
from tests.factories import ANCHOR, SYMBOL, make_experiment_definition, make_symbol_rules
from tests.integration.test_backtest_engine import BuyThenSell


def test_a_dataset_must_say_which_rules_the_experiment_ran_under() -> None:
    with pytest.raises(ValueError, match="symbol_rules"):
        DatasetSpec(  # type: ignore[call-arg]
            symbol=SYMBOL,
            market_type=MarketType.SPOT,
            timeframe=Timeframe.H1,
            start=ANCHOR,
            end=ANCHOR.replace(year=2027),
            source="fixture",
        )


def test_a_dataset_must_say_which_market_it_ran_on() -> None:
    with pytest.raises(ValueError, match="market_type"):
        DatasetSpec(  # type: ignore[call-arg]
            symbol=SYMBOL,
            timeframe=Timeframe.H1,
            start=ANCHOR,
            end=ANCHOR.replace(year=2027),
            source="fixture",
            symbol_rules=make_symbol_rules(),
        )


def test_different_rules_make_a_different_experiment() -> None:
    coarse = make_experiment_definition(symbol_rules=make_symbol_rules(price_tick=Decimal("0.01")))
    fine = make_experiment_definition(symbol_rules=make_symbol_rules(price_tick=Decimal("0.001")))

    assert experiment_id(coarse) != experiment_id(fine)


def test_the_market_type_is_part_of_the_experiment_s_name() -> None:
    spot = make_experiment_definition(market_type=MarketType.SPOT)
    perpetual = make_experiment_definition(market_type=MarketType.PERPETUAL)

    assert experiment_id(spot) != experiment_id(perpetual)


def test_the_rules_the_definition_carries_are_the_rules_the_engine_uses() -> None:
    # The point of pinning them. Nothing at run time consults a venue, so the run is the same
    # run whenever it happens.
    rules = make_symbol_rules(price_tick=Decimal("0.5"))
    definition = make_experiment_definition(symbol_rules=rules, strategy_id="buy_then_sell")
    registry = StrategyRegistry()
    registry.register(BuyThenSell)

    engine = ExperimentEngineFactory(
        registry=registry, features_for=lambda _: NullFeaturePipeline(), quote_asset="USDT"
    )(definition)

    assert engine._symbols[SYMBOL].price_tick == Decimal("0.5")
