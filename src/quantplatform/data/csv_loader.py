"""Reading the canonical historical CSV format.

This module is responsible for transport only: reading bytes, checksumming them, matching
the header explicitly, and turning each data row into a
:class:`~quantplatform.data.records.RawBarRecord` of untouched strings. It parses no
numbers and validates no values — that is
:mod:`quantplatform.data.validation`'s job — because a malformed cell must still be
quotable in a finding afterwards.

Only the one canonical schema documented in :data:`~quantplatform.data.records.CANONICAL_COLUMNS`
is supported. Auto-detecting arbitrary exchange CSV layouts is deliberately out of scope.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

from quantplatform.core.errors import DataProviderError
from quantplatform.data.records import CANONICAL_COLUMNS, RawBarRecord

__all__ = ["CsvSource", "compute_checksum", "load_csv_records"]

_EXTRA_CELLS_KEY = "__extra_cells__"
"""Key under which :class:`csv.DictReader` collects cells beyond the declared header."""

_READ_CHUNK_BYTES = 1 << 20


def compute_checksum(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's exact bytes.

    Hashing raw bytes (rather than parsed content) means the checksum changes if anything
    at all about the source changes, which is the property provenance needs.

    Args:
        path: File to hash.

    Returns:
        A 64-character lowercase hex digest.

    Raises:
        DataProviderError: If the file cannot be read.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise DataProviderError(
            "could not read the source file to compute its checksum",
            path=str(path),
            reason=str(exc),
        ) from exc
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class CsvSource:
    """A parsed CSV source: its provenance and the raw rows it yielded."""

    path: Path
    checksum: str
    records: tuple[RawBarRecord, ...]
    header: tuple[str, ...]


def load_csv_records(
    path: Path,
    *,
    source_id: str,
    delimiter: str = ",",
    encoding: str = "utf-8",
) -> CsvSource:
    """Read a canonical CSV file into raw records.

    Structural problems fail loudly here, because they make the whole file unusable rather
    than affecting one row: a missing file, an undecodable file, an absent header, or a
    header lacking a required column. Row-level problems are left entirely to validation.

    Args:
        path: File to read.
        source_id: Logical source identifier stamped onto every record.
        delimiter: Field delimiter; a single character.
        encoding: Text encoding used to decode the file.

    Returns:
        The file's checksum, header and raw records.

    Raises:
        DataProviderError: If the file is missing, undecodable, headerless, or is missing a
            required canonical column.
    """
    checksum = compute_checksum(path)

    try:
        text = path.read_text(encoding=encoding)
    except OSError as exc:
        raise DataProviderError(
            "could not read the source file",
            path=str(path),
            reason=str(exc),
        ) from exc
    except UnicodeDecodeError as exc:
        raise DataProviderError(
            "could not decode the source file with the configured encoding",
            path=str(path),
            encoding=encoding,
            reason=str(exc),
        ) from exc

    reader = csv.DictReader(
        text.splitlines(),
        delimiter=delimiter,
        restkey=_EXTRA_CELLS_KEY,
        restval="",
    )
    if reader.fieldnames is None:
        raise DataProviderError(
            "the source file is empty and has no header row",
            path=str(path),
        )

    header = tuple(name.strip() for name in reader.fieldnames)
    missing = tuple(column for column in CANONICAL_COLUMNS if column not in header)
    if missing:
        raise DataProviderError(
            "the source file is missing required columns",
            path=str(path),
            missing=list(missing),
            expected=list(CANONICAL_COLUMNS),
            found=list(header),
        )

    extra_columns = tuple(name for name in header if name not in CANONICAL_COLUMNS)
    records = tuple(
        _build_record(
            row=row,
            source_id=source_id,
            source_row=index,
            extra_columns=extra_columns,
        )
        for index, row in enumerate(reader, start=1)
    )
    return CsvSource(path=path, checksum=checksum, records=records, header=header)


def _build_record(
    *,
    row: dict[str, str | list[str]],
    source_id: str,
    source_row: int,
    extra_columns: tuple[str, ...],
) -> RawBarRecord:
    """Turn one CSV row into a raw record, keeping every value as its original string."""
    extra_fields = {name: _cell(row, name) for name in extra_columns}
    surplus = row.get(_EXTRA_CELLS_KEY)
    if isinstance(surplus, list) and surplus:
        extra_fields[_EXTRA_CELLS_KEY] = ",".join(surplus)

    return RawBarRecord(
        source=source_id,
        source_row=source_row,
        symbol=_cell(row, "symbol"),
        market_type=_cell(row, "market_type"),
        timeframe=_cell(row, "timeframe"),
        open_time=_cell(row, "open_time"),
        close_time=_cell(row, "close_time"),
        open=_cell(row, "open"),
        high=_cell(row, "high"),
        low=_cell(row, "low"),
        close=_cell(row, "close"),
        volume=_cell(row, "volume"),
        trade_count=_cell(row, "trade_count"),
        extra_fields=extra_fields,
    )


def _cell(row: dict[str, str | list[str]], name: str) -> str:
    """Return one cell as a stripped string, tolerating absent or surplus values."""
    value = row.get(name, "")
    if isinstance(value, list):
        return ",".join(value).strip()
    return (value or "").strip()
