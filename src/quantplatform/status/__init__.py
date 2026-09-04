"""Reading a paper session's own record of itself, for a person rather than a parser.

An observer domain. It may import ``core``, ``config``, ``storage``, ``reporting`` and
``strategies``; it may not import ``execution``, ``risk``, ``portfolio``, ``paper``,
``backtesting`` or ``orchestration``. That list is the read-only guarantee: this code cannot
place an order, move a stop, reset a breaker or resume a session, because it cannot reach
anything that could.
"""

from quantplatform.status.model import Health, SessionStatus, gather_status
from quantplatform.status.render import render_status, supports_colour

__all__ = [
    "Health",
    "SessionStatus",
    "gather_status",
    "render_status",
    "supports_colour",
]
