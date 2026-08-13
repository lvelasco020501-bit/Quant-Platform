"""Typed, environment-driven configuration.

Every operational parameter — risk limits, execution assumptions, venue endpoints — is
configuration rather than code, so behaviour can be changed and audited without touching
business logic. Secrets are typed as :class:`~pydantic.SecretStr` so they cannot be
printed, serialised or logged by accident.

The platform defaults to paper execution. Live execution requires three independent
signals: the mode, an explicit enable flag and an exact confirmation phrase.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from quantplatform.core.constants import LIVE_CONFIRMATION_PHRASE
from quantplatform.core.enums import (
    AlertSeverity,
    AssetClass,
    Environment,
    ExecutionMode,
    FindingSeverity,
    LogFormat,
    MarketType,
    Timeframe,
)
from quantplatform.core.errors import ConfigurationError, LiveTradingNotAuthorizedError
from quantplatform.core.models.base import (
    AssetCode,
    StrategyId,
    Symbol,
    Text,
    UtcDatetime,
    VenueId,
)
from quantplatform.core.numeric import Money, NonNegativeMoney, Rate

__all__ = [
    "BacktestSettings",
    "DataSettings",
    "DatabaseSettings",
    "ExchangeSettings",
    "MarketSettings",
    "MonitoringSettings",
    "PaperSettings",
    "RiskSettings",
    "Settings",
    "load_settings",
]


class _SettingsSection(BaseModel):
    """Base class for configuration sections."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class MarketSettings(_SettingsSection):
    """The instrument and timeframe the platform trades by default."""

    asset_class: AssetClass = AssetClass.CRYPTO
    symbol: Symbol = "BTC/USDT"
    base_asset: AssetCode = "BTC"
    quote_asset: AssetCode = "USDT"
    market_type: MarketType = MarketType.SPOT
    timeframe: Timeframe = Timeframe.H1

    @model_validator(mode="after")
    def _validate_symbol(self) -> Self:
        """Check that the symbol agrees with the configured assets."""
        expected = f"{self.base_asset}/{self.quote_asset}"
        if self.symbol != expected:
            msg = f"symbol {self.symbol!r} does not match assets {expected!r}"
            raise ValueError(msg)
        return self


class RiskSettings(_SettingsSection):
    """Externalised risk limits.

    Fractions are expressed on the unit interval, where ``0.05`` means five percent.
    """

    allowed_symbols: tuple[Symbol, ...] = ("BTC/USDT",)
    allowed_market_types: tuple[MarketType, ...] = (MarketType.SPOT,)
    allow_short: bool = False
    allow_leverage: bool = False
    max_leverage: Money = Decimal(1)

    max_open_positions: int = Field(default=1, ge=1)
    max_exposure_fraction: Rate = Decimal("0.95")
    target_position_fraction: Rate = Decimal("0.95")
    max_open_orders: int = Field(default=1, ge=0)
    max_daily_orders: int = Field(default=20, ge=1)
    max_hourly_orders: int = Field(default=5, ge=1)

    max_daily_drawdown_fraction: Rate = Decimal("0.05")
    max_total_drawdown_fraction: Rate = Decimal("0.20")
    max_spread_basis_points: NonNegativeMoney = Decimal(25)
    max_realized_volatility_fraction: Rate = Decimal("0.15")

    max_consecutive_api_failures: int = Field(default=3, ge=1)
    data_staleness_multiplier: Money = Field(default=Decimal("1.5"), gt=1)
    """Freshness budget as a multiple of the bar interval before data counts as stale."""

    require_reconciliation: bool = True
    min_signal_confidence: Rate = Decimal(0)

    @model_validator(mode="after")
    def _validate_restrictions(self) -> Self:
        """Enforce the platform's initial spot-only, long-only restrictions."""
        if not self.allowed_symbols:
            msg = "at least one symbol must be allowed"
            raise ValueError(msg)
        if not self.allowed_market_types:
            msg = "at least one market type must be allowed"
            raise ValueError(msg)
        if not self.allow_leverage and self.max_leverage != Decimal(1):
            msg = "max_leverage must be 1 while leverage is disabled"
            raise ValueError(msg)
        if self.allow_leverage and self.max_leverage < Decimal(1):
            msg = "max_leverage must be at least 1"
            raise ValueError(msg)
        shortable = [
            market_type for market_type in self.allowed_market_types if market_type.allows_short
        ]
        if self.allow_short and not shortable:
            msg = "short selling requires a shortable market type"
            raise ValueError(msg)
        leveraged = [
            market_type for market_type in self.allowed_market_types if market_type.allows_leverage
        ]
        if self.allow_leverage and not leveraged:
            msg = "leverage requires a market type that supports it"
            raise ValueError(msg)
        if self.max_total_drawdown_fraction < self.max_daily_drawdown_fraction:
            msg = "max_total_drawdown_fraction must not be below max_daily_drawdown_fraction"
            raise ValueError(msg)
        if self.max_daily_orders < self.max_hourly_orders:
            msg = "max_daily_orders must not be below max_hourly_orders"
            raise ValueError(msg)
        if self.target_position_fraction > self.max_exposure_fraction:
            msg = "target_position_fraction must not exceed max_exposure_fraction"
            raise ValueError(msg)
        return self


class ExchangeSettings(_SettingsSection):
    """Venue endpoint and credential configuration.

    Credentials are optional because backtest, paper and shadow modes never authenticate.
    """

    name: str = Field(default="binance", min_length=1, max_length=32)
    testnet: bool = True
    base_url: str | None = None
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)

    @property
    def has_credentials(self) -> bool:
        """Return whether both credential halves are present."""
        return self.api_key is not None and self.api_secret is not None


class DatabaseSettings(_SettingsSection):
    """PostgreSQL connection configuration."""

    dsn: SecretStr = SecretStr("postgresql+psycopg://quant:quant@localhost:5432/quantplatform")
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    statement_timeout_seconds: int = Field(default=30, ge=1)
    echo: bool = False


class DataSettings(_SettingsSection):
    """Configuration for the historical market-data ingestion pipeline.

    Expected symbol, market type and timeframe are deliberately not duplicated here:
    :attr:`MarketSettings.symbol`, :attr:`MarketSettings.market_type` and
    :attr:`MarketSettings.timeframe` are the single source of truth, and the ingestion
    service and CLI default to them unless a specific invocation overrides them (for
    example, to prove that a fixture with the wrong symbol is correctly rejected).
    """

    csv_delimiter: str = Field(default=",", min_length=1, max_length=1)
    csv_encoding: str = Field(default="utf-8", min_length=1)

    close_grace_period_seconds: int = Field(default=0, ge=0)
    """Extra time, beyond the bar's own close timestamp, before it is treated as closed."""

    gap_severity: FindingSeverity = FindingSeverity.WARNING
    """Severity recorded for each missing interval, before any escalation threshold."""

    max_allowed_gap_bars: int = Field(default=24, ge=0)
    """Total missing bars beyond which the dataset is treated as unusable and rejected.

    A single missing bar or a handful of them only produces a ``WARNING`` (or whatever
    :attr:`gap_severity` is configured to). Once the total count of missing intervals in a
    dataset exceeds this threshold, an additional ``FATAL`` finding is raised and the whole
    ingestion run fails, because a dataset with that many holes cannot be trusted for
    downstream feature computation or backtesting.
    """

    max_data_age_multiplier: Money = Field(default=Decimal("2"), gt=1)
    """Freshness budget, as a multiple of the timeframe, before the latest bar is stale.

    Ignored whenever the ingestion is given an explicit historical reference time (either
    through :attr:`historical_backfill_end` or a per-call override): an import that
    intentionally ends in the past must never be flagged stale merely for being old.
    """

    historical_backfill_end: UtcDatetime | None = None
    """Default "as of" boundary for historical imports, overridable per ingestion call."""

    batch_size: int = Field(default=1000, ge=1)
    """Number of bars written to storage per batch during ingestion."""

    default_source_id: VenueId = "csv_historical"
    """Default logical source identifier when an ingestion call does not supply one."""

    @model_validator(mode="after")
    def _validate_gap_severity(self) -> Self:
        """Restrict the configurable gap severity to the two values the spec allows."""
        allowed = (FindingSeverity.WARNING, FindingSeverity.ERROR)
        if self.gap_severity not in allowed:
            msg = "gap_severity must be either warning or error"
            raise ValueError(msg)
        return self


class BacktestSettings(_SettingsSection):
    """Simulation assumptions recorded with every backtest run."""

    initial_capital: NonNegativeMoney = Decimal(10_000)
    commission_basis_points: NonNegativeMoney = Decimal(10)
    spread_basis_points: NonNegativeMoney = Decimal(2)
    slippage_basis_points: NonNegativeMoney = Decimal(5)
    latency_bars: int = Field(default=0, ge=0)
    random_seed: int = Field(default=42, ge=0)

    @model_validator(mode="after")
    def _validate_capital(self) -> Self:
        """Require capital to actually fund a backtest."""
        if self.initial_capital <= 0:
            msg = "initial_capital must be strictly positive"
            raise ValueError(msg)
        return self


class PaperSettings(_SettingsSection):
    """Everything a long-running paper session needs that is not code.

    Deployment configuration only: where artefacts go, which instruments to stream, and how
    the feed should behave when the network misbehaves. Nothing here influences a trading
    decision — sizing, limits and execution assumptions live in
    :class:`RiskSettings` and :class:`BacktestSettings` and are untouched by this section.
    """

    session_id: Text = "paper-session"
    """Identity persisted state is stored under. Two sessions sharing one would overwrite
    each other's accounts, so it doubles as the state file's name."""

    strategy_id: StrategyId | None = None
    """Registry identifier of the strategy to run.

    No default, because there is no sensible one: the platform ships no strategy, and
    guessing a name would turn a configuration mistake into a startup that runs something
    nobody chose. Startup refuses until this is set and resolvable.
    """

    symbols: tuple[Symbol, ...] = ("BTC/USDT",)
    timeframe: Timeframe = Timeframe.H1

    reports_directory: Path = Path("var/reports")
    state_directory: Path = Path("var/state")
    log_directory: Path = Path("var/logs")

    websocket_url: str = Field(default="wss://stream.binance.com:9443/ws", min_length=1)
    receive_timeout_seconds: float = Field(default=5.0, gt=0)
    heartbeat_timeout_seconds: float = Field(default=60.0, gt=0)
    close_grace_seconds: float = Field(default=2.0, ge=0)
    """Maximum tolerated lag of the local clock behind the venue's confirmed candle close.

    One meaning, both consumers: the market-data feed and the paper session each treat a
    candle as final once ``now >= close_time - close_grace_seconds``. It can never make a
    forming candle actionable, because the venue's own closed flag is checked independently.
    """
    reconnect_initial_delay_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_delay_seconds: float = Field(default=60.0, gt=0)
    reconnect_backoff_multiplier: float = Field(default=2.0, ge=1.0)
    max_reconnect_attempts: int = Field(default=5, ge=1)

    symbol_rules_refresh_seconds: float = Field(default=21_600.0, gt=0)
    """How old the venue's trading rules may get before they are re-fetched.

    Six hours against a twenty-four hour staleness budget, which is four refreshes of margin
    rather than one. The margin is the point: a single failed refresh is then an event with
    eighteen hours of slack behind it, not the beginning of an outage. Set too close to the
    budget and one unreachable endpoint stops the run.

    Must stay strictly below
    :attr:`~quantplatform.risk.config.RiskConfiguration.stale_symbol_rules_seconds`, which
    startup enforces — the two are separately configurable and a pair that cannot work is
    refused at startup rather than discovered a day into a run.
    """

    stall_alert_margin_seconds: float = Field(default=120.0, gt=0)
    """Grace period added to one candle interval before a silent pipeline is an alert.

    A session that has processed nothing for exactly one timeframe has not necessarily
    stalled — the next bar may simply not have closed yet, or one delivery cycle may be
    running a little behind. Past ``timeframe.seconds + stall_alert_margin_seconds`` with
    still nothing processed is past any ordinary explanation, and
    :class:`~quantplatform.paper.watchdog.StallWatchdog` raises a critical alert.
    """

    report_timezone: str = "UTC"
    render_charts: bool = True
    chart_dpi: int = Field(default=150, ge=50, le=600)

    max_bars: int | None = Field(default=None, ge=1)
    """Stop after this many received bars, or ``None`` to run until stopped. Bounds a smoke
    run without needing a second code path."""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Check the instrument list and the feed timings.

        Endpoint validation is deliberately not repeated here: it belongs to
        :class:`~quantplatform.marketdata.config.MarketDataConfiguration`, which refuses
        anything that is not a public credential-free stream, and duplicating the rule
        would let the two drift apart.

        Raises:
            ValueError: If the symbol list is empty or repeats, or the heartbeat budget
                does not exceed the read timeout.
        """
        if not self.symbols:
            msg = "at least one symbol must be configured"
            raise ValueError(msg)
        if len(set(self.symbols)) != len(self.symbols):
            msg = "symbols must not repeat"
            raise ValueError(msg)
        if self.heartbeat_timeout_seconds <= self.receive_timeout_seconds:
            msg = "heartbeat_timeout_seconds must exceed receive_timeout_seconds"
            raise ValueError(msg)
        return self


class MonitoringSettings(_SettingsSection):
    """Health monitoring and alerting configuration."""

    heartbeat_seconds: int = Field(default=60, ge=1)
    reconciliation_interval_seconds: int = Field(default=300, ge=1)
    max_clock_skew_seconds: NonNegativeMoney = Decimal(2)
    max_process_restarts: int = Field(default=3, ge=1)
    alert_min_severity: AlertSeverity = AlertSeverity.WARNING


class Settings(BaseSettings):
    """Root configuration object.

    Values are read from the environment using the ``QP_`` prefix and a double-underscore
    delimiter for nested sections, for example ``QP_RISK__MAX_DAILY_ORDERS=10``.
    """

    model_config = SettingsConfigDict(
        env_prefix="QP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    live_trading_enabled: bool = False
    live_confirmation: SecretStr | None = None
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    market: MarketSettings = Field(default_factory=MarketSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    paper: PaperSettings = Field(default_factory=PaperSettings)

    @model_validator(mode="after")
    def _validate_coherence(self) -> Self:
        """Check cross-section coherence and the live-trading authorisation gate.

        Raises:
            ConfigurationError: If the traded market is not permitted by the risk limits.
            LiveTradingNotAuthorizedError: If live execution is selected without complete
                and explicit authorisation.
        """
        if self.market.symbol not in self.risk.allowed_symbols:
            raise ConfigurationError(
                "the configured market symbol is not in the allowed symbol list",
                symbol=self.market.symbol,
            )
        if self.market.market_type not in self.risk.allowed_market_types:
            raise ConfigurationError(
                "the configured market type is not in the allowed market type list",
                market_type=self.market.market_type.value,
            )
        if self.execution_mode is ExecutionMode.LIVE:
            self._assert_live_authorized()
        elif self.live_trading_enabled:
            raise ConfigurationError(
                "live_trading_enabled must be false unless execution_mode is live",
                execution_mode=self.execution_mode.value,
            )
        return self

    def _assert_live_authorized(self) -> None:
        """Verify every precondition for arming live execution.

        Raises:
            LiveTradingNotAuthorizedError: If any precondition is unmet.
        """
        if not self.live_trading_enabled:
            raise LiveTradingNotAuthorizedError(
                "live execution requires live_trading_enabled to be true"
            )
        if (
            self.live_confirmation is None
            or self.live_confirmation.get_secret_value() != LIVE_CONFIRMATION_PHRASE
        ):
            raise LiveTradingNotAuthorizedError(
                "live execution requires the exact confirmation phrase",
                expected_env_var="QP_LIVE_CONFIRMATION",
            )
        if not self.exchange.has_credentials:
            raise LiveTradingNotAuthorizedError(
                "live execution requires exchange credentials",
                exchange=self.exchange.name,
            )
        if self.environment is Environment.DEVELOPMENT:
            raise LiveTradingNotAuthorizedError(
                "live execution is not permitted from a development environment",
                environment=self.environment.value,
            )
        if self.risk.allow_short or self.risk.allow_leverage:
            raise LiveTradingNotAuthorizedError(
                "live execution is restricted to unleveraged long-only trading",
                allow_short=self.risk.allow_short,
                allow_leverage=self.risk.allow_leverage,
            )

    @property
    def live_trading_armed(self) -> bool:
        """Return whether the process is authorised to submit real orders."""
        return self.execution_mode is ExecutionMode.LIVE and self.live_trading_enabled

    def secret_values(self) -> tuple[str, ...]:
        """Return the literal secret values to mask in logs.

        Returns:
            Every configured credential value, for use with
            :func:`~quantplatform.core.logging_config.configure_logging`.
        """
        candidates = (
            self.exchange.api_key,
            self.exchange.api_secret,
            self.database.dsn,
            self.live_confirmation,
        )
        return tuple(secret.get_secret_value() for secret in candidates if secret is not None)


def load_settings(**overrides: Any) -> Settings:  # noqa: ANN401 - pydantic field values are heterogeneous by design
    """Build a settings instance from the environment.

    No module-level cache is used: the composition root loads settings once and injects
    them, which keeps configuration explicit and tests independent of process state.

    Args:
        **overrides: Field values that take precedence over the environment, intended for
            tests and for command-line overrides.

    Returns:
        A validated, frozen settings instance.

    Raises:
        ConfigurationError: If the configuration is incoherent or unsafe.
    """
    return Settings(**overrides)
