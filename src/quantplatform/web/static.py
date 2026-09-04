"""Where the single page and its assets live on disk.

A module rather than a path literal in the app, so the location is stated once and a moved
directory breaks at import rather than at the first request for a stylesheet.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["STATIC_ROOT"]

STATIC_ROOT = Path(__file__).parent / "assets"
"""Directory served at ``/static`` and holding ``index.html``."""
