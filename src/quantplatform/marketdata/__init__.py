"""Live market data: the platform's only connection to a real exchange.

This package is where the outside world enters, and it is deliberately the narrowest door
in the system. It reads public candle streams and produces
:class:`~quantplatform.core.models.market.MarketBar` objects. That is the whole contract.

**It cannot trade.** There is no order method, no balance lookup, no account or user-data
stream, no signed request and no credential anywhere in the package. The endpoint is
validated to be a public market-data stream before a socket is opened. Making this package
place a trade is not a matter of adding a call — nothing in its vocabulary can describe one.

**Nothing downstream knows it exists.** The feed satisfies
:class:`~quantplatform.core.interfaces.PaperMarketDataFeed`, the port a paper session
already consumed in Phase 6, so a live stream drops in exactly where a replay double sat.
The backtest engine, risk engine, simulated broker and portfolio engine cannot tell whether
a bar came from a CSV file, a recorded replay or a WebSocket — which is precisely what makes
a paper run against real data evidence about live behaviour rather than a new code path.

**What is real here and what is still simulated.** The market data is real. Execution is
not: bars flow into the same simulated broker and virtual portfolio as ever. This package
brings the platform to the point where the only remaining fiction is the fills.
"""

from __future__ import annotations

from quantplatform.marketdata.clock import FeedClock
from quantplatform.marketdata.config import MarketDataConfiguration
from quantplatform.marketdata.feed import BinanceSpotMarketDataFeed, WebSocketCandleTransport
from quantplatform.marketdata.models import (
    CandleAdmission,
    CandleOutcome,
    FeedMetrics,
    GapReport,
    RejectedCandle,
    StreamSubscription,
)
from quantplatform.marketdata.reconnect import BackoffSchedule, ReconnectPolicy
from quantplatform.marketdata.validation import CandleParser, CandleSequenceValidator

__all__ = [
    "BackoffSchedule",
    "BinanceSpotMarketDataFeed",
    "CandleAdmission",
    "CandleOutcome",
    "CandleParser",
    "CandleSequenceValidator",
    "FeedClock",
    "FeedMetrics",
    "GapReport",
    "MarketDataConfiguration",
    "ReconnectPolicy",
    "RejectedCandle",
    "StreamSubscription",
    "WebSocketCandleTransport",
]
