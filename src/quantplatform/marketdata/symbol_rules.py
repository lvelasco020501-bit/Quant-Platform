"""Venue trading rules, fetched from Binance's public metadata endpoint.

Tick size, lot step and minimum notional are facts about the venue, not settings. Hard-coding
them is how an order gets sized against a limit the exchange stopped enforcing a year ago —
and the failure is silent, because a rejected order looks like a strategy that did not trade.
So they are fetched, parsed exactly, and validated before anything sizes anything.

**Read-only, unauthenticated, market-data only.** ``/api/v3/exchangeInfo`` is public: no key,
no signature, no account, no orders. The URL is validated before a request is made, and an
architecture test asserts this module can neither sign a request nor name an account or
order endpoint. It is the same guarantee the candle feed carries, for the same reason.

**Decimal from end to end.** Binance publishes filters as decimal strings. They are parsed
straight into :class:`~decimal.Decimal`; no value passes through a float, because a tick size
of ``0.01`` that becomes ``0.010000000000000000208`` is a tick size that rounds wrongly.

**Cached for the process, never on disk.** Rules change rarely but they do change, and a
cache that outlived the process would let a restart pick up last month's minimum notional
without anyone choosing that.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

from quantplatform.core.clock import Clock
from quantplatform.core.enums import MarketType
from quantplatform.core.errors import (
    ConfigurationError,
    DataIntegrityError,
    DataProviderError,
    MarketDataSubscriptionError,
)
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.numeric import to_decimal
from quantplatform.core.timeutils import ensure_utc

__all__ = [
    "BinanceSpotSymbolRulesProvider",
    "ExchangeInfoTransport",
    "HttpExchangeInfoTransport",
]

DEFAULT_EXCHANGE_INFO_URL: Final[str] = "https://api.binance.com/api/v3/exchangeInfo"

_TRADING_STATUS: Final[str] = "TRADING"
_SPOT_PERMISSION: Final[str] = "SPOT"

_LOT_SIZE: Final[str] = "LOT_SIZE"
_PRICE_FILTER: Final[str] = "PRICE_FILTER"
_NOTIONAL_FILTERS: Final[tuple[str, ...]] = ("NOTIONAL", "MIN_NOTIONAL")
"""Binance renamed this filter; both spellings appear in the wild depending on the symbol."""

_FORBIDDEN_URL_TOKENS: Final[tuple[str, ...]] = (
    "listenkey",
    "userdatastream",
    "/order",
    "/account",
    "withdraw",
    "mytrades",
    "apikey",
    "api_key",
    "signature",
)
"""Substrings that would mean the endpoint is not public metadata.

``/api/`` is deliberately *not* here — unlike the WebSocket case, the metadata endpoint
genuinely lives under it. What is excluded is anything account-, order- or
credential-shaped, which public exchange info never needs.
"""


@runtime_checkable
class ExchangeInfoTransport(Protocol):
    """Fetches the venue's metadata document as text.

    A port for the same reason the candle stream has one: every interesting case — a missing
    filter, a delisted symbol, a malformed decimal — has to be reproducible without a
    network, and none of them can be arranged against the real venue on demand.
    """

    def fetch(self, url: str) -> str:
        """Return the document at a URL.

        Raises:
            DataProviderError: If the document cannot be retrieved.
        """
        ...


class HttpExchangeInfoTransport:
    """An :class:`ExchangeInfoTransport` over a plain HTTPS GET.

    Deliberately the thinnest possible adapter: one request, no authentication, no headers
    beyond a user agent, no retries. Retrying a metadata fetch would hide an outage that
    startup ought to fail on.
    """

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        """Create a transport.

        Args:
            timeout_seconds: How long to wait for the whole response.
        """
        self._timeout = timeout_seconds

    def fetch(self, url: str) -> str:
        """Fetch the metadata document.

        Raises:
            DataProviderError: If the request fails or returns a non-text body.
        """
        import urllib.error  # noqa: PLC0415 - kept local so the port has no import cost
        import urllib.request  # noqa: PLC0415

        request = urllib.request.Request(  # noqa: S310 - scheme validated by the provider
            url, headers={"User-Agent": "quantplatform/exchange-info"}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return str(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
            raise DataProviderError(
                "venue metadata could not be fetched", error=type(exc).__name__
            ) from exc


class BinanceSpotSymbolRulesProvider:
    """Turns Binance's public exchange info into validated :class:`SymbolRules`."""

    def __init__(
        self,
        *,
        clock: Clock,
        transport: ExchangeInfoTransport | None = None,
        url: str = DEFAULT_EXCHANGE_INFO_URL,
    ) -> None:
        """Create a provider.

        Args:
            clock: Injected time source, stamped onto each rule set as ``updated_at``.
            transport: How the document is retrieved. A real HTTPS GET when omitted.
            url: Public metadata endpoint.

        Raises:
            ConfigurationError: If the URL is not a public, credential-free HTTPS endpoint.
        """
        _validate_endpoint(url)
        self._clock = clock
        self._transport = transport if transport is not None else HttpExchangeInfoTransport()
        self._url = url
        self._cache: dict[str, SymbolRules] = {}

    @property
    def url(self) -> str:
        """Return the metadata endpoint in use."""
        return self._url

    @property
    def cached_symbols(self) -> tuple[str, ...]:
        """Return the canonical symbols already fetched in this process, sorted."""
        return tuple(sorted(self._cache))

    def fetch(self, symbols: Sequence[str]) -> dict[str, SymbolRules]:
        """Fetch and validate the venue's rules for a set of canonical symbols.

        One request covers every symbol: the document describes the whole venue, and asking
        for it once per instrument would multiply the failure modes for no benefit.

        Args:
            symbols: Canonical platform symbols, ``BTC/USDT`` form.

        Returns:
            Validated rules per canonical symbol.

        Raises:
            DataProviderError: If the document cannot be fetched or parsed.
            MarketDataSubscriptionError: If a requested symbol is absent from the venue, or
                is not currently trading as spot.
            DataIntegrityError: If a symbol's filters are missing or unparseable.
        """
        wanted = {_venue_symbol(symbol): symbol for symbol in symbols}
        missing = [symbol for symbol in symbols if symbol not in self._cache]
        if not missing:
            return {symbol: self._cache[symbol] for symbol in symbols}

        document = self._load()
        fetched_at = ensure_utc(self._clock.now())
        found: dict[str, SymbolRules] = {}
        for entry in document:
            venue_symbol = str(entry.get("symbol", ""))
            canonical = wanted.get(venue_symbol)
            if canonical is None:
                continue
            found[canonical] = self._build(entry, canonical=canonical, fetched_at=fetched_at)

        absent = sorted(set(symbols) - set(found))
        if absent:
            raise MarketDataSubscriptionError(
                "the venue does not list these symbols", symbols=absent, url=self._url
            )
        self._cache.update(found)
        return {symbol: self._cache[symbol] for symbol in symbols}

    def _load(self) -> list[dict[str, Any]]:
        """Fetch the document and return its symbol entries.

        Raises:
            DataProviderError: If the response is not a JSON object carrying a symbol list.
        """
        payload = self._transport.fetch(self._url)
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DataProviderError("venue metadata is not valid JSON", url=self._url) from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("symbols"), list):
            raise DataProviderError("venue metadata does not carry a symbol list", url=self._url)
        return [entry for entry in decoded["symbols"] if isinstance(entry, dict)]

    def _build(
        self, entry: Mapping[str, Any], *, canonical: str, fetched_at: datetime
    ) -> SymbolRules:
        """Map one venue entry onto validated trading rules.

        Raises:
            MarketDataSubscriptionError: If the symbol is not currently spot-tradable.
            DataIntegrityError: If required filters are absent or unparseable.
        """
        venue_symbol = str(entry.get("symbol"))
        status = str(entry.get("status", ""))
        if status != _TRADING_STATUS:
            raise MarketDataSubscriptionError(
                "the venue is not currently trading this symbol",
                symbol=canonical,
                status=status,
            )
        permissions = [str(item) for item in entry.get("permissions", [])]
        sets = [str(item) for group in entry.get("permissionSets", []) for item in group]
        if permissions and _SPOT_PERMISSION not in permissions and _SPOT_PERMISSION not in sets:
            raise MarketDataSubscriptionError(
                "the venue does not permit spot trading on this symbol",
                symbol=canonical,
                permissions=permissions,
            )

        filters = _filters(entry, symbol=canonical)
        lot = _require_filter(filters, _LOT_SIZE, symbol=canonical)
        price = _require_filter(filters, _PRICE_FILTER, symbol=canonical)
        notional = _first_filter(filters, _NOTIONAL_FILTERS, symbol=canonical)

        base = str(entry.get("baseAsset", ""))
        quote = str(entry.get("quoteAsset", ""))
        if f"{base}/{quote}" != canonical:
            raise DataIntegrityError(
                "the venue's assets do not match the canonical symbol",
                symbol=canonical,
                venue_symbol=venue_symbol,
                base_asset=base,
                quote_asset=quote,
            )

        return SymbolRules(
            symbol=canonical,
            base_asset=base,
            quote_asset=quote,
            market_type=MarketType.SPOT,
            price_tick=_decimal(price, "tickSize", symbol=canonical),
            quantity_step=_decimal(lot, "stepSize", symbol=canonical),
            min_quantity=_decimal(lot, "minQty", symbol=canonical),
            max_quantity=_optional_decimal(lot, "maxQty", symbol=canonical),
            min_notional=_decimal(notional, "minNotional", symbol=canonical),
            max_notional=_optional_decimal(notional, "maxNotional", symbol=canonical),
            source=f"binance_spot:{venue_symbol}",
            updated_at=fetched_at,
        )


def _validate_endpoint(url: str) -> None:
    """Refuse anything that is not a public, credential-free metadata endpoint.

    Raises:
        ConfigurationError: If the scheme is not HTTPS, credentials are embedded, or the
            path names an account, order or authenticated endpoint.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ConfigurationError("the exchange-info endpoint must use https", scheme=parts.scheme)
    if not parts.hostname:
        raise ConfigurationError("the exchange-info endpoint must name a host")
    if parts.username is not None or parts.password is not None:
        raise ConfigurationError("the exchange-info endpoint must not embed credentials")
    lowered = url.lower()
    for token in _FORBIDDEN_URL_TOKENS:
        if token in lowered:
            raise ConfigurationError(
                "the exchange-info endpoint must be public metadata", contains=token
            )


def _venue_symbol(symbol: str) -> str:
    """Return the venue's spelling of a canonical symbol."""
    return symbol.replace("/", "")


def _filters(entry: Mapping[str, Any], *, symbol: str) -> dict[str, Mapping[str, Any]]:
    """Index a symbol's filters by type.

    Raises:
        DataIntegrityError: If the entry carries no filter list.
    """
    raw = entry.get("filters")
    if not isinstance(raw, list):
        raise DataIntegrityError("the venue entry carries no filters", symbol=symbol)
    return {
        str(item.get("filterType")): item
        for item in raw
        if isinstance(item, dict) and item.get("filterType") is not None
    }


def _require_filter(
    filters: Mapping[str, Mapping[str, Any]], name: str, *, symbol: str
) -> Mapping[str, Any]:
    """Return a required filter.

    Raises:
        DataIntegrityError: If it is absent. Defaulting would mean sizing orders against a
            limit the venue never published.
    """
    found = filters.get(name)
    if found is None:
        raise DataIntegrityError(
            "the venue entry is missing a required filter",
            symbol=symbol,
            filter=name,
            present=sorted(filters),
        )
    return found


def _first_filter(
    filters: Mapping[str, Mapping[str, Any]], names: Sequence[str], *, symbol: str
) -> Mapping[str, Any]:
    """Return the first of several acceptable filter spellings.

    Raises:
        DataIntegrityError: If none of them is present.
    """
    for name in names:
        found = filters.get(name)
        if found is not None:
            return found
    raise DataIntegrityError(
        "the venue entry is missing a notional filter",
        symbol=symbol,
        accepted=list(names),
        present=sorted(filters),
    )


def _decimal(source: Mapping[str, Any], field: str, *, symbol: str) -> Decimal:
    """Read a required decimal filter value.

    Raises:
        DataIntegrityError: If it is absent or not an exact decimal. Binance publishes these
            as strings; a float here would corrupt a tick size before anything used it.
    """
    if field not in source:
        raise DataIntegrityError(
            "the venue filter is missing a required value", symbol=symbol, field=field
        )
    try:
        return to_decimal(source[field])
    except (ValueError, TypeError) as exc:
        raise DataIntegrityError(
            "the venue filter value is not a valid decimal",
            symbol=symbol,
            field=field,
            received=repr(source[field]),
        ) from exc


def _optional_decimal(source: Mapping[str, Any], field: str, *, symbol: str) -> Decimal | None:
    """Read an optional decimal filter value, treating zero as absent.

    Binance uses ``0`` to mean "no ceiling" on ``maxNotional`` and similar fields, which is
    not the same as a maximum of zero — that would forbid every order.
    """
    if field not in source or source[field] is None:
        return None
    value = _decimal(source, field, symbol=symbol)
    return None if value <= 0 else value
