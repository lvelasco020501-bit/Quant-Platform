"""Pre-7C: venue trading rules parsed from Binance's public metadata.

No test here touches the network. Every case — a missing filter, a delisted symbol, a
malformed decimal — is a fixture handed to a transport double, which is the only way those
cases can be exercised on demand.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from quantplatform.core.clock import SimulatedClock
from quantplatform.core.enums import MarketType
from quantplatform.core.errors import (
    ConfigurationError,
    DataIntegrityError,
    DataProviderError,
    MarketDataSubscriptionError,
)
from quantplatform.marketdata.symbol_rules import (
    DEFAULT_EXCHANGE_INFO_URL,
    BinanceSpotSymbolRulesProvider,
    ExchangeInfoTransport,
)
from tests.factories import ANCHOR


class _StaticTransport:
    """Returns a prepared document, recording how often it was asked."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def fetch(self, url: str) -> str:
        self.calls += 1
        self.url = url
        return self.payload


def _filters(**overrides: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - venue payloads are heterogeneous
    """Binance's real filter shapes, as published for BTCUSDT."""
    defaults: dict[str, dict[str, Any]] = {
        "PRICE_FILTER": {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01000000",
            "maxPrice": "1000000.00000000",
            "tickSize": "0.01000000",
        },
        "LOT_SIZE": {
            "filterType": "LOT_SIZE",
            "minQty": "0.00001000",
            "maxQty": "9000.00000000",
            "stepSize": "0.00001000",
        },
        "NOTIONAL": {
            "filterType": "NOTIONAL",
            "minNotional": "5.00000000",
            "maxNotional": "9000000.00000000",
        },
    }
    defaults.update(overrides)
    return [value for value in defaults.values() if value]


def _document(**overrides: Any) -> str:  # noqa: ANN401 - venue payloads are heterogeneous
    entry: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "permissions": ["SPOT"],
        "filters": _filters(),
    }
    entry.update(overrides)
    return json.dumps({"timezone": "UTC", "symbols": [entry]})


def _provider(payload: str) -> tuple[BinanceSpotSymbolRulesProvider, _StaticTransport]:
    transport = _StaticTransport(payload)
    return (
        BinanceSpotSymbolRulesProvider(clock=SimulatedClock(ANCHOR), transport=transport),
        transport,
    )


# --- Happy path ---------------------------------------------------------------------------------


def test_the_transport_double_satisfies_the_port() -> None:
    assert isinstance(_StaticTransport("{}"), ExchangeInfoTransport)


def test_a_real_shaped_response_maps_onto_validated_rules() -> None:
    provider, _ = _provider(_document())

    rules = provider.fetch(["BTC/USDT"])["BTC/USDT"]

    assert rules.symbol == "BTC/USDT"
    assert rules.base_asset == "BTC"
    assert rules.quote_asset == "USDT"
    assert rules.market_type is MarketType.SPOT
    assert rules.price_tick == Decimal("0.01000000")
    assert rules.quantity_step == Decimal("0.00001000")
    assert rules.min_quantity == Decimal("0.00001000")
    assert rules.max_quantity == Decimal("9000.00000000")
    assert rules.min_notional == Decimal("5.00000000")
    assert rules.max_notional == Decimal("9000000.00000000")
    assert rules.source == "binance_spot:BTCUSDT"
    assert rules.updated_at == ANCHOR


def test_every_numeric_value_is_an_exact_decimal() -> None:
    # A tick size that went through a float is a tick size that rounds wrongly.
    provider, _ = _provider(_document())

    rules = provider.fetch(["BTC/USDT"])["BTC/USDT"]

    for value in (rules.price_tick, rules.quantity_step, rules.min_quantity, rules.min_notional):
        assert isinstance(value, Decimal)
    assert str(rules.price_tick) == "0.01000000"


def test_the_older_min_notional_filter_spelling_is_accepted() -> None:
    # Binance renamed this filter; both spellings still appear depending on the symbol.
    payload = _document(
        filters=_filters(
            NOTIONAL={},
            MIN_NOTIONAL={"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000"},
        )
    )
    provider, _ = _provider(payload)

    assert provider.fetch(["BTC/USDT"])["BTC/USDT"].min_notional == Decimal("10")


def test_an_absent_ceiling_is_none_rather_than_zero() -> None:
    # Binance writes 0 for "no ceiling"; a maximum of zero would forbid every order.
    payload = _document(
        filters=_filters(
            NOTIONAL={"filterType": "NOTIONAL", "minNotional": "5.0", "maxNotional": "0"},
            LOT_SIZE={
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "0",
                "stepSize": "0.001",
            },
        )
    )
    provider, _ = _provider(payload)

    rules = provider.fetch(["BTC/USDT"])["BTC/USDT"]

    assert rules.max_notional is None
    assert rules.max_quantity is None


def test_rules_are_cached_for_the_process() -> None:
    provider, transport = _provider(_document())

    provider.fetch(["BTC/USDT"])
    provider.fetch(["BTC/USDT"])

    assert transport.calls == 1
    assert provider.cached_symbols == ("BTC/USDT",)


# --- Refusals -----------------------------------------------------------------------------------


def test_a_symbol_the_venue_does_not_list_is_refused() -> None:
    provider, _ = _provider(_document())

    with pytest.raises(MarketDataSubscriptionError, match="does not list"):
        provider.fetch(["DOGE/USDT"])


def test_a_symbol_that_is_not_trading_is_refused() -> None:
    provider, _ = _provider(_document(status="HALT"))

    with pytest.raises(MarketDataSubscriptionError, match="not currently trading"):
        provider.fetch(["BTC/USDT"])


def test_a_symbol_without_spot_permission_is_refused() -> None:
    provider, _ = _provider(_document(permissions=["MARGIN"]))

    with pytest.raises(MarketDataSubscriptionError, match="does not permit spot"):
        provider.fetch(["BTC/USDT"])


@pytest.mark.parametrize("dropped", ["LOT_SIZE", "PRICE_FILTER"])
def test_a_missing_required_filter_is_refused(dropped: str) -> None:
    # Defaulting would size orders against a limit the venue never published.
    provider, _ = _provider(_document(filters=_filters(**{dropped: {}})))

    with pytest.raises(DataIntegrityError, match="missing a required filter"):
        provider.fetch(["BTC/USDT"])


def test_a_missing_notional_filter_is_refused() -> None:
    provider, _ = _provider(_document(filters=_filters(NOTIONAL={})))

    with pytest.raises(DataIntegrityError, match="missing a notional filter"):
        provider.fetch(["BTC/USDT"])


def test_an_entry_with_no_filters_at_all_is_refused() -> None:
    provider, _ = _provider(_document(filters="not-a-list"))

    with pytest.raises(DataIntegrityError, match="carries no filters"):
        provider.fetch(["BTC/USDT"])


@pytest.mark.parametrize("tick", ["not-a-number", None, 0.01])
def test_an_unparseable_decimal_is_refused(tick: object) -> None:
    payload = _document(
        filters=_filters(
            PRICE_FILTER={"filterType": "PRICE_FILTER", "tickSize": tick}
            if tick is not None
            else {"filterType": "PRICE_FILTER"}
        )
    )
    provider, _ = _provider(payload)

    with pytest.raises(DataIntegrityError):
        provider.fetch(["BTC/USDT"])


def test_assets_that_disagree_with_the_symbol_are_refused() -> None:
    provider, _ = _provider(_document(baseAsset="ETH"))

    with pytest.raises(DataIntegrityError, match="do not match the canonical symbol"):
        provider.fetch(["BTC/USDT"])


def test_a_non_json_document_is_refused() -> None:
    provider, _ = _provider("<html>rate limited</html>")

    with pytest.raises(DataProviderError, match="not valid JSON"):
        provider.fetch(["BTC/USDT"])


def test_a_document_without_a_symbol_list_is_refused() -> None:
    provider, _ = _provider(json.dumps({"timezone": "UTC"}))

    with pytest.raises(DataProviderError, match="does not carry a symbol list"):
        provider.fetch(["BTC/USDT"])


# --- Endpoint safety ----------------------------------------------------------------------------


def test_the_default_endpoint_is_public_binance_metadata() -> None:
    assert DEFAULT_EXCHANGE_INFO_URL == "https://api.binance.com/api/v3/exchangeInfo"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.binance.com/api/v3/exchangeInfo",
        "https://user:secret@api.binance.com/api/v3/exchangeInfo",
        "https://api.binance.com/api/v3/order",
        "https://api.binance.com/api/v3/account",
        "https://api.binance.com/api/v3/userDataStream",
        "https://api.binance.com/sapi/v1/capital/withdraw/apply",
        "https://api.binance.com/api/v3/myTrades",
    ],
)
def test_an_endpoint_that_is_not_public_metadata_is_refused(url: str) -> None:
    # The one place a config edit could turn a read-only component into an account client.
    with pytest.raises(ConfigurationError):
        BinanceSpotSymbolRulesProvider(clock=SimulatedClock(ANCHOR), url=url)


def test_the_provider_never_sends_a_credential() -> None:
    provider, transport = _provider(_document())

    provider.fetch(["BTC/USDT"])

    assert transport.url == DEFAULT_EXCHANGE_INFO_URL
    assert "key" not in transport.url.lower()
    assert "signature" not in transport.url.lower()
