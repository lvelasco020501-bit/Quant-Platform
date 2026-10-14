"""Operational invariants about how a session is *installed*, not about what it trades.

Nothing here is imported by the CLI, the paper runner or Mission Control, and that is
deliberate: a module whose job is to police the paper process's import surface has no business
being part of it.
"""

from __future__ import annotations

from quantplatform.ops.paper_isolation import (
    PAPER_COMMAND,
    Isolation,
    PaperDeployment,
    Severity,
    Violation,
    command_line,
    venv_for,
    violations,
)

__all__ = [
    "PAPER_COMMAND",
    "Isolation",
    "PaperDeployment",
    "Severity",
    "Violation",
    "command_line",
    "venv_for",
    "violations",
]
