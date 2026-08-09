"""Configuration for the live market-data feed.

Every operational parameter of the feed — endpoint, timings, retry budget, subscribed
instruments — is configuration rather than code, and every one of them is validated before
a socket is opened. A feed that discovers its endpoint is wrong only after connecting has
already spent the reconnect budget it will need for a real outage.

**The endpoint validation is a safety boundary, not tidiness.** The URL is the one place a
read-only market-data feed could be turned into something else by editing a config file, so
the checks here refuse anything that is not a public stream: no credentials, no query
string, and none of the paths Binance uses for account, user-data or trading endpoints.
"""

from __future__ import annotations

from typing import Final, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.base import Symbol, VenueId

__all__ = ["MarketDataConfiguration"]

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"ws", "wss"})
"""Only WebSocket schemes. An ``http`` endpoint here would mean a REST poller wired by
mistake into a component whose entire contract is "blocks until the next candle".
"""

_FORBIDDEN_URL_TOKENS: Final[tuple[str, ...]] = (
    "listenkey",
    "userdatastream",
    "user-data",
    "/api/",
    "/sapi/",
    "/fapi/",
    "/dapi/",
    "apikey",
    "api_key",
    "api-key",
    "secret",
    "signature",
    "token",
    "order",
    "account",
    "withdraw",
    "mytrades",
)
"""Substrings that disqualify an endpoint.

Matched case-insensitively against the whole URL. Every one of them marks either an
authenticated endpoint or an account/trading one; a *market-data* stream needs none of
them, so their presence means the endpoint is not what this component is allowed to read.
"""


class MarketDataConfiguration(BaseModel):
    """Validated settings for one streaming market-data feed."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    websocket_url: str = Field(default="wss://stream.binance.com:9443/ws", min_length=1)
    """Public stream endpoint. Must be a credential-free ``ws``/``wss`` URL."""

    symbols: tuple[Symbol, ...] = ("BTC/USDT",)
    """Canonical platform symbols to subscribe to, in ``BASE/QUOTE`` form."""

    timeframe: Timeframe = Timeframe.H1
    market_type: MarketType = MarketType.SPOT

    source_id: VenueId = "binance_spot_ws"
    """Recorded as :attr:`~quantplatform.core.models.market.MarketBar.source` on every bar,
    so a bar's provenance survives into the audit trail and into persistence."""

    receive_timeout_seconds: float = Field(default=5.0, gt=0)
    """How long a single read waits before returning control so the heartbeat can be checked.

    Not an error budget: on a quiet stream, timing out repeatedly is the normal state.
    """

    heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    """Silence beyond which the connection is presumed dead and is reconnected.

    Must exceed :attr:`receive_timeout_seconds`, otherwise the first ordinary read timeout
    would be indistinguishable from a dead socket and the feed would reconnect constantly.
    """

    close_grace_seconds: float = Field(default=2.0, ge=0)
    """Tolerance for our clock lagging the venue's when confirming a candle has closed.

    Grace granted *to the venue*, not withheld from it: a candle the venue has marked
    closed is accepted even if our clock has not quite reached its close timestamp. It can
    never make a forming candle actionable, because the closed flag is checked
    independently and both must agree. See
    :meth:`~quantplatform.marketdata.clock.FeedClock.is_bar_final` for why the opposite
    direction would silently drop candles.
    """

    reconnect_initial_delay_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_delay_seconds: float = Field(default=60.0, gt=0)
    reconnect_backoff_multiplier: float = Field(default=2.0, ge=1.0)
    max_reconnect_attempts: int = Field(default=5, ge=1)
    """Consecutive failed attempts before the feed gives up and raises.

    Deliberately finite. A feed that retries forever turns an outage into a silent stall,
    and a paper session that has stopped receiving candles should end loudly rather than
    sit at a stale portfolio believing it is still trading.
    """

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the endpoint, the instrument list and the timing relationships.

        Raises:
            ValueError: If the endpoint is not a public credential-free stream, the symbol
                list is empty or repeats, the market type is unsupported, or the timings
                are mutually inconsistent.
        """
        self._validate_endpoint()
        if not self.symbols:
            msg = "at least one symbol must be subscribed"
            raise ValueError(msg)
        if len(set(self.symbols)) != len(self.symbols):
            msg = "symbols must not repeat"
            raise ValueError(msg)
        if self.market_type is not MarketType.SPOT:
            msg = "only spot market data is supported"
            raise ValueError(msg)
        if self.heartbeat_timeout_seconds <= self.receive_timeout_seconds:
            msg = "heartbeat_timeout_seconds must exceed receive_timeout_seconds"
            raise ValueError(msg)
        if self.reconnect_max_delay_seconds < self.reconnect_initial_delay_seconds:
            msg = "reconnect_max_delay_seconds must not be below reconnect_initial_delay_seconds"
            raise ValueError(msg)
        return self

    def _validate_endpoint(self) -> None:
        """Refuse any endpoint that is not a public, credential-free market-data stream.

        Raises:
            ValueError: If the URL carries credentials, a query string, a non-WebSocket
                scheme, or a path belonging to an authenticated or trading endpoint.
        """
        parts = urlsplit(self.websocket_url)
        if parts.scheme not in _ALLOWED_SCHEMES:
            msg = f"websocket_url must use ws or wss, got {parts.scheme!r}"
            raise ValueError(msg)
        if not parts.hostname:
            msg = "websocket_url must name a host"
            raise ValueError(msg)
        if parts.username is not None or parts.password is not None:
            msg = "websocket_url must not embed credentials"
            raise ValueError(msg)
        if parts.query:
            msg = "websocket_url must not carry a query string"
            raise ValueError(msg)
        lowered = self.websocket_url.lower()
        for token in _FORBIDDEN_URL_TOKENS:
            if token in lowered:
                msg = f"websocket_url must be a public market-data stream; it contains {token!r}"
                raise ValueError(msg)

    def venue_symbol(self, symbol: str) -> str:
        """Return the venue's spelling of a canonical symbol.

        Args:
            symbol: Canonical platform symbol such as ``BTC/USDT``.

        Returns:
            The venue symbol, for example ``BTCUSDT``.
        """
        return symbol.replace("/", "")

    def stream_name(self, symbol: str) -> str:
        """Return the venue stream identifier for a symbol at the configured timeframe.

        Args:
            symbol: Canonical platform symbol.

        Returns:
            A stream name such as ``btcusdt@kline_1h``.
        """
        return f"{self.venue_symbol(symbol).lower()}@kline_{self.timeframe.value}"
