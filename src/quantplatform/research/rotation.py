"""A cross-sectional portfolio simulator, because the production engine cannot answer M30.

The backtest engine this platform trades on is single-symbol by construction: it holds one
position, sizes it from one stop distance, and asks one strategy about one bar at a time. That
is the right shape for every milestone up to M29 and the wrong shape for a rotation rule, whose
whole question -- *which of these six is strongest right now* -- is invisible from inside a
single symbol's history.

So this module simulates portfolios instead, and the honest thing to say about it is said first:
**this is a second backtest path, and it has none of the production engine's certification.**
Nothing here runs Risk V2, sizes a position from a stop, or refuses an order. What it does do is
reuse the platform's arithmetic wherever that arithmetic already exists --
:class:`~quantplatform.features.indicators.IndicatorFeatures` computes every score, so a 72-bar
return here is the same quantity ``momentum_roc`` trades on, to the digit -- and pay the same 15
basis points per side the engine charges on a fill.

Four properties are load-bearing, and each one is tested:

**No lookahead.** The holding for bar ``i+1`` is decided from closes up to and including bar
``i``. The one optimistic assumption is that the rebalance executes at that same close, which is
the standard convention for momentum work and is stated here rather than left implicit.

**Weights drift.** A portfolio is tracked by what each position is actually *worth*, not by the
weight it was given. Skipping that would silently rebalance every multi-asset holding back to
equal weight every bar, free of charge, which is both a cost the rule never paid and a
volatility-harvesting edge it never earned.

**Costs are paid on the notional that moves.** Turnover is charged asset by asset on
``|weight_target - weight_actual|``, so swapping a whole holding pays twice: once to leave, once
to arrive. Turnover is an output of a rule, never a parameter of one.

**Cash is a position.** A rule holding nothing earns nothing and pays nothing, and the bars it
spends flat are counted and reported. Sitting out is a decision the gate gets to see.
"""

from __future__ import annotations

from bisect import insort
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from itertools import pairwise
from statistics import median
from typing import TYPE_CHECKING, Final

from quantplatform.core.models.base import DomainModel, Text, UtcDatetime
from quantplatform.features.indicators import IndicatorFeatures

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar

__all__ = [
    "AssetContribution",
    "Episode",
    "EquityPoint",
    "ExposurePolicy",
    "RotationRun",
    "RotationSpec",
    "Series",
    "align",
    "basket_index",
    "buy_and_hold",
    "equal_weight_basket",
    "exposure_for",
    "max_drawdown",
    "pairwise_correlation",
    "profit_factor",
    "simulate",
    "weights_for",
    "yearly_returns",
]

Series = Mapping[str, Sequence["MarketBar"]]
"""One canonical bar series per asset, each in ascending open-time order."""

ZERO: Final[Decimal] = Decimal(0)
ONE: Final[Decimal] = Decimal(1)
BASIS: Final[Decimal] = Decimal(10_000)

WORKING_PRECISION: Final[int] = 40
"""Digits the simulation runs at. Compounding thousands of bars at the default 28 would let
rounding accumulate into the third decimal of an annual figure."""

MIN_OBSERVATIONS: Final[int] = 2
"""Fewest points that can carry a correlation. One pair has no dispersion to correlate."""

INITIAL_EQUITY: Final[Decimal] = Decimal(10_000)
"""Starting capital. Every figure the gate reads is a ratio, so the level is arbitrary; it is
fixed so two runs are comparable in absolute terms as well as relative ones."""


# --- Records -------------------------------------------------------------------------------------


class EquityPoint(DomainModel):
    """Account value at one bar's close."""

    at: UtcDatetime
    equity: Decimal


class Episode(DomainModel):
    """One asset held from entry to exit: this simulator's unit of a trade.

    Profit is gross of nothing and net of the entry and exit costs charged on this asset's own
    weight changes, so an episode's ``net_profit`` is what holding it actually earned.
    """

    asset: Text
    opened_at: UtcDatetime
    closed_at: UtcDatetime
    bars: int
    gross_profit: Decimal
    cost: Decimal

    @property
    def net_profit(self) -> Decimal:
        """Return what the episode earned after the costs of entering and leaving it."""
        return self.gross_profit - self.cost


class AssetContribution(DomainModel):
    """What one asset added to the result over the whole run."""

    asset: Text
    gross_profit: Decimal
    cost: Decimal
    bars_held: int
    episodes: int

    @property
    def net_profit(self) -> Decimal:
        """Return this asset's contribution after the costs of trading it."""
        return self.gross_profit - self.cost


class RotationRun(DomainModel):
    """Everything one simulation produced, before any of it is judged."""

    equity_curve: tuple[EquityPoint, ...]
    episodes: tuple[Episode, ...]
    contributions: tuple[AssetContribution, ...]
    bars: int
    bars_held: int
    """Bars with at least one asset held; the complement is time spent in cash."""
    turnover: Decimal
    """Sum of absolute weight changes over the run. One full swap of a single holding is 2."""
    cost_paid: Decimal
    initial_equity: Decimal

    @property
    def final_equity(self) -> Decimal:
        """Return the account value at the last bar."""
        return self.equity_curve[-1].equity if self.equity_curve else self.initial_equity

    @property
    def total_return(self) -> Decimal:
        """Return the whole run's return on initial capital."""
        return self.final_equity / self.initial_equity - ONE

    @property
    def bars_in_cash(self) -> int:
        """Return how many bars the rule chose to hold nothing."""
        return self.bars - self.bars_held


# --- Alignment -----------------------------------------------------------------------------------


def align(series: Series) -> tuple[datetime, ...]:
    """Return every instant any asset has a bar for, ascending and without duplicates.

    The grid is the union rather than the intersection on purpose: an asset that listed in 2020
    must be absent from the ranking in 2019 and present after, which is what the union expresses
    and what an intersection would silently discard along with three years of BTC history.
    """
    return tuple(sorted({bar.open_time for bars in series.values() for bar in bars}))


def _positions(series: Series, grid: Sequence[datetime]) -> dict[str, tuple[int | None, ...]]:
    """Return, per asset and per grid slot, that asset's own bar index or ``None`` if absent."""
    out: dict[str, tuple[int | None, ...]] = {}
    for asset, bars in series.items():
        lookup = {bar.open_time: index for index, bar in enumerate(bars)}
        out[asset] = tuple(lookup.get(stamp) for stamp in grid)
    return out


def _move(bars: Sequence[MarketBar], slots: Sequence[int | None], slot: int) -> Decimal | None:
    """Return one asset's close-to-close move into ``slot``, or ``None`` if it cannot be had."""
    if slot <= 0:
        return None
    here, before = slots[slot], slots[slot - 1]
    if here is None or before is None:
        return None
    earlier = bars[before].close
    return None if earlier == ZERO else bars[here].close / earlier - ONE


# --- Scores, computed by the production pipeline -------------------------------------------------


def _scores(
    series: Series,
    positions: Mapping[str, Sequence[int | None]],
    *,
    lookback: int,
    vol_window: int,
    normalised: bool,
) -> dict[str, tuple[Decimal | None, ...]]:
    """Return each asset's ranking score at each grid slot, ``None`` where undefined.

    The score is computed by the production indicator pipeline rather than reimplemented. Raw
    momentum is ``roc_<lookback>``. The volatility-normalised score divides it by
    ``rvol_<vol_window> * sqrt(lookback)``, which is exactly the hurdle ``vol_momentum`` compares
    its own threshold against -- so a cross-sectional score of 1.0 means the same thing here as
    it does in that strategy.
    """
    roc_name = f"roc_{lookback}"
    vol_name = f"rvol_{vol_window}"
    pipeline = IndicatorFeatures([roc_name, vol_name] if normalised else [roc_name])
    root = Decimal(lookback).sqrt()
    out: dict[str, tuple[Decimal | None, ...]] = {}
    for asset, bars in series.items():
        out[asset] = tuple(
            None
            if slot is None
            else _one_score(
                pipeline.compute(bars[: slot + 1]),
                roc_name=roc_name,
                vol_name=vol_name,
                normalised=normalised,
                root=root,
            )
            for slot in positions[asset]
        )
    return out


def _one_score(
    features: Mapping[str, Decimal],
    *,
    roc_name: str,
    vol_name: str,
    normalised: bool,
    root: Decimal,
) -> Decimal | None:
    """Return one asset's score from its computed features, or ``None`` if a window is short."""
    momentum = features.get(roc_name)
    if momentum is None:
        return None
    if not normalised:
        return momentum
    volatility = features.get(vol_name)
    if volatility is None or volatility == ZERO:
        return None
    return momentum / (volatility * root)


# --- The market aggregate ------------------------------------------------------------------------


def basket_index(series: Series, grid: Sequence[datetime]) -> tuple[Decimal | None, ...]:
    """Return an equal-weight index of whatever assets are present, chained bar by bar.

    The aggregate the regime-filtered family asks about, and an *index* rather than a portfolio:
    it pays no costs because nobody trades it. Each bar's move is the simple mean of the moves of
    every asset present at both ends of that bar, so an asset joining the universe changes the
    composition without putting a step in the level.
    """
    positions = _positions(series, grid)
    level: Decimal | None = None
    out: list[Decimal | None] = []
    for slot in range(len(grid)):
        moves = [
            move
            for asset, bars in series.items()
            if (move := _move(bars, positions[asset], slot)) is not None
        ]
        if moves:
            step = sum(moves, start=ZERO) / Decimal(len(moves))
            # The slot before the first measurable move is the index's base of one. Seeding to
            # one *at* that move instead would silently discard a bar of the aggregate and
            # shift every moving average built on it by one slot.
            level = (ONE if level is None else level) * (ONE + step)
        out.append(level)
    return tuple(out)


def _volatility_columns(
    levels: Sequence[Decimal | None], window: int
) -> tuple[tuple[Decimal | None, ...], tuple[Decimal | None, ...]]:
    """Return the aggregate's realised volatility per slot, and its causal running median.

    Volatility is the population standard deviation of the index's bar-to-bar moves over
    ``window`` bars, the same definition ``rvol_<n>`` computes on a price series. The median
    beside it is taken over observations up to and including that slot and never beyond, so a
    volatility target built from it knows nothing the tape had not already shown.
    """
    moves: list[Decimal | None] = [None]
    for before, here in pairwise(levels):
        moves.append(
            None if before is None or here is None or before == ZERO else here / before - ONE
        )
    vols: list[Decimal | None] = []
    medians: list[Decimal | None] = []
    seen: list[Decimal] = []
    for slot in range(len(levels)):
        recent = moves[slot + 1 - window : slot + 1] if slot + 1 >= window else []
        if recent and all(move is not None for move in recent):
            values = [move for move in recent if move is not None]
            mean = sum(values, start=ZERO) / Decimal(len(values))
            vol = (
                sum(((value - mean) ** 2 for value in values), start=ZERO) / Decimal(len(values))
            ).sqrt()
            vols.append(vol)
            insort(seen, vol)
        else:
            vols.append(None)
        medians.append(median(seen) if seen else None)
    return tuple(vols), tuple(medians)


def _above_average(levels: Sequence[Decimal | None], slot: int, window: int) -> bool:
    """Return whether the index sits above the mean of its own last ``window`` levels.

    That mean is what ``sma_<n>`` computes. A slot without a full window answers ``False``: the
    filter has not earned an opinion yet, and the declared behaviour of an unfilled filter is to
    stay out rather than to assume a favourable regime.
    """
    if slot + 1 < window:
        return False
    recent = levels[slot + 1 - window : slot + 1]
    current = levels[slot]
    if current is None or any(level is None for level in recent):
        return False
    values = [level for level in recent if level is not None]
    return current > sum(values, start=ZERO) / Decimal(len(values))


# --- The rule ------------------------------------------------------------------------------------


def weights_for(
    ranked: Sequence[tuple[str, Decimal]],
    *,
    hold: int,
    threshold: Decimal | None,
) -> dict[str, Decimal]:
    """Return the equal weights a rule assigns, given assets already ranked best first.

    Args:
        ranked: Eligible assets with their scores, best first.
        hold: How many to hold at once.
        threshold: Score an asset must exceed to be held at all, or ``None`` to hold the top
            ``hold`` unconditionally.

    Returns:
        One weight per held asset, summing to one, or an empty mapping meaning cash.
    """
    chosen = [asset for asset, score in ranked[:hold] if threshold is None or score > threshold]
    if not chosen:
        return {}
    weight = ONE / Decimal(len(chosen))
    return dict.fromkeys(chosen, weight)


# --- The simulation ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExposurePolicy:
    """How much of the account a rule is allowed to have at risk, and never which asset.

    The overlay is deliberately separate from the ranking. A rule's signals decide *what* is
    held; this decides *how much*, and the two cannot reach into each other -- which is the
    only way to ask "can this drawdown be controlled without touching the edge?" and get an
    answer that means anything.

    At most one mechanism may be set. Stacking two would produce a result no single mechanism
    explains, and there would be no way to say which one earned it.
    """

    fixed: Decimal | None = None
    """Constant share of the account at risk, the rest in cash."""
    volatility_window: int | None = None
    """Bars the aggregate's realised volatility is measured over. When set, exposure is the
    trailing median of that volatility divided by its current value, capped at one: full risk
    in a typically calm tape, less than full when the tape is rougher than it has usually
    been. The median is taken over observations up to the current bar only, so it uses no
    information from the future and needs no threshold chosen by anyone."""
    drawdown_power: int | None = None
    """Exponent on remaining capital: exposure is ``(1 - drawdown) ** power``. The peak it is
    measured from is a running maximum that never resets, so recovering re-risks only to the
    extent the account has actually recovered. Parameter-free apart from the exponent, and
    no level anywhere for a threshold to be chosen at."""

    def __post_init__(self) -> None:
        """Reject a policy that sets more than one mechanism, or a nonsensical level.

        Raises:
            ValueError: If two mechanisms are set at once, or a fixed exposure is outside
                ``(0, 1]`` -- above one would be leverage, which is excluded by instruction.
        """
        chosen = [
            name
            for name, value in (
                ("fixed", self.fixed),
                ("volatility_window", self.volatility_window),
                ("drawdown_power", self.drawdown_power),
            )
            if value is not None
        ]
        if len(chosen) > 1:
            msg = f"an exposure policy sets one mechanism, not {chosen}"
            raise ValueError(msg)
        if self.fixed is not None and not (ZERO < self.fixed <= ONE):
            msg = "a fixed exposure lies in (0, 1]: above one is leverage"
            raise ValueError(msg)

    @property
    def controlled(self) -> bool:
        """Return whether this policy does anything at all."""
        return (
            self.fixed is not None
            or self.volatility_window is not None
            or self.drawdown_power is not None
        )


def exposure_for(
    policy: ExposurePolicy,
    *,
    drawdown: Decimal,
    volatility: Decimal | None,
    median_volatility: Decimal | None,
) -> Decimal:
    """Return the share of the account this policy puts at risk right now, in ``[0, 1]``.

    Args:
        policy: The declared mechanism.
        drawdown: Current fall from the running peak, as a positive fraction.
        volatility: The aggregate's realised volatility at this bar, or ``None`` if its window
            does not yet fit.
        median_volatility: Median of every volatility observed up to and including this bar.

    Returns:
        One for an uncontrolled policy. For volatility targeting, one while the measurement is
        unavailable -- an unmeasured tape is not evidence of a rough one, and guessing either
        way would be a decision the data has not supported yet.
    """
    if policy.fixed is not None:
        return policy.fixed
    if policy.volatility_window is not None:
        if volatility is None or median_volatility is None or volatility <= ZERO:
            return ONE
        return min(ONE, median_volatility / volatility)
    if policy.drawdown_power is not None:
        remaining = max(ZERO, ONE - drawdown)
        return remaining**policy.drawdown_power
    return ONE


@dataclass(frozen=True)
class RotationSpec:
    """One rotation rule's mechanics, as a record rather than nine call arguments.

    Frozen and passed whole, so a run can never be recorded against a different set of numbers
    than the ones it was executed with -- the failure mode a long positional signature invites.
    """

    lookback: int
    """Bars the ranking score looks back over."""
    hold: int
    """How many assets are held at once, equally weighted at each rebalance."""
    normalised: bool = False
    """Whether to rank on volatility-normalised momentum rather than raw momentum."""
    threshold: Decimal | None = None
    """Score floor below which the rule holds cash instead of the best asset."""
    regime_filter: int | None = None
    """Length of the aggregate basket's moving-average filter, or ``None`` for no filter."""
    vol_window: int = 1
    """Window the normalising volatility is measured over."""
    rebalance_on_entry_only: bool = False
    """Equalise weights only when the held set changes, letting them drift otherwise. What
    makes a buy-and-hold benchmark buy-and-hold."""
    exposure: ExposurePolicy = field(default_factory=ExposurePolicy)
    """How much of the account to put behind the signals. Never which signals."""


@dataclass(frozen=True)
class _Plan:
    """Everything the bar loop reads, gathered so it travels as one argument."""

    series: Series
    grid: tuple[datetime, ...]
    positions: Mapping[str, Sequence[int | None]]
    scores: Mapping[str, Sequence[Decimal | None]]
    levels: Sequence[Decimal | None]
    hold: int
    threshold: Decimal | None
    regime_filter: int | None
    rate: Decimal
    start: datetime | None
    rebalance_on_entry_only: bool
    exposure: ExposurePolicy
    volatility: Sequence[Decimal | None]
    """The aggregate's realised volatility at each slot, empty when nothing reads it."""
    median_volatility: Sequence[Decimal | None]
    """The median of every volatility observed up to each slot -- causal by construction."""
    """When set, weights are only equalised as assets join the universe and left to drift
    otherwise -- the buy-and-hold basket, whose whole point is that it does not trade."""


@dataclass
class _Book:
    """The mutable state of one run: what is held, what it earned, what it cost."""

    equity: Decimal = INITIAL_EQUITY
    actual: dict[str, Decimal] = field(default_factory=dict)
    """Fraction of equity each position is currently *worth*, which drifts with the market."""
    open_at: dict[str, datetime] = field(default_factory=dict)
    open_bars: dict[str, int] = field(default_factory=dict)
    open_gross: dict[str, Decimal] = field(default_factory=dict)
    open_cost: dict[str, Decimal] = field(default_factory=dict)
    episodes: list[Episode] = field(default_factory=list)
    curve: list[EquityPoint] = field(default_factory=list)
    gross: dict[str, Decimal] = field(default_factory=dict)
    spent: dict[str, Decimal] = field(default_factory=dict)
    bars_of: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    turnover: Decimal = ZERO
    paid: Decimal = ZERO
    bars: int = 0
    bars_held: int = 0


def simulate(
    series: Series,
    spec: RotationSpec,
    *,
    cost_basis_points: Decimal,
    start: datetime | None = None,
) -> RotationRun:
    """Run one rotation rule over aligned series and record what it did.

    Args:
        series: One bar series per asset.
        spec: The rule's mechanics.
        cost_basis_points: Cost charged on each side of each weight change.
        start: Ignore grid slots before this instant, for an out-of-sample window. History
            before it is still read, so the first in-window decision is as informed as a
            continuous run's would be -- the alternative confuses a short window with a cold
            start.

    Returns:
        The run, with no judgement applied to it.
    """
    grid = align(series)
    positions = _positions(series, grid)
    window = spec.exposure.volatility_window
    with localcontext() as ctx:
        ctx.prec = WORKING_PRECISION
        needs_levels = spec.regime_filter is not None or window is not None
        levels = basket_index(series, grid) if needs_levels else ()
        vols, medians = _volatility_columns(levels, window) if window is not None else ((), ())
        return _walk(
            _Plan(
                series=series,
                grid=grid,
                positions=positions,
                scores=_scores(
                    series,
                    positions,
                    lookback=spec.lookback,
                    vol_window=spec.vol_window,
                    normalised=spec.normalised,
                ),
                levels=levels,
                hold=spec.hold,
                threshold=spec.threshold,
                regime_filter=spec.regime_filter,
                rate=cost_basis_points / BASIS,
                start=start,
                rebalance_on_entry_only=spec.rebalance_on_entry_only,
                exposure=spec.exposure,
                volatility=vols,
                median_volatility=medians,
            )
        )


def _scaled(
    plan: _Plan, slot: int, wanted: dict[str, Decimal], drawdown: Decimal
) -> dict[str, Decimal]:
    """Return the signal's weights scaled by the declared exposure, the remainder in cash.

    The scaling happens *after* the ranking has chosen, and it cannot reorder or replace a
    choice -- which is what lets M31 ask whether the drawdown is controllable without touching
    the edge, and get an answer about exposure alone.
    """
    if not plan.exposure.controlled or not wanted:
        return wanted
    share = exposure_for(
        plan.exposure,
        drawdown=drawdown,
        volatility=plan.volatility[slot] if plan.volatility else None,
        median_volatility=plan.median_volatility[slot] if plan.median_volatility else None,
    )
    if share <= ZERO:
        return {}
    return {asset: weight * share for asset, weight in wanted.items()}


def _target(plan: _Plan, slot: int, actual: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Return the weights the rule wants to hold over the bar after ``slot``."""
    if plan.regime_filter is not None and not _above_average(plan.levels, slot, plan.regime_filter):
        return {}
    eligible: list[tuple[str, Decimal]] = []
    for asset in plan.series:
        score = plan.scores[asset][slot]
        tradeable = slot + 1 < len(plan.grid) and plan.positions[asset][slot + 1] is not None
        if score is not None and tradeable:
            eligible.append((asset, score))
    eligible.sort(key=lambda pair: (-pair[1], pair[0]))
    wanted = weights_for(eligible, hold=plan.hold, threshold=plan.threshold)
    if plan.rebalance_on_entry_only and set(wanted) == set(actual) and actual:
        # The held set is unchanged, so a buy-and-hold benchmark does nothing at all: the
        # positions keep whatever weights the market has given them.
        return dict(actual)
    return wanted


def _walk(plan: _Plan) -> RotationRun:
    """Step through the grid, rebalancing at each close and holding over the next bar."""
    book = _Book(
        actual={},
        gross=dict.fromkeys(plan.series, ZERO),
        spent=dict.fromkeys(plan.series, ZERO),
        bars_of=dict.fromkeys(plan.series, 0),
        counts=dict.fromkeys(plan.series, 0),
    )
    peak = INITIAL_EQUITY
    for slot in range(len(plan.grid) - 1):
        if plan.start is not None and plan.grid[slot] < plan.start:
            continue
        # A running maximum that never resets: recovering re-risks only as far as the account
        # has actually recovered, which is what "do not reset references" asks for.
        peak = max(peak, book.equity)
        drawdown = (peak - book.equity) / peak if peak > ZERO else ZERO
        _rebalance(
            plan, book, slot, _scaled(plan, slot, _target(plan, slot, book.actual), drawdown)
        )
        book.bars += 1
        if book.actual:
            book.bars_held += 1
        _hold(plan, book, slot)
        book.curve.append(EquityPoint(at=plan.grid[slot + 1], equity=book.equity))
    _close_out(book, plan.grid[-1])
    return RotationRun(
        equity_curve=tuple(book.curve),
        episodes=tuple(book.episodes),
        contributions=tuple(
            AssetContribution(
                asset=asset,
                gross_profit=book.gross[asset],
                cost=book.spent[asset],
                bars_held=book.bars_of[asset],
                episodes=book.counts[asset],
            )
            for asset in plan.series
        ),
        bars=book.bars,
        bars_held=book.bars_held,
        turnover=book.turnover,
        cost_paid=book.paid,
        initial_equity=INITIAL_EQUITY,
    )


def _rebalance(plan: _Plan, book: _Book, slot: int, target: Mapping[str, Decimal]) -> None:
    """Charge the cost of moving from what is held to ``target``, opening and closing episodes."""
    for asset in plan.series:
        before = book.actual.get(asset, ZERO)
        after = target.get(asset, ZERO)
        if before == after:
            continue
        charge = book.equity * abs(after - before) * plan.rate
        book.turnover += abs(after - before)
        book.paid += charge
        book.equity -= charge
        if before == ZERO:
            book.open_at[asset] = plan.grid[slot]
            book.open_bars[asset] = 0
            book.open_gross[asset] = ZERO
            book.open_cost[asset] = charge
        elif after == ZERO:
            _finish(book, asset, plan.grid[slot], charge)
        else:
            book.open_cost[asset] += charge
    book.actual = {asset: weight for asset, weight in target.items() if weight > ZERO}


def _finish(book: _Book, asset: str, at: datetime, charge: Decimal) -> None:
    """Close one asset's episode and file it, charging the exit cost to it."""
    cost = book.open_cost.pop(asset) + charge
    book.episodes.append(
        Episode(
            asset=asset,
            opened_at=book.open_at.pop(asset),
            closed_at=at,
            bars=book.open_bars.pop(asset),
            gross_profit=book.open_gross.pop(asset),
            cost=cost,
        )
    )
    book.counts[asset] += 1
    book.spent[asset] += cost


def _hold(plan: _Plan, book: _Book, slot: int) -> None:
    """Apply one bar's market move to every position and let the weights drift with it."""
    if not book.actual:
        return
    opening = book.equity
    values: dict[str, Decimal] = {}
    cash = opening * (ONE - sum(book.actual.values(), start=ZERO))
    for asset, weight in book.actual.items():
        move = _move(plan.series[asset], plan.positions[asset], slot + 1)
        stake = opening * weight
        profit = ZERO if move is None else stake * move
        values[asset] = stake + profit
        book.gross[asset] += profit
        book.open_gross[asset] += profit
        book.open_bars[asset] += 1
        book.bars_of[asset] += 1
    book.equity = sum(values.values(), start=ZERO) + cash
    book.actual = (
        {asset: value / book.equity for asset, value in values.items()}
        if book.equity > ZERO
        else {}
    )


def _close_out(book: _Book, at: datetime) -> None:
    """Mark every still-open episode to the final bar, so nothing goes uncounted."""
    for asset in list(book.open_at):
        _finish(book, asset, at, ZERO)


# --- Benchmarks ----------------------------------------------------------------------------------


def buy_and_hold(bars: Sequence[MarketBar], *, cost_basis_points: Decimal) -> RotationRun:
    """Return the run produced by buying once and never trading again.

    Enters on the second bar rather than the first, because a one-bar return is the shortest
    ranking score that exists and no rule can act before it has one. Over a nine-year daily
    history that is one bar of the roughly 3300 available.
    """
    return simulate(
        {bars[0].symbol: bars},
        RotationSpec(lookback=1, hold=1, rebalance_on_entry_only=True),
        cost_basis_points=cost_basis_points,
    )


def equal_weight_basket(series: Series, *, cost_basis_points: Decimal) -> RotationRun:
    """Return the run produced by holding every available asset, bought once each.

    The static comparison M30 exists to beat: no ranking, no timing. Weights are equalised only
    when an asset joins the universe and left to drift in between, so the basket's turnover is
    the cost of joining and nothing else. Rebalancing it daily would hand the benchmark a free
    volatility-harvesting edge and charge it no commission for the privilege.
    """
    return simulate(
        series,
        RotationSpec(lookback=1, hold=len(series), rebalance_on_entry_only=True),
        cost_basis_points=cost_basis_points,
    )


# --- Reading a run -------------------------------------------------------------------------------


def max_drawdown(curve: Sequence[EquityPoint]) -> Decimal:
    """Return the deepest peak-to-trough fall in the equity curve, as a positive fraction."""
    peak = ZERO
    worst = ZERO
    for point in curve:
        peak = max(peak, point.equity)
        if peak > ZERO:
            worst = max(worst, (peak - point.equity) / peak)
    return worst


def yearly_returns(curve: Sequence[EquityPoint], initial: Decimal) -> dict[int, Decimal]:
    """Return each calendar year's return, cut from the continuous curve."""
    out: dict[int, Decimal] = {}
    opening = initial
    for year in sorted({point.at.year for point in curve}):
        closing = [point.equity for point in curve if point.at.year == year][-1]
        out[year] = closing / opening - ONE if opening != ZERO else ZERO
        opening = closing
    return out


def profit_factor(episodes: Sequence[Episode]) -> Decimal | None:
    """Return gross profit over gross loss across episodes, or ``None`` if nothing was lost."""
    won = sum((e.net_profit for e in episodes if e.net_profit > ZERO), start=ZERO)
    lost = sum((-e.net_profit for e in episodes if e.net_profit < ZERO), start=ZERO)
    return None if lost == ZERO else won / lost


def pairwise_correlation(series: Series, assets: Sequence[str]) -> Decimal | None:
    """Return the mean Pearson correlation of bar returns across every pair of ``assets``.

    Measured on the slots where both members of a pair are present, the only window where the
    question has an answer. ``None`` when fewer than two assets were given.
    """
    if len(assets) < MIN_OBSERVATIONS:
        return None
    grid = align(series)
    positions = _positions(series, grid)
    returns = {
        asset: [_move(series[asset], positions[asset], slot) for slot in range(len(grid))]
        for asset in assets
    }
    values = [
        found
        for first, second in _pairs(assets)
        if (found := _correlation(returns[first], returns[second])) is not None
    ]
    if not values:
        return None
    return sum(values, start=ZERO) / Decimal(len(values))


def _pairs(assets: Sequence[str]) -> list[tuple[str, str]]:
    """Return every unordered pair of distinct assets, in declaration order."""
    return [(assets[i], assets[j]) for i in range(len(assets)) for j in range(i + 1, len(assets))]


def _correlation(left: Sequence[Decimal | None], right: Sequence[Decimal | None]) -> Decimal | None:
    """Return the Pearson correlation of two return columns over their common slots."""
    both = [(a, b) for a, b in zip(left, right, strict=True) if a is not None and b is not None]
    if len(both) < MIN_OBSERVATIONS:
        return None
    n = Decimal(len(both))
    mean_x = sum((a for a, _ in both), start=ZERO) / n
    mean_y = sum((b for _, b in both), start=ZERO) / n
    covariance = sum(((a - mean_x) * (b - mean_y) for a, b in both), start=ZERO)
    var_x = sum(((a - mean_x) ** 2 for a, _ in both), start=ZERO)
    var_y = sum(((b - mean_y) ** 2 for _, b in both), start=ZERO)
    if ZERO in (var_x, var_y):
        return None
    return covariance / (var_x * var_y).sqrt()
