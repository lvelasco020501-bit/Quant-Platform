"""Static PNG charts of a day.

Six pictures of the same day, drawn from the series the report already carries rather than
recomputed here. A chart drawn today and one drawn next month from the stored JSON are the
same chart, which is the only way a picture can be evidence.

**No pyplot.** Figures are built through the object-oriented
:class:`~matplotlib.figure.Figure` and the Agg canvas directly, so no global backend state is
set, no GUI is initialised and nothing here depends on process-wide configuration a caller
might have changed. **No interactive output** — a PNG on disk and nothing else.

**A day with nothing to draw is not an error.** An empty series produces a placeholder image
saying so, because a missing file reads as a broken pipeline while an empty chart reads as a
quiet day.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import DateFormatter, date2num
from matplotlib.figure import Figure

from quantplatform.reporting.config import ReportingConfiguration
from quantplatform.reporting.models import DailyReport, SeriesPoint

__all__ = ["CHART_FILENAMES", "ChartRenderer"]

CHART_FILENAMES: tuple[str, ...] = (
    "equity.png",
    "drawdown.png",
    "returns.png",
    "trades.png",
    "distribution.png",
    "exposure.png",
)
"""Every chart a complete day produces, in the order they are drawn."""

_HISTOGRAM_BINS = 12
_EMPTY_MESSAGE = "no data for this day"
_MIN_POINTS_FOR_SPACING = 2
"""Bar width is inferred from the gap between observations, which needs two of them."""


class ChartRenderer:
    """Draws a day's charts as PNG files."""

    def __init__(self, *, config: ReportingConfiguration) -> None:
        """Create a renderer.

        Args:
            config: Supplies figure size and resolution.
        """
        self._config = config

    def render_all(self, report: DailyReport, directory: Path) -> tuple[Path, ...]:
        """Draw every chart for a day.

        Args:
            report: The finished report.
            directory: Where the PNG files go; created if absent.

        Returns:
            The paths written, in :data:`CHART_FILENAMES` order.
        """
        directory.mkdir(parents=True, exist_ok=True)
        series = report.series
        return (
            self._line(
                directory / "equity.png",
                series.equity,
                title=f"Equity — {report.day.isoformat()}",
                ylabel=f"Equity ({report.quote_asset})",
            ),
            self._line(
                directory / "drawdown.png",
                series.drawdown,
                title=f"Drawdown — {report.day.isoformat()}",
                ylabel="Drawdown (fraction)",
                fill=True,
                invert=True,
            ),
            self._bars_over_time(
                directory / "returns.png",
                series.returns,
                title=f"Per-bar returns — {report.day.isoformat()}",
                ylabel="Return (fraction)",
            ),
            self._trade_bars(
                directory / "trades.png",
                series.trade_pnl,
                title=f"PnL by trade — {report.day.isoformat()}",
                ylabel=f"Net PnL ({report.quote_asset})",
            ),
            self._histogram(
                directory / "distribution.png",
                series.trade_pnl,
                title=f"Trade distribution — {report.day.isoformat()}",
                xlabel=f"Net PnL ({report.quote_asset})",
            ),
            self._line(
                directory / "exposure.png",
                series.exposure,
                title=f"Exposure — {report.day.isoformat()}",
                ylabel="Position value / equity",
                fill=True,
            ),
        )

    # --- Individual charts --------------------------------------------------------------------

    def _line(
        self,
        path: Path,
        points: Sequence[SeriesPoint],
        *,
        title: str,
        ylabel: str,
        fill: bool = False,
        invert: bool = False,
    ) -> Path:
        """Draw a time series as a line, optionally filled."""
        figure, axes = self._figure(title, ylabel)
        if not points:
            self._mark_empty(axes)
            return self._save(figure, path)
        times = _to_day_numbers(points)
        values = [float(point.value) for point in points]
        axes.plot(times, values, linewidth=1.4)
        if fill:
            axes.fill_between(times, values, alpha=0.25)
        if invert:
            axes.invert_yaxis()
        self._format_time_axis(axes, times)
        return self._save(figure, path)

    def _bars_over_time(
        self, path: Path, points: Sequence[SeriesPoint], *, title: str, ylabel: str
    ) -> Path:
        """Draw a time series as signed bars around zero."""
        figure, axes = self._figure(title, ylabel)
        if not points:
            self._mark_empty(axes)
            return self._save(figure, path)
        times = _to_day_numbers(points)
        values = [float(point.value) for point in points]
        axes.bar(times, values, width=_bar_width(times), align="center")
        axes.axhline(0.0, linewidth=0.8)
        self._format_time_axis(axes, times)
        return self._save(figure, path)

    def _trade_bars(
        self, path: Path, values: Sequence[Decimal], *, title: str, ylabel: str
    ) -> Path:
        """Draw each closed trade's net result as a bar, in the order they closed."""
        figure, axes = self._figure(title, ylabel)
        if not values:
            self._mark_empty(axes)
            return self._save(figure, path)
        results = [float(value) for value in values]
        axes.bar(range(1, len(results) + 1), results)
        axes.axhline(0.0, linewidth=0.8)
        axes.set_xlabel("Trade")
        return self._save(figure, path)

    def _histogram(self, path: Path, values: Sequence[Decimal], *, title: str, xlabel: str) -> Path:
        """Draw the distribution of closed-trade results."""
        figure, axes = self._figure(title, "Trades")
        if not values:
            self._mark_empty(axes)
            return self._save(figure, path)
        results = [float(value) for value in values]
        axes.hist(results, bins=min(_HISTOGRAM_BINS, max(1, len(results))))
        axes.set_xlabel(xlabel)
        return self._save(figure, path)

    # --- Shared plumbing ----------------------------------------------------------------------

    def _figure(self, title: str, ylabel: str) -> tuple[Figure, Axes]:
        """Build a figure and its single axes, sized from configuration."""
        figure = Figure(
            figsize=(self._config.chart_width_inches, self._config.chart_height_inches),
            dpi=self._config.chart_dpi,
            layout="constrained",
        )
        FigureCanvasAgg(figure)
        axes = figure.subplots()
        axes.set_title(title)
        axes.set_ylabel(ylabel)
        axes.grid(visible=True, alpha=0.3)
        return figure, axes

    @staticmethod
    def _mark_empty(axes: Axes) -> None:
        """Say plainly that there was nothing to draw."""
        axes.text(0.5, 0.5, _EMPTY_MESSAGE, ha="center", va="center", transform=axes.transAxes)
        axes.set_xticks([])
        axes.set_yticks([])

    def _format_time_axis(self, axes: Axes, times: Sequence[float]) -> None:
        """Label a day-number axis as clock times in the reporting zone."""
        # matplotlib ships no annotations for its date helpers, so these two calls are the
        # only place in the package the type checker has to be told to trust an upstream API.
        formatter = DateFormatter("%H:%M", tz=self._config.zone)  # type: ignore[no-untyped-call]
        axes.xaxis.set_major_formatter(formatter)
        if len(times) > 1:
            axes.set_xlim(times[0], times[-1])
        for label in axes.get_xticklabels():
            label.set_rotation(30)
            label.set_horizontalalignment("right")

    @staticmethod
    def _save(figure: Figure, path: Path) -> Path:
        """Write the figure and release it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path, format="png")
        figure.clear()
        return path


def _to_day_numbers(points: Sequence[SeriesPoint]) -> list[float]:
    """Convert instants to matplotlib's day-number axis.

    Done explicitly rather than by handing datetimes to matplotlib's unit converter: the
    conversion is the same, but this way the axis values are ordinary floats the type
    checker can follow all the way through.
    """
    return [float(date2num(point.at)) for point in points]  # type: ignore[no-untyped-call]


def _bar_width(times: Sequence[float]) -> float:
    """Return a bar width in days that leaves a visible gap between observations."""
    if len(times) < _MIN_POINTS_FOR_SPACING:
        return 0.01
    return max((times[1] - times[0]) * 0.8, 1e-6)
