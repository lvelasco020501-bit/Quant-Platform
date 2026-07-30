"""Shared builders and fakes for the Phase 2 data-layer tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

from quantplatform.config.settings import DataSettings
from quantplatform.core.clock import Clock, SimulatedClock
from quantplatform.core.enums import (
    BarWriteOutcome,
    MarketType,
    Timeframe,
)
from quantplatform.core.models.data import BarWriteResult, DataQualityFinding, IngestionRun
from quantplatform.core.models.market import MarketBar
from quantplatform.data.closed_candle import ClosedCandlePolicy
from quantplatform.data.findings import FindingRecorder
from quantplatform.data.records import RawBarRecord
from quantplatform.data.validation import DatasetExpectations

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "csv"

SYMBOL = "BTC/USDT"
MARKET_TYPE = MarketType.SPOT
TIMEFRAME = Timeframe.H1

# Every fixture's data sits on 2026-01-01 and closes by 05:00. This instant is late enough
# that all of it has closed, but close enough that the default freshness budget (twice the
# 1h timeframe) is not exceeded, so fixtures do not pick up an incidental staleness finding.
AFTER_ALL_FIXTURES = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)

EXPECTATIONS = DatasetExpectations(
    symbol=SYMBOL,
    market_type=MARKET_TYPE,
    timeframe=TIMEFRAME,
)


def fixture(name: str) -> Path:
    """Return the path of a named CSV fixture."""
    return FIXTURE_DIR / name


def make_clock(now: datetime = AFTER_ALL_FIXTURES) -> SimulatedClock:
    """Return a simulated clock fixed at ``now``."""
    return SimulatedClock(now)


def make_recorder(
    *,
    clock: Clock | None = None,
    run_id: UUID | None = None,
    source: str = "test",
) -> FindingRecorder:
    """Return a finding recorder bound to the standard test expectations."""
    return FindingRecorder(
        run_id=run_id or uuid4(),
        source=source,
        clock=clock or make_clock(),
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
    )


def make_policy(
    *,
    clock: Clock | None = None,
    grace_seconds: int = 0,
    reference_time: datetime | None = None,
) -> ClosedCandlePolicy:
    """Return a closed-candle policy over a simulated clock."""
    return ClosedCandlePolicy(
        clock=clock or make_clock(),
        grace_period=timedelta(seconds=grace_seconds),
        reference_time=reference_time,
    )


def make_raw_record(
    *,
    source_row: int = 1,
    symbol: str = SYMBOL,
    market_type: str = "spot",
    timeframe: str = "1h",
    open_time: str = "2026-01-01T00:00:00+00:00",
    close_time: str = "2026-01-01T01:00:00+00:00",
    open_price: str = "50000",
    high: str = "50200",
    low: str = "49900",
    close: str = "50100",
    volume: str = "12.5",
    trade_count: str = "100",
) -> RawBarRecord:
    """Build a raw record whose defaults are a sound BTC/USDT 1h candle."""
    return RawBarRecord(
        source="test",
        source_row=source_row,
        symbol=symbol,
        market_type=market_type,
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        trade_count=trade_count,
    )


def make_bar(
    *,
    hour: int = 0,
    close: str = "50100",
    symbol: str = SYMBOL,
    source: str = "test",
) -> MarketBar:
    """Build a valid closed bar opening at the given hour on 2026-01-01."""
    open_time = datetime(2026, 1, 1, hour, tzinfo=UTC)
    return MarketBar(
        symbol=symbol,
        market_type=MARKET_TYPE,
        timeframe=TIMEFRAME,
        open_time=open_time,
        close_time=open_time + TIMEFRAME.duration,
        open="50000",
        high="60000",
        low="40000",
        close=close,
        volume="12.5",
        quote_volume=None,
        trade_count=100,
        source=source,
        is_closed=True,
    )


def data_settings(**overrides: object) -> DataSettings:
    """Return data settings with the given overrides applied."""
    return DataSettings(**overrides)  # type: ignore[arg-type]


class InMemoryMarketBarRepository:
    """An in-memory stand-in for the market bar repository.

    Reproduces the real repository's contract: identity is the natural key, an identical
    re-add is an exact duplicate, and differing values conflict without overwriting.
    """

    def __init__(self) -> None:
        self.bars: dict[tuple[str, MarketType, Timeframe, datetime], MarketBar] = {}
        self.fail_on_add = False

    async def add_bars(self, bars: Sequence[MarketBar]) -> Sequence[BarWriteResult]:
        """Stage bars, classifying each against what is already stored."""
        if self.fail_on_add:
            msg = "simulated storage failure"
            raise RuntimeError(msg)

        results: list[BarWriteResult] = []
        for bar in bars:
            key = (bar.symbol, bar.market_type, bar.timeframe, bar.open_time)
            stored = self.bars.get(key)
            if stored is None:
                self.bars[key] = bar
                results.append(BarWriteResult(bar=bar, outcome=BarWriteOutcome.INSERTED))
            elif _same_values(stored, bar):
                results.append(BarWriteResult(bar=bar, outcome=BarWriteOutcome.EXACT_DUPLICATE))
            else:
                results.append(
                    BarWriteResult(
                        bar=bar,
                        outcome=BarWriteOutcome.CONFLICTING,
                        existing_bar=stored,
                    )
                )
        return results

    async def get_bars(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> Sequence[MarketBar]:
        """Return stored bars in ``[start, end)`` ordered by open time."""
        selected = [
            bar
            for (bar_symbol, bar_market, bar_timeframe, open_time), bar in self.bars.items()
            if bar_symbol == symbol
            and bar_market is market_type
            and bar_timeframe is timeframe
            and start <= open_time < end
        ]
        return tuple(sorted(selected, key=lambda bar: bar.open_time))

    async def get_latest_bar(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
    ) -> MarketBar | None:
        """Return the most recent stored bar, if any."""
        candidates = [
            bar
            for (bar_symbol, bar_market, bar_timeframe, _), bar in self.bars.items()
            if bar_symbol == symbol and bar_market is market_type and bar_timeframe is timeframe
        ]
        return max(candidates, key=lambda bar: bar.open_time, default=None)

    async def exists(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
        open_time: datetime,
    ) -> bool:
        """Return whether a bar is stored under this natural key."""
        return (symbol, market_type, timeframe, open_time) in self.bars

    async def count_bars(
        self,
        *,
        symbol: str,
        market_type: MarketType,
        timeframe: Timeframe,
    ) -> int:
        """Return how many bars are stored for this instrument."""
        return len(
            [
                bar
                for (bar_symbol, bar_market, bar_timeframe, _), bar in self.bars.items()
                if bar_symbol == symbol and bar_market is market_type and bar_timeframe is timeframe
            ]
        )


class InMemoryIngestionRunRepository:
    """An in-memory stand-in for the ingestion run repository."""

    def __init__(self) -> None:
        self.runs: dict[UUID, IngestionRun] = {}
        self.findings: dict[UUID, tuple[DataQualityFinding, ...]] = {}

    async def record_run(
        self,
        run: IngestionRun,
        findings: Sequence[DataQualityFinding],
    ) -> None:
        """Stage a run and its findings."""
        self.runs[run.run_id] = run
        self.findings[run.run_id] = tuple(findings)

    async def get_run(self, run_id: UUID) -> IngestionRun | None:
        """Return a staged run by id."""
        return self.runs.get(run_id)

    async def get_findings(self, run_id: UUID) -> Sequence[DataQualityFinding]:
        """Return the findings staged against a run."""
        return self.findings.get(run_id, ())


class InMemoryDataUnitOfWork:
    """An in-memory unit of work whose commit semantics mirror the real one.

    Staged writes are held aside and only merged into the shared store on
    :meth:`commit`, so a scope that raises or is simply never committed leaves the store
    untouched — which is what makes the "no bars persist on fatal failure" test meaningful
    rather than vacuous.
    """

    def __init__(
        self,
        bar_store: InMemoryMarketBarRepository,
        run_store: InMemoryIngestionRunRepository,
    ) -> None:
        self._bar_store = bar_store
        self._run_store = run_store
        self._staged_bars = InMemoryMarketBarRepository()
        self._staged_runs = InMemoryIngestionRunRepository()
        self.committed = False

    @property
    def bars(self) -> InMemoryMarketBarRepository:
        """Return the staged bar repository."""
        return self._staged_bars

    @property
    def runs(self) -> InMemoryIngestionRunRepository:
        """Return the staged run repository."""
        return self._staged_runs

    async def __aenter__(self) -> InMemoryDataUnitOfWork:
        """Begin the scope, seeding staged state from the shared store."""
        self._staged_bars = InMemoryMarketBarRepository()
        self._staged_bars.bars = dict(self._bar_store.bars)
        self._staged_bars.fail_on_add = self._bar_store.fail_on_add
        self._staged_runs = InMemoryIngestionRunRepository()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Discard staged state unless it was committed."""
        return

    async def commit(self) -> None:
        """Merge staged state into the shared store."""
        self._bar_store.bars = dict(self._staged_bars.bars)
        self._run_store.runs.update(self._staged_runs.runs)
        self._run_store.findings.update(self._staged_runs.findings)
        self.committed = True

    async def rollback(self) -> None:
        """Discard staged state."""
        self._staged_bars = InMemoryMarketBarRepository()
        self._staged_runs = InMemoryIngestionRunRepository()


def _same_values(left: MarketBar, right: MarketBar) -> bool:
    """Return whether two bars carry identical OHLCV and trade count."""
    return (
        left.open == right.open
        and left.high == right.high
        and left.low == right.low
        and left.close == right.close
        and left.volume == right.volume
        and left.quote_volume == right.quote_volume
        and left.trade_count == right.trade_count
    )
