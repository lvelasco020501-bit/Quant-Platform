"""Daily reporting: what a session did, and whether to believe it.

This package observes. It reads a finished day out of a
:class:`~quantplatform.paper.results.SessionResult` and writes value objects, files and
pictures. It holds no reference to a strategy, a risk engine, a broker or a portfolio, and
there is no path from anything here back into a trading decision — enforced structurally, so
"reporting cannot influence trading" is a property of the import graph rather than a promise
in a docstring.

**Two questions, kept apart.** *How did it trade* — PnL, win rate, drawdown, Sharpe — and
*is the output worth believing* — feed stability, gaps, reconnects, rejection ratios. A
profitable day that dropped a third of its candles is a red day, and a single blended score
would let that pass.

**A metric that could not be computed is ``None``, never zero,** matching the rule the
backtesting metrics already set. The daily figures are computed by the very same function,
so the two definitions cannot drift.

**The reporting layer never gives investment advice.** It says what a process did and what
an operator might check. It does not say what to trade, how much to hold, or whether a
strategy is worth running.
"""

from __future__ import annotations

from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.reporting.charts import CHART_FILENAMES, ChartRenderer
from quantplatform.reporting.config import (
    AlertThresholds,
    ReportFormat,
    ReportingConfiguration,
)
from quantplatform.reporting.daily import (
    DailyReportBuilder,
    DailyReportRecorder,
    evaluate_alerts,
    reconstruct_round_trips,
)
from quantplatform.reporting.health import evaluate_health
from quantplatform.reporting.models import (
    Alert,
    AlertCode,
    DailyAlerts,
    DailyComparison,
    DailyHealth,
    DailyReport,
    DailySeries,
    DailyStatistics,
    DailySummary,
    FeedDiagnostics,
    HealthCheck,
    HealthCheckName,
    HealthLevel,
    RoundTrip,
    SeriesPoint,
)
from quantplatform.reporting.summary import build_summary, render_csv, render_markdown
from quantplatform.reporting.writer import DailyReportWriter, WrittenReport

__all__ = [
    "CHART_FILENAMES",
    "Alert",
    "AlertCode",
    "AlertThresholds",
    "ChartRenderer",
    "DailyAlerts",
    "DailyComparison",
    "DailyHealth",
    "DailyReport",
    "DailyReportBuilder",
    "DailyReportRecorder",
    "DailyReportWriter",
    "DailySeries",
    "DailyStatistics",
    "DailySummary",
    "FeedDiagnostics",
    "FeedMetricsSnapshot",
    "HealthCheck",
    "HealthCheckName",
    "HealthLevel",
    "ReportFormat",
    "ReportingConfiguration",
    "RoundTrip",
    "SeriesPoint",
    "WrittenReport",
    "build_summary",
    "evaluate_alerts",
    "evaluate_health",
    "reconstruct_round_trips",
    "render_csv",
    "render_markdown",
]
