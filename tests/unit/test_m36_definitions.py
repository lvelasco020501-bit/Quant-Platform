"""The builder that lets phase 2 reach the corrected universe, and the equality that keeps it safe.

``test_the_six_catalogued_markets_build_exactly_what_m29_builds`` is the whole point of this
file. M36 phase 2 compares a portfolio built on engine positions against one built on signal
timelines, and that comparison is only meaningful if the engine is being driven by the same
definitions the earlier milestones used. A field drifting here would make phase 2 incomparable
with the work it exists to validate, and would do it silently.
"""

from __future__ import annotations

import pytest

from quantplatform.core.enums import Timeframe
from quantplatform.research.m16 import ASSETS
from quantplatform.research.m29 import definition_for as m29_definition_for
from quantplatform.research.m32 import pool_symbols
from quantplatform.research.m36 import SLEEVES
from quantplatform.research.m36_definitions import (
    MARKET_RULES_DIR,
    definition_for,
    market_ref,
    pool_rules,
)

CATALOGUED = tuple(asset.raw for asset in ASSETS)
UNCATALOGUED = tuple(raw for raw in pool_symbols() if raw not in CATALOGUED)


def _probe(key: str) -> object:
    return next(p for p in SLEEVES if p.key == key)


@pytest.mark.parametrize("raw", CATALOGUED)
@pytest.mark.parametrize("sleeve", ["B2", "G1"])
@pytest.mark.parametrize("latching", [False, True])
def test_the_six_catalogued_markets_build_exactly_what_m29_builds(
    raw: str,
    sleeve: str,
    latching: bool,  # noqa: FBT001 - pytest supplies parametrized arguments positionally
) -> None:
    probe = _probe(sleeve)
    mine = definition_for(market_ref(raw, ()), probe, Timeframe.H4, latching=latching)  # type: ignore[arg-type]
    theirs = m29_definition_for(raw, probe, Timeframe.H4, latching=latching)  # type: ignore[arg-type]

    # Every field the engine reads. The name differs by design -- these are different
    # experiments -- and a name changes no behaviour.
    assert mine.strategy == theirs.strategy
    assert mine.dataset == theirs.dataset
    assert mine.backtest == theirs.backtest
    assert mine.risk == theirs.risk
    assert mine.role == theirs.role
    assert mine.name != theirs.name


@pytest.mark.parametrize("raw", CATALOGUED)
def test_a_catalogued_market_needs_no_bars_to_resolve(raw: str) -> None:
    # Its window comes from M16's curated boundary, not from whatever is on disk.
    ref = market_ref(raw, ())

    assert ref.symbol.endswith("/USDT")
    assert ref.start.tzinfo is not None


@pytest.mark.parametrize("raw", UNCATALOGUED)
def test_every_uncatalogued_pool_market_has_captured_venue_rules(raw: str) -> None:
    # Phase 2 claims order rejection as an exercised Risk V2 layer. That claim is false unless
    # every market the portfolio can fund has real tick, step and minimum-notional values.
    path = MARKET_RULES_DIR / f"{raw}.json"

    assert path.exists(), f"run scripts/m36_venue_rules.py: {raw} has no captured rules"


def test_an_uncatalogued_market_refuses_to_resolve_without_bars() -> None:
    # Its start is the first bar on disk, so there is no start without bars. Guessing one would
    # silently change the window a definition runs over.
    with pytest.raises(ValueError, match="no bars"):
        market_ref(UNCATALOGUED[0], ())


def test_a_market_with_no_captured_rules_fails_loudly_and_says_what_to_run() -> None:
    with pytest.raises(FileNotFoundError, match="m36_venue_rules"):
        pool_rules("DEFINITELYNOTAMARKETUSDT")


def test_the_builder_carries_the_frozen_parameters_unchanged() -> None:
    # The sleeves reach through M34 to M29's probe objects; the builder must not reinterpret
    # them on the way to the engine.
    for sleeve in ("B2", "G1"):
        probe = _probe(sleeve)
        built = definition_for(market_ref("BTCUSDT", ()), probe, Timeframe.H4, latching=False)  # type: ignore[arg-type]

        assert built.strategy.params == probe.candidate.params  # type: ignore[attr-defined]
        assert built.strategy.strategy_id == probe.candidate.strategy_id  # type: ignore[attr-defined]


def test_the_reference_policy_differs_from_deployed_only_in_risk() -> None:
    probe = _probe("B2")
    ref = market_ref("BTCUSDT", ())

    research = definition_for(ref, probe, Timeframe.H4, latching=False)  # type: ignore[arg-type]
    deployed = definition_for(ref, probe, Timeframe.H4, latching=True)  # type: ignore[arg-type]

    assert research.strategy == deployed.strategy
    assert research.dataset == deployed.dataset
    assert research.risk != deployed.risk
