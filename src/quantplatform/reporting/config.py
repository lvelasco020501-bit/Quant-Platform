"""Configuration for daily reporting.

Where reports go, what shapes they take, what counts as a day, and the thresholds that turn
an observation into an alert. Every one of these is a policy decision rather than a fact
about the market, which is why they live in configuration and not in the code that computes
the numbers.

**Nothing here can change what the session trades.** The thresholds decide when a report
says something looks wrong; they never decide what the strategy, the risk engine or the
broker do about it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantplatform.core.numeric import Money, NonNegativeMoney, Rate
from quantplatform.core.timeutils import ensure_utc

__all__ = ["AlertThresholds", "ReportFormat", "ReportingConfiguration"]


class ReportFormat(StrEnum):
    """A serialisation a daily report can be written in."""

    JSON = "json"
    """Complete and round-trippable: the only format a later comparison reads back."""

    CSV = "csv"
    """One header row and one data row, so a month of days concatenates into a table."""

    MARKDOWN = "markdown"
    """The human-readable form, for a person reading one day."""


class AlertThresholds(BaseModel):
    """When an observation becomes something worth saying out loud.

    Defaults are deliberately tight on the operational checks and loose on the trading
    ones. A gap in the candle series is always worth knowing about; a losing day is
    ordinary, and an alert that fires on every red day is an alert nobody reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    max_daily_drawdown: Rate = Decimal("0.05")
    max_daily_loss: NonNegativeMoney = Decimal(500)
    """Absolute quote-asset loss, as a positive magnitude, beyond which the day is flagged."""

    max_gap_count: int = Field(default=0, ge=0)
    max_missing_bars: int = Field(default=0, ge=0)
    max_reconnects: int = Field(default=3, ge=0)
    max_heartbeat_failures: int = Field(default=2, ge=0)
    max_runtime_exceptions: int = Field(default=0, ge=0)
    max_session_interruptions: int = Field(default=1, ge=0)
    max_clock_drift_seconds: NonNegativeMoney = Decimal(2)

    max_risk_rejection_ratio: Rate = Decimal("0.5")
    max_broker_rejection_ratio: Rate = Decimal("0.1")
    min_acceptance_rate: Rate = Decimal("0.95")
    """Floor on the share of candles the *feed* delivered of what it parsed."""

    minimum_session_acceptance_rate: Rate = Decimal("0.95")
    """Floor on the share of delivered bars the *session* actually processed.

    A separate limit because it answers a separate question. A feed can deliver every
    candle flawlessly while the session downstream refuses all of them — which is exactly
    what a contradictory grace period once did, for a week, under a green report.
    """

    max_slippage_ratio: Rate = Decimal("0.02")
    """Slippage as a share of the day's traded notional, beyond which execution looks wrong."""

    max_commission_ratio: Rate = Decimal("0.02")
    """Commission as a share of the day's traded notional."""

    red_escalation_factor: Money = Field(default=Decimal(2), gt=1)
    """How far past a threshold an observation has to sit before yellow becomes red."""

    red_escalation_count: int = Field(default=3, ge=1)
    """For counters whose threshold is zero, the count at which yellow becomes red.

    A ratio can be exceeded by a factor; a count whose limit is zero cannot, because every
    multiple of zero is zero. Those need an absolute step instead.
    """

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the loss threshold is expressed as a magnitude.

        Raises:
            ValueError: If the daily loss limit is signed rather than a magnitude.
        """
        if self.max_daily_loss < 0:
            msg = "max_daily_loss is a positive magnitude, not a signed amount"
            raise ValueError(msg)
        return self


class ReportingConfiguration(BaseModel):
    """Where daily reports are written and how they are shaped."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    output_directory: Path = Path("reports")
    """Root of the ``YYYY/MM/DD`` tree. Relative paths resolve against the process's
    working directory, which a composition root is expected to set deliberately."""

    formats: tuple[ReportFormat, ...] = (
        ReportFormat.JSON,
        ReportFormat.CSV,
        ReportFormat.MARKDOWN,
    )

    render_charts: bool = True
    chart_dpi: int = Field(default=150, ge=50, le=600)
    chart_width_inches: float = Field(default=10.0, gt=0, le=60)
    chart_height_inches: float = Field(default=4.5, gt=0, le=60)

    timezone: str = "UTC"
    """IANA zone deciding where one reporting day ends and the next begins.

    Every timestamp inside a report stays UTC. This only labels the day, because "Tuesday"
    is a question about the operator's calendar rather than about the market's clock.
    """

    retention_days: int | None = Field(default=None, ge=1)
    """Days of history to keep, or ``None`` to keep everything.

    ``None`` by default, and pruning never happens on its own: deleting an audit trail is
    not something a write should do as a side effect. A caller has to ask for it.
    """

    thresholds: AlertThresholds = AlertThresholds()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the output location, the format list and the time zone.

        Raises:
            ValueError: If the formats are empty or repeat, the zone is unknown, or the
                output directory is not a usable relative or absolute path.
        """
        if not self.formats:
            msg = "at least one report format must be written"
            raise ValueError(msg)
        if len(set(self.formats)) != len(self.formats):
            msg = "report formats must not repeat"
            raise ValueError(msg)
        if not self.output_directory.parts:
            msg = "output_directory must not be empty"
            raise ValueError(msg)
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"unknown time zone {self.timezone!r}"
            raise ValueError(msg) from exc
        return self

    @property
    def zone(self) -> ZoneInfo:
        """Return the zone that decides where a reporting day begins."""
        return ZoneInfo(self.timezone)

    def day_of(self, moment: datetime) -> date:
        """Return the reporting day a UTC instant falls in.

        Args:
            moment: Timezone-aware instant.

        Returns:
            The calendar date in the configured reporting zone.
        """
        return ensure_utc(moment).astimezone(self.zone).date()

    def directory_for(self, day: date) -> Path:
        """Return the ``<root>/YYYY/MM/DD`` directory a day's artefacts belong in."""
        return self.output_directory / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"

    def writes(self, report_format: ReportFormat) -> bool:
        """Return whether this configuration asks for a given format."""
        return report_format in self.formats
