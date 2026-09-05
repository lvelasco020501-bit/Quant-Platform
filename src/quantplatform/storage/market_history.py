"""Persisting the candles a session has seen, so a restart is not blind.

Append-only JSON Lines: a manifest on the first line, then one candle per line in the order
the session processed them. The format is chosen for what goes wrong rather than for what
goes right — a process killed mid-write leaves a torn final line and nothing else, which is
detectable and discardable, where a rewritten file could be left half-replaced with no way
to tell.

**This module stores market data and nothing else.** There is no path through it by which a
balance, a position, an order or a fill could be written or read, because the only thing it
serialises is a :class:`~quantplatform.core.models.market.MarketBar`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Self

from pydantic import ValidationError

from quantplatform.core.errors import StorageError
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.warm_start import (
    MarketHistory,
    MarketHistoryManifest,
    history_digest,
)

__all__ = ["FileMarketHistoryRepository"]

_SUFFIX = ".history.jsonl"


class FileMarketHistoryRepository:
    """Reads and appends one session's market history."""

    def __init__(self, directory: Path, *, writable: bool = True) -> None:
        """Open a repository rooted at a directory.

        Args:
            directory: Where history files live. Created if absent when writable.
            writable: Whether this repository will be appended to. Use :meth:`for_reading`
                rather than passing ``False``.

        Raises:
            StorageError: If the directory cannot be created or is not writable.
        """
        self._directory = directory
        if not writable:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                "market history directory could not be created", directory=str(directory)
            ) from exc
        if not os.access(directory, os.W_OK):
            raise StorageError("market history directory is not writable", directory=str(directory))

    @classmethod
    def for_reading(cls, directory: Path) -> Self:
        """Open a repository that will only ever be read from.

        A reader needs neither to create the directory nor to hold write permission on it,
        and demanding either would over-privilege every process that only wants to look.
        """
        return cls(directory, writable=False)

    def path_for(self, session_id: str) -> Path:
        """Return the history file for a session.

        Raises:
            StorageError: If the identity cannot be used as a file name. A session id
                containing a path separator would read or write outside the configured
                directory, so it is refused rather than sanitised.
        """
        if not session_id or session_id != Path(session_id).name or session_id in {".", ".."}:
            raise StorageError("session id is not usable as a file name", session_id=session_id)
        return self._directory / f"{session_id}{_SUFFIX}"

    def start(self, manifest: MarketHistoryManifest) -> None:
        """Begin a history file by writing its manifest.

        Called once, before any candle. A file that already exists is left alone: appending
        is the whole point, and rewriting the manifest of a run in progress would discard
        the binding that makes the file safe to load.

        Raises:
            StorageError: If the file cannot be written.
        """
        path = self.path_for(manifest.source_session_id)
        if path.exists():
            return
        try:
            with path.open("w", encoding="utf-8") as handle:
                handle.write(manifest.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError(
                "market history manifest could not be written", path=str(path)
            ) from exc

    def append(self, session_id: str, bar: MarketBar) -> None:
        """Append one processed candle.

        Flushed and fsynced on every write. A history that lags the snapshot it is
        cross-checked against would be rejected at load, so buying throughput by leaving
        candles in a buffer would buy a file that fails exactly when it is needed.

        Raises:
            StorageError: If the candle cannot be written.
        """
        path = self.path_for(session_id)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(bar.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageError(
                "market history candle could not be appended", path=str(path), session_id=session_id
            ) from exc

    def load(self, session_id: str) -> MarketHistory | None:
        """Return a session's validated history, or ``None`` when it has none.

        A torn final line — the signature of a process killed mid-append — is dropped rather
        than treated as corruption, because the candle it describes was never fully
        processed either. Every other malformation is refused: a history that is wrong in a
        way we cannot name is a history that could seed an indicator with a number nobody
        can account for.

        Raises:
            StorageError: If the file exists but cannot be read or does not describe one
                coherent, contiguous window of a single instrument.
        """
        path = self.path_for(session_id)
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise StorageError(
                "market history could not be read", session_id=session_id, path=str(path)
            ) from exc
        if not lines:
            raise StorageError(
                "market history file is empty; it should hold at least a manifest",
                session_id=session_id,
                path=str(path),
            )

        try:
            manifest = MarketHistoryManifest.model_validate_json(lines[0])
        except ValidationError as exc:
            raise StorageError(
                "market history manifest is not valid",
                session_id=session_id,
                path=str(path),
                errors=[error["type"] for error in exc.errors()],
            ) from exc
        if manifest.source_session_id != session_id:
            raise StorageError(
                "market history names a different session than the file it lives in",
                session_id=session_id,
                manifest_session_id=manifest.source_session_id,
                path=str(path),
            )

        bars = self._parse_bars(lines[1:], session_id=session_id, path=path)
        if not bars:
            return None
        try:
            return MarketHistory(
                manifest=manifest,
                bars=bars,
                bars_count=len(bars),
                first_bar_close_time=bars[0].close_time,
                last_bar_close_time=bars[-1].close_time,
                digest=history_digest(bars),
            )
        except ValidationError as exc:
            raise StorageError(
                "market history is not a coherent window",
                session_id=session_id,
                path=str(path),
                errors=[str(error["msg"]) for error in exc.errors()],
            ) from exc

    def _parse_bars(
        self, lines: list[str], *, session_id: str, path: Path
    ) -> tuple[MarketBar, ...]:
        """Parse candle lines, tolerating only a torn final one."""
        bars: list[MarketBar] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                bars.append(MarketBar.model_validate_json(line))
            except ValidationError as exc:
                is_last = index == len(lines) - 1
                if is_last:
                    # A process killed mid-append leaves half a line. The candle it would
                    # have described was not fully processed either, so dropping it keeps
                    # the file consistent with the snapshot rather than ahead of it.
                    break
                raise StorageError(
                    "market history holds a malformed candle",
                    session_id=session_id,
                    path=str(path),
                    line=index + 2,
                    errors=[error["type"] for error in exc.errors()],
                ) from exc
        return tuple(bars)
