"""Mission Control: a read-only web view of a paper trading session.

An observer domain. It may import ``core``, ``config``, ``status`` and ``strategies``; it may
not import ``execution``, ``risk``, ``portfolio``, ``paper`` or ``backtesting``. That list is
the guarantee: this service cannot start a session, place an order, move a stop, clear a
breaker or resume anything, because it cannot reach any code that could.
"""

from quantplatform.web.api import create_app
from quantplatform.web.config import SCHEMA_VERSION, WebSettings

__all__ = ["SCHEMA_VERSION", "WebSettings", "create_app"]
