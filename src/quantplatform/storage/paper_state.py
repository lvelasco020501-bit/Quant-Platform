"""Durable paper-session state, one JSON document per session.

A paper session runs for days. A process restart in the middle of one must not silently
reset the account it was tracking, and until now the only implementation of
:class:`~quantplatform.core.interfaces.PaperStateRepository` was in-memory — which is to
say, no implementation at all for anything that outlives a process.

**Crash safety is the whole design.** A snapshot is written to a temporary file in the same
directory, flushed, fsynced, and then moved into place with :meth:`~pathlib.Path.replace`,
which is atomic on POSIX. A reader therefore sees either the previous complete snapshot or
the new complete one, never a half-written mixture. The containing directory is fsynced afterwards
so the rename itself survives a power loss, not merely the bytes. A partially written
snapshot is worse than no snapshot, because it resumes into an account that never existed.

**JSON rather than SQL.** The state is one document read once at startup and written once
per bar; there is nothing relational about it, and requiring a database to be up before a
paper session can start would make the session's availability depend on something it does
not otherwise need.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from quantplatform.core.errors import StorageError
from quantplatform.core.models.paper import PaperSessionState

__all__ = ["FilePaperStateRepository"]

_SUFFIX: Final[str] = ".json"
_TEMP_SUFFIX: Final[str] = ".tmp"

_COMPUTED_FIELD_EXCLUSIONS: Final[dict[str, dict[str, set[str]]]] = {
    "balances": {"__all__": {"total"}},
    "positions": {"__all__": {"cost_basis"}},
}
"""Derived fields that must not be written, because they cannot be read back.

:class:`~quantplatform.core.models.portfolio.Balance` and
:class:`~quantplatform.core.models.portfolio.Position` publish ``total`` and ``cost_basis``
as pydantic computed fields. Those serialise, but the domain models forbid unknown input,
so feeding them back in fails validation. Excluding them on write costs nothing — both are
functions of fields that *are* stored, and are recomputed identically on load.

The alternative was relaxing the domain models, which would have weakened a validation rule
that exists for good reason in order to satisfy a storage detail. Kept here, in the adapter,
where a serialisation quirk belongs. :func:`_assert_exclusions_are_complete` in the test
suite fails if a new computed field appears and this map is not updated.
"""


class FilePaperStateRepository:
    """Stores paper-session snapshots as atomically replaced JSON files."""

    def __init__(self, directory: Path) -> None:
        """Create a repository rooted at a directory.

        Args:
            directory: Where session documents live. Created if absent.

        Raises:
            StorageError: If the directory cannot be created or is not usable.
        """
        self._directory = directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                "paper state directory could not be created", directory=str(directory)
            ) from exc
        if not os.access(directory, os.W_OK):
            raise StorageError("paper state directory is not writable", directory=str(directory))

    @property
    def directory(self) -> Path:
        """Return the directory session documents are stored in."""
        return self._directory

    def path_for(self, session_id: str) -> Path:
        """Return the document path for a session.

        Args:
            session_id: Session identity.

        Returns:
            The file the session's snapshot lives in.

        Raises:
            StorageError: If the identity cannot be used as a file name. A session id
                containing a path separator would write outside the configured directory,
                so it is refused rather than sanitised — silently renaming an operator's
                session would make two sessions collide under one file.
        """
        if not session_id or session_id != Path(session_id).name or session_id in {".", ".."}:
            raise StorageError("session id is not usable as a file name", session_id=session_id)
        return self._directory / f"{session_id}{_SUFFIX}"

    def load(self, session_id: str) -> PaperSessionState | None:
        """Return the stored state for a session, or ``None`` when never saved.

        Args:
            session_id: Session identity.

        Returns:
            The snapshot, or ``None`` if this session has no document.

        Raises:
            StorageError: If a document exists but cannot be read or does not describe a
                valid session. Refusing is deliberate: resuming from a corrupt snapshot
                would restore an account that never existed, and starting fresh instead
                would silently discard the one that did.
        """
        path = self.path_for(session_id)
        if not path.is_file():
            return None
        try:
            payload = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(
                "paper session state could not be read", session_id=session_id, path=str(path)
            ) from exc
        try:
            return PaperSessionState.model_validate_json(payload)
        except ValidationError as exc:
            raise StorageError(
                "stored paper session state is not valid",
                session_id=session_id,
                path=str(path),
                errors=[error["type"] for error in exc.errors()],
            ) from exc

    def save(self, state: PaperSessionState) -> None:
        """Store a session's state, replacing any previous snapshot.

        Written to a temporary file and moved into place, so a crash mid-write leaves the
        previous snapshot intact rather than a truncated one.

        Args:
            state: Snapshot to persist.

        Raises:
            StorageError: If the snapshot cannot be written.
        """
        path = self.path_for(state.session_id)
        payload = state.model_dump_json(indent=2, exclude=_COMPUTED_FIELD_EXCLUSIONS)
        temporary = path.with_name(f"{path.name}{_TEMP_SUFFIX}")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            self._sync_directory()
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StorageError(
                "paper session state could not be written",
                session_id=state.session_id,
                path=str(path),
            ) from exc

    def delete(self, session_id: str) -> None:
        """Remove a session's stored state; a no-op when nothing was stored.

        Raises:
            StorageError: If an existing document cannot be removed.
        """
        path = self.path_for(session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(
                "paper session state could not be removed",
                session_id=session_id,
                path=str(path),
            ) from exc

    def session_ids(self) -> tuple[str, ...]:
        """Return every session with a stored document, in sorted order."""
        return tuple(sorted(path.stem for path in self._directory.glob(f"*{_SUFFIX}")))

    def _sync_directory(self) -> None:
        """Flush the directory entry so the rename survives a power loss.

        Writing the file durably is not enough on its own: without this, a crash can leave
        the new bytes on disk and the directory still pointing at the old name.
        """
        try:
            descriptor = os.open(self._directory, os.O_RDONLY)
        except OSError:  # pragma: no cover - platforms that cannot open a directory
            return
        try:
            os.fsync(descriptor)
        except OSError:  # pragma: no cover - filesystems that refuse directory fsync
            pass
        finally:
            os.close(descriptor)
