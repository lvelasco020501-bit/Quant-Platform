"""The composition root for a paper trading session.

Wiring and nothing else. Every object below already exists and already knows what it does;
this module's only job is to hand each one its collaborators and get out of the way. There
is no decision here about what to trade, how much, or when — searching this file for such a
decision should come up empty, and if it ever does not, the decision is in the wrong place.

**Two things it will not invent.** A strategy and the venue's trading rules are injected,
never defaulted. Guessing a strategy would run something nobody chose; guessing a tick size
or a minimum notional would size orders against numbers the venue never published. Both are
validated at startup and refused loudly when absent, which is the only honest thing a
composition root can do about information it does not have.

**Fail fast, then run forever.** Everything that can be checked before a socket opens is
checked before a socket opens: directories, endpoint, instruments, timeframe, execution
mode, strategy resolution. After that the process is expected to stay up for days, so the
run loop holds no growing state of its own and stops cooperatively on a signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from quantplatform.backtesting.config import BacktestConfig
from quantplatform.backtesting.engine import BacktestEngine
from quantplatform.config.settings import Settings
from quantplatform.core.clock import Clock, SystemClock
from quantplatform.core.constants import ZERO
from quantplatform.core.enums import CommissionModel, ExecutionMode, SlippageModel
from quantplatform.core.errors import ConfigurationError, StorageError
from quantplatform.core.interfaces import (
    FeaturePipeline,
    PaperStateRepository,
    SymbolRulesProvider,
)
from quantplatform.core.logging_config import get_logger
from quantplatform.core.models.execution_policy import ExecutionPolicy, FeePolicy, SlippagePolicy
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.portfolio import Balance
from quantplatform.core.models.risk import RiskBudget
from quantplatform.core.symbol_rules import SymbolRulesStore
from quantplatform.execution.broker import SimulatedBroker
from quantplatform.execution.config import ExecutionConfig
from quantplatform.marketdata.config import MarketDataConfiguration
from quantplatform.marketdata.feed import BinanceSpotMarketDataFeed
from quantplatform.orchestration.features import features_for
from quantplatform.orchestration.symbol_rules import SymbolRulesRefresher
from quantplatform.paper.results import SessionResult
from quantplatform.paper.runner import PaperTradingRunner
from quantplatform.paper.session import PaperTradingSession
from quantplatform.paper.watchdog import StallWatchdog
from quantplatform.portfolio.engine import SpotPortfolioEngine
from quantplatform.reporting.config import ReportingConfiguration
from quantplatform.reporting.daily import DailyReportBuilder, DailyReportRecorder
from quantplatform.reporting.writer import DailyReportWriter
from quantplatform.risk.config import RiskConfiguration
from quantplatform.risk.engine import StandardRiskEngine
from quantplatform.storage.paper_state import FilePaperStateRepository
from quantplatform.strategies.base import BaseStrategy
from quantplatform.strategies.registry import StrategyRegistry, build_default_registry

__all__ = [
    "PaperDeployment",
    "build_paper_deployment",
    "symbol_rules_freshness_budget",
    "validate_startup",
]

_LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PaperDeployment:
    """Every wired component of a paper session, plus the loop that drives it."""

    settings: Settings
    clock: Clock
    feed: BinanceSpotMarketDataFeed
    session: PaperTradingSession
    runner: PaperTradingRunner
    recorder: DailyReportRecorder
    writer: DailyReportWriter
    repository: PaperStateRepository
    symbol_rules: SymbolRulesStore
    refresher: SymbolRulesRefresher | None = None
    watchdog: StallWatchdog | None = None
    log_paths: tuple[Path, ...] = ()

    def run(self, *, resume: bool = False) -> SessionResult:
        """Drive the session until the feed ends or a stop is requested.

        Args:
            resume: Restore persisted state and continue rather than starting fresh.

        Returns:
            Everything the session produced.
        """
        _LOGGER.info(
            "paper session starting",
            extra={
                "session_id": self.session.session_id,
                "symbols": list(self.feed.symbols),
                "resume": resume,
            },
        )
        if self.watchdog is not None:
            self.watchdog.start()
        try:
            return self.runner.run(resume=resume)
        finally:
            if self.watchdog is not None:
                self.watchdog.stop()
            _LOGGER.info(
                "paper session stopped",
                extra={
                    "session_id": self.session.session_id,
                    "bars_processed": self.session.runtime_metrics().bars_processed,
                    "reports": len(self.recorder.reports),
                    "report_failures": self.recorder.failures,
                    "feed_state": self.feed.state.value,
                },
            )

    def request_stop(self, reason: str = "requested") -> None:
        """Ask the feed and the runner to wind down at their next boundary.

        Both are told, and the order matters. The runner's loop is parked *inside* the
        feed's generator waiting on a socket read, so telling only the runner would leave
        it blocked until the next candle — up to a full bar interval away. Telling the feed
        first brings the read loop back within one receive timeout.
        """
        _LOGGER.info("stop requested", extra={"reason": reason})
        self.feed.request_stop()
        self.runner.request_stop()


def symbol_rules_freshness_budget(settings: Settings) -> int:
    """Return the freshness budget the risk engine will enforce on venue trading rules.

    Exists so the refresh schedule, the startup check and the operator-facing summary all
    read one number. Two of them reading it from different places is how a deployment ends
    up refreshing every six hours against a budget somebody had already changed.

    Args:
        settings: Effective configuration.

    Returns:
        The budget in seconds.
    """
    return _risk_configuration(settings, _execution_policy(settings)).stale_symbol_rules_seconds


def validate_startup(
    settings: Settings,
    *,
    symbol_rules: Mapping[str, SymbolRules],
    registry: StrategyRegistry,
) -> BaseStrategy:
    """Refuse to start on anything that can be known to be wrong beforehand.

    Everything here is cheap and everything here is fatal. A paper run is expected to last
    a week; the cost of discovering a missing directory or an unresolvable strategy on day
    four is the whole week, and none of these checks need a network to perform.

    **Returns the strategy it built, and building it is the point.** Asking whether an
    identifier is present in the registry is not the same question as whether the thing it
    names can be constructed, and answering the cheap one was how ``paper check`` came to
    report a deployment ready that could not build its own strategy: ``breakout`` declares
    no default lookbacks, so it was unconstructable while the membership test passed
    happily. The instance is returned rather than discarded so the caller wires the very
    object validation approved, instead of building a second one that agrees by coincidence.

    Args:
        settings: Effective configuration.
        symbol_rules: Venue trading rules for every configured instrument.
        registry: Strategy registry the configured identifier is resolved against.

    Returns:
        The constructed strategy, ready to be wired.

    Raises:
        ConfigurationError: If the configuration is incoherent, the strategy cannot be
            resolved, or venue rules are missing for a configured instrument.
        StrategyNotFoundError: If the configured identifier is not registered.
        StrategyParameterError: If the configured parameters fail the strategy's own schema.
        StorageError: If a required directory is unusable.
    """
    paper = settings.paper

    if settings.execution_mode is not ExecutionMode.PAPER:
        raise ConfigurationError(
            "paper deployment refuses to start outside paper execution mode",
            execution_mode=settings.execution_mode.value,
        )
    if settings.live_trading_armed:
        raise ConfigurationError("live trading is armed; refusing to start a paper session")

    for label, directory in (
        ("reports_directory", paper.reports_directory),
        ("state_directory", paper.state_directory),
        ("log_directory", paper.log_directory),
    ):
        _require_usable_directory(label, directory)

    # Endpoint, duplicate symbols and timeframe support are all enforced by the market-data
    # configuration's own validator. Constructing it here surfaces those failures at startup
    # rather than at the first connection, and keeps one definition of a valid endpoint.
    _market_data_configuration(settings)

    unknown = sorted(set(paper.symbols) - set(settings.risk.allowed_symbols))
    if unknown:
        raise ConfigurationError(
            "configured symbols are not permitted by the risk limits",
            symbols=unknown,
            allowed=sorted(settings.risk.allowed_symbols),
        )
    missing_rules = sorted(set(paper.symbols) - set(symbol_rules))
    if missing_rules:
        raise ConfigurationError(
            "no venue trading rules were supplied for these symbols; the platform does not "
            "fetch them, and sizing an order without them would use limits the venue never "
            "published",
            symbols=missing_rules,
        )
    if settings.market.market_type not in settings.risk.allowed_market_types:
        raise ConfigurationError(
            "the configured market type is not permitted by the risk limits",
            market_type=settings.market.market_type.value,
        )

    # The refresh cadence and the freshness budget are configured separately and must be
    # compatible; a pair that cannot work would otherwise run for a day and then refuse
    # every order until someone noticed.
    stale_after = symbol_rules_freshness_budget(settings)
    if paper.symbol_rules_refresh_seconds >= stale_after:
        raise ConfigurationError(
            "the symbol rules refresh interval must be strictly below the risk engine's "
            "staleness budget, or the rules expire before the next refresh is due and "
            "every order intent is refused from that point on",
            refresh_interval_seconds=paper.symbol_rules_refresh_seconds,
            stale_after_seconds=stale_after,
        )

    if paper.strategy_id is None:
        raise ConfigurationError(
            "no strategy is configured; set QP_PAPER__STRATEGY_ID to a registered strategy"
        )
    if paper.strategy_id not in registry:
        raise ConfigurationError(
            "the configured strategy is not registered",
            strategy_id=paper.strategy_id,
            registered=sorted(item.strategy_id for item in registry.list_metadata()),
        )
    return resolve_strategy(settings, registry=registry)


def resolve_strategy(settings: Settings, *, registry: StrategyRegistry) -> BaseStrategy:
    """Build the configured strategy from the configured parameters.

    The single place a paper session's strategy comes into existence. ``check`` and ``run``
    both reach it through :func:`validate_startup`, which is what makes a passing check a
    statement about the run rather than about a different code path that resembles it.

    Nothing here knows what any particular strategy's parameters mean. The mapping is
    forwarded as configured and the strategy's declared schema decides — which is why adding
    a strategy that needs three parameters, or none, requires no change to this function.

    Args:
        settings: Effective configuration.
        registry: Strategy registry the configured identifier is resolved against.

    Returns:
        The constructed strategy.

    Raises:
        StrategyNotFoundError: If the configured identifier is not registered.
        StrategyParameterError: If the parameters fail the strategy's declared schema —
            missing, unknown or out of range alike.
    """
    paper = settings.paper
    return registry.create(str(paper.strategy_id), dict(paper.strategy_params))


def build_paper_deployment(  # noqa: PLR0913 - a composition root's parameters are its seams
    settings: Settings,
    *,
    symbol_rules: Mapping[str, SymbolRules],
    strategy: BaseStrategy | None = None,
    features: FeaturePipeline | None = None,
    registry: StrategyRegistry | None = None,
    clock: Clock | None = None,
    repository: PaperStateRepository | None = None,
    rules_provider: SymbolRulesProvider | None = None,
    log_paths: tuple[Path, ...] = (),
) -> PaperDeployment:
    """Wire a complete paper session from configuration.

    Args:
        settings: Effective configuration.
        symbol_rules: Venue trading rules per canonical symbol.
        strategy: A pre-built strategy, bypassing registry resolution. Intended for a
            caller that already holds one; production resolves by identifier.
        features: Feature pipeline the strategy reads. Injected rather than chosen here,
            because which features a strategy needs is a property of the strategy, and a
            composition root picking one would be deciding what the strategy sees. Defaults
            to the null pipeline, which suits a strategy that declares no features and is
            rejected by the engine's contract check for one that does.
        registry: Strategy registry; the built-in one when omitted.
        clock: Time source. A real :class:`~quantplatform.core.clock.SystemClock` unless a
            caller injects otherwise, which is the only way a test can drive days quickly.
        repository: Where session state is persisted. A durable file repository rooted at
            the configured state directory when omitted.
        rules_provider: Read-only source of venue trading rules, used to keep them current
            for the length of the run. Omitting it wires no refresh loop, which is right for
            a bounded test and wrong for a multi-day session: the rules would age past the
            risk engine's freshness budget and every order would be refused from that point
            on. Reporting says so rather than showing zeros.
        log_paths: Log files already configured, recorded on the deployment for reporting.

    Returns:
        The wired deployment.

    Raises:
        ConfigurationError: If startup validation fails.
        StorageError: If a required directory is unusable.
    """
    resolved_registry = registry if registry is not None else build_default_registry()
    # One construction, not two. Validation builds the strategy and hands it over, so the
    # session runs the instance that was approved rather than a second one built from the
    # same identifier — which is how an empty parameter mapping went unnoticed here while
    # `check` reported the deployment ready.
    resolved_strategy = (
        strategy
        if strategy is not None
        else validate_startup(settings, symbol_rules=symbol_rules, registry=resolved_registry)
    )
    resolved_clock = clock if clock is not None else SystemClock()
    paper = settings.paper

    # One store, shared by reference with the portfolio engine, the broker and the
    # pipeline. Handing each of them its own copy is what would let a refresh reach one and
    # not the others, leaving the broker rounding to a lot size the risk engine no longer
    # believes in.
    rules = SymbolRulesStore({symbol: symbol_rules[symbol] for symbol in paper.symbols})
    policy = _execution_policy(settings)
    # Read once and shared: the refresher's schedule is validated against the very same
    # freshness budget the risk engine will enforce, so the two cannot drift apart.
    risk = _risk_configuration(settings, policy)
    # One number, used twice and never converted. It is the opening balance the account is
    # *actually* seeded with, and the capital the run configuration declares. Passing an
    # empty balance tuple here while declaring capital there is precisely what produced a
    # session that could not place an order and a report claiming a loss of the whole
    # (non-existent) account.
    starting_capital = settings.backtest.initial_capital
    portfolio = SpotPortfolioEngine(
        quote_asset=settings.market.quote_asset,
        symbols=rules,
        execution_mode=ExecutionMode.PAPER,
        initial_balances=(_opening_balance(settings, starting_capital, resolved_clock),),
        source=paper.session_id,
    )
    broker = SimulatedBroker(
        symbols=rules,
        portfolio=portfolio,
        execution_mode=ExecutionMode.PAPER,
        started_at=resolved_clock.now(),
        config=ExecutionConfig(policy=policy),
        source=paper.session_id,
    )
    engine = BacktestEngine(
        config=_backtest_configuration(settings),
        strategy=resolved_strategy,
        features=features if features is not None else features_for(resolved_strategy),
        risk_engine=StandardRiskEngine(config=risk),
        broker=broker,
        portfolio=portfolio,
        symbols=rules,
    )

    reporting = _reporting_configuration(settings)
    writer = DailyReportWriter(config=reporting)
    recorder = DailyReportRecorder(
        builder=DailyReportBuilder(config=reporting, clock=resolved_clock),
        sink=writer.write,
        previous=writer.read_previous(reporting.day_of(resolved_clock.now())),
    )

    resolved_repository = (
        repository if repository is not None else FilePaperStateRepository(paper.state_directory)
    )
    session = PaperTradingSession(
        session_id=paper.session_id,
        engine=engine,
        broker=broker,
        portfolio=portfolio,
        config=engine._config,  # noqa: SLF001 - the engine owns the run configuration
        clock=resolved_clock,
        state_repository=resolved_repository,
        close_grace_seconds=paper.close_grace_seconds,
        day_rollover_observer=recorder,
    )
    feed = BinanceSpotMarketDataFeed(
        config=_market_data_configuration(settings), clock=resolved_clock
    )
    refresher = (
        SymbolRulesRefresher(
            store=rules,
            provider=rules_provider,
            clock=resolved_clock,
            refresh_interval_seconds=paper.symbol_rules_refresh_seconds,
            stale_after_seconds=risk.stale_symbol_rules_seconds,
            symbols=paper.symbols,
            open_orders=broker.open_orders,
        )
        if rules_provider is not None
        else None
    )
    runner = PaperTradingRunner(
        session=session,
        feed=feed,
        max_bars=paper.max_bars,
        feed_metrics=feed,
        symbol_rules=refresher,
    )
    watchdog = StallWatchdog(
        clock=resolved_clock,
        threshold_seconds=paper.timeframe.seconds + paper.stall_alert_margin_seconds,
        session_metrics=session.runtime_metrics,
        feed_metrics=lambda: session.feed_metrics,
        session_id=paper.session_id,
    )
    return PaperDeployment(
        settings=settings,
        clock=resolved_clock,
        feed=feed,
        session=session,
        runner=runner,
        recorder=recorder,
        writer=writer,
        repository=resolved_repository,
        symbol_rules=rules,
        refresher=refresher,
        watchdog=watchdog,
        log_paths=log_paths,
    )


# --- Configuration translation ------------------------------------------------------------------


def _market_data_configuration(settings: Settings) -> MarketDataConfiguration:
    """Translate paper settings into the feed's own configuration."""
    paper = settings.paper
    return MarketDataConfiguration(
        websocket_url=paper.websocket_url,
        symbols=paper.symbols,
        timeframe=paper.timeframe,
        market_type=settings.market.market_type,
        source_id=f"{settings.exchange.name}_spot_ws",
        receive_timeout_seconds=paper.receive_timeout_seconds,
        heartbeat_timeout_seconds=paper.heartbeat_timeout_seconds,
        close_grace_seconds=paper.close_grace_seconds,
        reconnect_initial_delay_seconds=paper.reconnect_initial_delay_seconds,
        reconnect_max_delay_seconds=paper.reconnect_max_delay_seconds,
        reconnect_backoff_multiplier=paper.reconnect_backoff_multiplier,
        max_reconnect_attempts=paper.max_reconnect_attempts,
    )


def _reporting_configuration(settings: Settings) -> ReportingConfiguration:
    """Translate paper settings into the reporting layer's configuration."""
    paper = settings.paper
    return ReportingConfiguration(
        output_directory=paper.reports_directory,
        timezone=paper.report_timezone,
        render_charts=paper.render_charts,
        chart_dpi=paper.chart_dpi,
    )


def _opening_balance(settings: Settings, capital: Decimal, clock: Clock) -> Balance:
    """Build the account's opening cash balance.

    Args:
        settings: Effective configuration, for the quote asset the account is denominated in.
        capital: The starting capital, the same value handed to the run configuration.
        clock: Injected time source for the balance's timestamp.

    Returns:
        A single free quote-asset balance. Nothing is locked, and no base asset is seeded —
        the portfolio engine is flat-start and refuses a nonzero base balance outright.
    """
    return Balance(
        asset=settings.market.quote_asset,
        free=capital,
        locked=ZERO,
        updated_at=clock.now(),
    )


def _backtest_configuration(settings: Settings) -> BacktestConfig:
    """Translate settings into the run configuration the pipeline reads.

    Every value comes from configuration that already existed. Nothing here is a new
    trading assumption; the spread, capital and rate parameters are the same ones a
    backtest of this configuration would use.
    """
    return BacktestConfig(
        run_id=settings.paper.session_id,
        quote_asset=settings.market.quote_asset,
        initial_capital=settings.backtest.initial_capital,
        execution_mode=ExecutionMode.PAPER,
        timeframe=settings.paper.timeframe,
        entry_fraction=settings.risk.target_position_fraction,
        assumed_spread_basis_points=settings.backtest.spread_basis_points,
    )


def _execution_policy(settings: Settings) -> ExecutionPolicy:
    """Build the single fee and slippage policy risk and execution both read."""
    return ExecutionPolicy(
        fee=FeePolicy(
            model=CommissionModel.BASIS_POINTS,
            basis_points=settings.backtest.commission_basis_points,
        ),
        slippage=SlippagePolicy(
            model=(
                SlippageModel.FIXED_BPS
                if settings.backtest.slippage_basis_points > 0
                else SlippageModel.OFF
            ),
            basis_points=settings.backtest.slippage_basis_points,
        ),
    )


def _risk_configuration(settings: Settings, policy: ExecutionPolicy) -> RiskConfiguration:
    """Translate risk settings into the engine's configuration.

    A translation, not a policy change: every limit below is read from
    :class:`~quantplatform.config.settings.RiskSettings` exactly as configured.
    """
    risk = settings.risk
    interval = settings.paper.timeframe.seconds

    # Risk V2 is a pure configuration fact (see `RiskConfiguration.risk_v2_active`): a
    # `risk_budget` is built only when all four of its fields are set, which `RiskSettings`
    # itself already guarantees is all-or-nothing. Every other M7/M8 field is passed through
    # exactly as configured — `None` when unset, so V1 behaviour is untouched unless `.env`
    # says otherwise.
    risk_budget: RiskBudget | None = None
    if (
        risk.risk_per_trade_pct is not None
        and risk.max_position_exposure_pct is not None
        and risk.min_stop_distance_bps is not None
        and risk.max_stop_distance_bps is not None
    ):
        risk_budget = RiskBudget(
            risk_per_trade_pct=risk.risk_per_trade_pct,
            max_position_exposure_pct=risk.max_position_exposure_pct,
            min_stop_distance_bps=risk.min_stop_distance_bps,
            max_stop_distance_bps=risk.max_stop_distance_bps,
        )

    return RiskConfiguration(
        execution_policy=policy,
        max_open_positions=risk.max_open_positions,
        max_open_orders=risk.max_open_orders,
        max_orders_per_day=risk.max_daily_orders,
        max_orders_per_hour=risk.max_hourly_orders,
        max_portfolio_exposure_pct=risk.max_exposure_fraction,
        max_daily_drawdown_pct=risk.max_daily_drawdown_fraction,
        max_total_drawdown_pct=risk.max_total_drawdown_fraction,
        max_spread_bps=risk.max_spread_basis_points,
        max_volatility=risk.max_realized_volatility_fraction,
        max_consecutive_api_failures=risk.max_consecutive_api_failures,
        # The freshness budget is configured as a multiple of the bar interval; the engine
        # wants it in seconds. Converting here keeps one source of truth rather than a
        # second staleness setting that could disagree with the first.
        stale_market_data_seconds=int(interval * risk.data_staleness_multiplier),
        risk_budget=risk_budget,
        initial_stop_distance_bps=risk.initial_stop_distance_bps,
        trailing_activation_bps=risk.trailing_activation_bps,
        trailing_distance_bps=risk.trailing_distance_bps,
        break_even_activation_bps=risk.break_even_activation_bps,
        take_profit_distance_bps=risk.take_profit_distance_bps,
        max_holding_bars=risk.max_holding_bars,
        max_daily_loss_pct=risk.max_daily_loss_pct,
        max_consecutive_losses=risk.max_consecutive_losses,
        latch_total_drawdown=risk.latch_total_drawdown,
    )


def _require_usable_directory(label: str, directory: Path) -> None:
    """Create a directory and prove it can be written to.

    Proved by writing, not by inspecting permission bits: a read-only mount, a full disk
    and a directory owned by another user all look fine until something tries.

    Raises:
        StorageError: If the directory cannot be created or written to.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StorageError(
            "required directory is not usable", setting=label, directory=str(directory)
        ) from exc
