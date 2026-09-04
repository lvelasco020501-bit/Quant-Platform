"""Running Mission Control.

Separate from the trading process by design: it is started, stopped and restarted on its own
schedule, and nothing it does can reach the session it observes.
"""

from __future__ import annotations

import uvicorn

from quantplatform.web.api import create_app
from quantplatform.web.config import WebSettings

__all__ = ["main"]


def main() -> None:
    """Serve the dashboard on the configured address."""
    settings = WebSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    main()
