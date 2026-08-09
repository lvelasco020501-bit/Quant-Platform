"""Putting a day on disk, and reading it back.

One directory per day under ``<root>/YYYY/MM/DD``, holding whichever of ``daily.json``,
``daily.csv`` and ``daily.md`` the configuration asks for, plus the PNG charts.

**JSON is the canonical form.** It is the only format read back, because it is the only one
that round-trips: CSV flattens and Markdown prose-ifies, and a comparison built from either
would be comparing against a lossy copy of yesterday.

**Nothing is deleted as a side effect.** Retention is opt-in and only ever happens when a
caller asks for it by name. A daily report is an audit trail, and a write that quietly
removed last month's is not a feature.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from quantplatform.reporting.charts import ChartRenderer
from quantplatform.reporting.config import ReportFormat, ReportingConfiguration
from quantplatform.reporting.models import DailyReport
from quantplatform.reporting.summary import render_csv, render_markdown

__all__ = ["DailyReportWriter", "WrittenReport"]

JSON_FILENAME = "daily.json"
CSV_FILENAME = "daily.csv"
MARKDOWN_FILENAME = "daily.md"

_DAY_PATH_DEPTH = 3
"""``YYYY/MM/DD`` — the exact shape a directory must have before pruning will touch it."""

_DAY_PATH_WIDTHS = (4, 2, 2)
"""Zero-padded widths of those three components."""


@dataclass(frozen=True, slots=True)
class WrittenReport:
    """Where a day's artefacts ended up."""

    day: date
    directory: Path
    json_path: Path | None = None
    csv_path: Path | None = None
    markdown_path: Path | None = None
    chart_paths: tuple[Path, ...] = ()

    @property
    def paths(self) -> tuple[Path, ...]:
        """Return every file written, in a stable order."""
        named = (self.json_path, self.csv_path, self.markdown_path)
        return tuple(path for path in named if path is not None) + self.chart_paths


@dataclass
class DailyReportWriter:
    """Writes daily reports to disk and reads them back for comparison."""

    config: ReportingConfiguration
    renderer: ChartRenderer | None = None
    written: list[WrittenReport] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Build a chart renderer when one was not supplied and charts are wanted."""
        if self.renderer is None and self.config.render_charts:
            self.renderer = ChartRenderer(config=self.config)

    def directory_for(self, day: date) -> Path:
        """Return the directory a day's artefacts belong in."""
        return self.config.directory_for(day)

    def write(self, report: DailyReport) -> WrittenReport:
        """Persist a day in every configured format.

        Args:
            report: The finished report.

        Returns:
            A record of exactly which files were written.
        """
        directory = self.directory_for(report.day)
        directory.mkdir(parents=True, exist_ok=True)

        json_path: Path | None = None
        csv_path: Path | None = None
        markdown_path: Path | None = None

        if self.config.writes(ReportFormat.JSON):
            json_path = directory / JSON_FILENAME
            json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        if self.config.writes(ReportFormat.CSV):
            csv_path = directory / CSV_FILENAME
            csv_path.write_text(render_csv(report), encoding="utf-8")
        if self.config.writes(ReportFormat.MARKDOWN):
            markdown_path = directory / MARKDOWN_FILENAME
            markdown_path.write_text(render_markdown(report), encoding="utf-8")

        charts: tuple[Path, ...] = ()
        if self.config.render_charts and self.renderer is not None:
            charts = self.renderer.render_all(report, directory)

        record = WrittenReport(
            day=report.day,
            directory=directory,
            json_path=json_path,
            csv_path=csv_path,
            markdown_path=markdown_path,
            chart_paths=charts,
        )
        self.written.append(record)
        return record

    def read(self, day: date) -> DailyReport | None:
        """Load a previously written day.

        Args:
            day: The day to load.

        Returns:
            The stored report, or ``None`` when that day was never written or was written
            without JSON — the only round-trippable format.
        """
        path = self.directory_for(day) / JSON_FILENAME
        if not path.is_file():
            return None
        return DailyReport.model_validate_json(path.read_text(encoding="utf-8"))

    def read_previous(self, day: date, *, lookback_days: int = 7) -> DailyReport | None:
        """Load the most recent report before a day.

        Searches backwards rather than assuming yesterday exists, because a session that
        was down over a weekend still deserves a comparison against the last day it ran.

        Args:
            day: The day to look back from, exclusive.
            lookback_days: How far back to search.

        Returns:
            The nearest earlier report, or ``None`` when none is within reach.
        """
        for offset in range(1, lookback_days + 1):
            found = self.read(day - timedelta(days=offset))
            if found is not None:
                return found
        return None

    def prune(self, *, today: date) -> tuple[Path, ...]:
        """Delete day directories older than the retention window.

        Never called automatically. Retention removes an audit trail, so it happens only
        when a caller asks for it and only when a window was configured.

        Only directories matching the exact ``<root>/YYYY/MM/DD`` shape are considered, and
        each is re-derived from its own parsed date rather than from a glob, so nothing
        outside the report tree can be selected by a stray file or an unexpected name.

        Args:
            today: The date retention is measured back from.

        Returns:
            The directories removed, oldest first.

        Raises:
            ValueError: If ``today`` precedes the retention window itself, which would make
                the cutoff meaningless.
        """
        window = self.config.retention_days
        root = self.config.output_directory
        if window is None or not root.is_dir():
            return ()
        cutoff = today - timedelta(days=window)
        if cutoff.year < 1:
            msg = "retention window extends before the start of the calendar"
            raise ValueError(msg)

        removed: list[Path] = []
        for candidate in sorted(root.glob("*/*/*")):
            if not candidate.is_dir():
                continue
            day = _parse_day_directory(root, candidate)
            if day is None or day >= cutoff:
                continue
            shutil.rmtree(candidate)
            removed.append(candidate)
        return tuple(removed)


def _parse_day_directory(root: Path, candidate: Path) -> date | None:
    """Return the day a directory represents, or ``None`` when it is not one.

    Deliberately strict: the path must sit exactly three levels under the root and every
    level must be the zero-padded number it claims to be, and the rebuilt path must equal
    the candidate. Anything else is not a report directory and is left alone.
    """
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        return None
    if len(parts) != _DAY_PATH_DEPTH:
        return None
    year, month, day = parts
    widths = (len(year), len(month), len(day))
    digits = year.isdigit() and month.isdigit() and day.isdigit()
    if widths != _DAY_PATH_WIDTHS or not digits:
        return None
    try:
        parsed = date(int(year), int(month), int(day))
    except ValueError:
        return None
    if root / f"{parsed.year:04d}" / f"{parsed.month:02d}" / f"{parsed.day:02d}" != candidate:
        return None
    return parsed
