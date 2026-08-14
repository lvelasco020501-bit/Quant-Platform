"""The one file that names which paper session is running, and under which process.

Two independent incidents share a single root cause, and this module is the answer to
both. Two `paper run` processes were alive at once for eighteen hours, writing into the
same ``var/`` tree — interleaving one log file, leaving another empty, and colliding on a
daily report that only one of them managed to write. Separately, the Control Center
dashboard spent that entire period displaying a session as live while pointed at the
*older* of the two, with nothing anywhere able to contradict it, because a dashboard
reading a state file can only learn what something last wrote — never whether anything is
still running.

Neither failure was a bug in any component. Each component did exactly what it was told.
What was missing was a single, checkable statement of *which session is the running one*,
and this file is that statement.

**A lock, not a registry.** It holds one session, because the whole point is that exactly
one may run against a given state directory. It is deliberately not a list of sessions,
not a history, and not a coordination protocol between peers — anything richer would
invite the very thing it exists to forbid.

**Self-describing on purpose.** The payload is plain JSON with no platform types in it, so
a reader — notably the dashboard, which lives in a separate checkout on a separate branch
— can answer "is this session actually running?" by reading a file and calling
``os.kill(pid, 0)``, without importing this package or sharing a release with it.

**A crash must never permanently block a restart.** A lock whose process is gone is stale,
not authoritative: it is reclaimed with a warning rather than treated as a refusal. The
alternative — an operator locked out by a lock file left behind by a killed process — is a
worse failure than the one this module prevents.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from quantplatform.core.errors import ConfigurationError, StorageError
from quantplatform.core.logging_config import get_logger
from quantplatform.core.timeutils import ensure_utc

__all__ = ["LOCK_FILENAME", "SessionLock", "SessionLockRecord", "read_session_lock"]

_LOGGER = get_logger(__name__)

LOCK_FILENAME: Final[str] = "paper-session.lock"
"""Name of the lock file, alongside the state files it governs.

Co-located with ``var/state/*.json`` deliberately: the directory an operator already
thinks of as "where the session lives" is where they should find the answer to "what is
running". A lock hidden somewhere else is a lock nobody checks.
"""

_TEMP_SUFFIX: Final[str] = ".tmp"


class SessionLockRecord:
    """Who holds the lock: a session name, a process, and when it claimed it.

    A plain object rather than a domain model, because its whole purpose is to survive the
    round trip through JSON that lets a foreign reader interpret it without this package.
    """

    __slots__ = ("pid", "session_id", "started_at")

    def __init__(self, *, session_id: str, pid: int, started_at: datetime) -> None:
        """Record a lock holder.

        Args:
            session_id: The session claiming the lock.
            pid: Process id of the runner that claimed it.
            started_at: When the claim was made.
        """
        self.session_id = session_id
        self.pid = pid
        self.started_at = started_at

    @property
    def is_alive(self) -> bool:
        """Return whether the recorded process still exists.

        Signal ``0`` performs the kernel's own permission-and-existence check without
        delivering anything, which is the standard way to ask "is this pid still there?".
        A ``PermissionError`` means the process exists but belongs to another user — still
        alive, and still a reason to refuse.

        **Known limitation, stated rather than hidden: process ids are reused.** A long-
        dead session's pid can eventually belong to something unrelated, and this check
        would then report a live holder that has nothing to do with trading. The exposure
        is small — the lock is removed on clean shutdown, so the window is a crashed
        process's lifetime — and the failure is closed rather than open: a spurious refusal
        with an actionable message, never a silent second session. :meth:`SessionLock.acquire`
        prints the pid precisely so an operator can check it themselves.
        """
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload written to disk."""
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "started_at": self.started_at.isoformat(),
        }

    def __repr__(self) -> str:
        """Return a representation naming the session and process."""
        return f"SessionLockRecord(session_id={self.session_id!r}, pid={self.pid})"


def read_session_lock(directory: Path) -> SessionLockRecord | None:
    """Return whoever currently holds the lock in ``directory``, if anyone.

    The read half of this module, kept as a free function so a caller that only wants to
    *observe* — a dashboard, a health check, an operator's script — never constructs
    something that looks capable of claiming the lock.

    Args:
        directory: The state directory the lock governs.

    Returns:
        The lock's holder, or ``None`` when no lock file exists or it cannot be read as
        one. An unreadable lock is deliberately reported as absent rather than raised on:
        a corrupt lock must not be able to prevent a reader from rendering a page, and the
        liveness question it would have answered is answered conservatively by its absence.
    """
    path = directory / LOCK_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return SessionLockRecord(
            session_id=str(payload["session_id"]),
            pid=int(payload["pid"]),
            started_at=ensure_utc(datetime.fromisoformat(str(payload["started_at"]))),
        )
    except (KeyError, TypeError, ValueError):
        return None


class SessionLock:
    """Claims, holds and releases the right to be the running paper session."""

    def __init__(self, *, directory: Path, session_id: str, pid: int | None = None) -> None:
        """Prepare a claim.

        Constructing this claims nothing; :meth:`acquire` does.

        Args:
            directory: State directory to lock. The same directory the session's state
                file is written to, so one directory means one session.
            session_id: The session that wants to run.
            pid: Process id to record. Defaults to this process, which is what production
                always wants; a test passes an explicit value to describe a process that
                is not itself.
        """
        self._directory = directory
        self._session_id = session_id
        self._pid = pid if pid is not None else os.getpid()
        self._path = directory / LOCK_FILENAME
        self._held = False

    @property
    def path(self) -> Path:
        """Return where the lock file lives."""
        return self._path

    @property
    def is_held(self) -> bool:
        """Return whether this object currently holds the lock."""
        return self._held

    def acquire(self, *, now: datetime) -> None:
        """Claim the right to run, or refuse because someone else already has it.

        Args:
            now: Current instant, from the caller's injected clock — nothing here reads a
                wall clock of its own.

        Raises:
            ConfigurationError: If another *live* process already holds the lock. The
                message names the holder's session and pid, because the operator's next
                question is always "which one, and is it really running?" and an error
                that does not answer it just moves the investigation elsewhere.
            StorageError: If the lock cannot be written.
        """
        existing = read_session_lock(self._directory)
        if existing is not None and existing.is_alive:
            raise ConfigurationError(
                "another paper session is already running against this state directory; "
                "refusing to start a second one. Two sessions sharing a directory "
                "interleave their logs, collide on daily reports, and leave a dashboard "
                "unable to say which is authoritative",
                running_session_id=existing.session_id,
                running_pid=existing.pid,
                running_since=existing.started_at.isoformat(),
                requested_session_id=self._session_id,
                lock_file=str(self._path),
            )
        if existing is not None:
            _LOGGER.warning(
                "reclaiming a stale session lock; its process is gone",
                extra={
                    "stale_session_id": existing.session_id,
                    "stale_pid": existing.pid,
                    "stale_since": existing.started_at.isoformat(),
                    "session_id": self._session_id,
                },
            )

        record = SessionLockRecord(
            session_id=self._session_id, pid=self._pid, started_at=ensure_utc(now)
        )
        self._write(record)
        self._held = True
        _LOGGER.info(
            "session lock acquired",
            extra={"session_id": self._session_id, "pid": self._pid, "lock_file": str(self._path)},
        )

    def release(self) -> None:
        """Give up the lock. Safe to call repeatedly, and never raises.

        A shutdown path that can fail during shutdown is not a shutdown path. A lock that
        outlives its process is recoverable — the next start reclaims it as stale — so
        failing to remove it is worth a log line and nothing more.
        """
        if not self._held:
            return
        self._held = False
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.warning(
                "could not remove the session lock; the next start will reclaim it as stale",
                extra={"lock_file": str(self._path), "error": type(exc).__name__},
            )
            return
        _LOGGER.info(
            "session lock released",
            extra={"session_id": self._session_id, "lock_file": str(self._path)},
        )

    def _write(self, record: SessionLockRecord) -> None:
        """Write the lock atomically, so a crash mid-write cannot leave a torn one.

        The same temp-file-and-rename discipline
        :class:`~quantplatform.storage.paper_state.FilePaperStateRepository` uses, for the
        same reason: a half-written lock would be unparseable, and an unparseable lock
        reads as *no* lock — which is precisely the state that lets a second session start.

        Raises:
            StorageError: If the lock cannot be written.
        """
        temporary = self._path.with_name(f"{self._path.name}{_TEMP_SUFFIX}")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(record.to_dict(), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._path)
        except OSError as exc:
            # The cleanup is itself allowed to fail. If the directory could not be created
            # at all, unlinking a path *inside* it raises NotADirectoryError, and letting
            # that escape would replace the useful StorageError below with a confusing one
            # about a temp file the caller never knew existed.
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise StorageError(
                "the paper session lock could not be written",
                session_id=self._session_id,
                path=str(self._path),
            ) from exc
