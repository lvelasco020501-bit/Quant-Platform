"""Allocating one account across several rules and several markets.

The one thing M34 adds to work this project already has. The signals come from frozen strategies
(:mod:`~quantplatform.research.sleeve`), the per-market eligibility from M32's point-in-time
liquidity rule, and the costs from the platform's own fee and slippage. What lives here is the
arithmetic deciding how much of the account stands behind each active signal:

    one active signal holds ``1 / (universe size x sleeves)`` of the account, no market holds
    more than ``1 / universe size`` across every sleeve long in it, and what is not allocated is
    cash earning nothing.

Both fractions are derived rather than chosen, and a fixed denominator is what lets the portfolio
be partly in cash at all: dividing by the *active* count instead would put the whole account to
work the moment one signal fired.

**Attribution is why positions are tracked per sleeve rather than per market.** Two sleeves long
the same market hold the same instrument and earn the same move, so netting them would be
cheaper -- and would make "does one strategy explain the result" unanswerable. They are kept
apart so both concentration questions, by market and by sleeve, can be asked of the same run.

The equity arithmetic mirrors :mod:`~quantplatform.research.rotation`: weights drift with the
market, cost is charged on the notional that actually moves, and cash is a position. A test holds
this module's single-asset, single-sleeve answer to that module's ``buy_and_hold``, which is what
makes the shared arithmetic safe to have written twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

from quantplatform.core.models.base import DomainModel, Text
from quantplatform.research.rotation import (
    BASIS,
    INITIAL_EQUITY,
    ONE,
    WORKING_PRECISION,
    ZERO,
    EquityPoint,
    Series,
    align,
)

if TYPE_CHECKING:
    from quantplatform.core.models.market import MarketBar

__all__ = [
    "Allocation",
    "Holding",
    "PortfolioRun",
    "SleeveContribution",
    "deployed",
    "normalise_to",
    "positions",
    "simulate_portfolio",
    "targets_for",
]

Holding = tuple[str, str]
"""One position: ``(sleeve key, market)``. Two sleeves in one market are two holdings."""


def positions(series: Series, grid: Sequence[datetime]) -> dict[str, tuple[int | None, ...]]:
    """Return, per market and per grid slot, that market's own bar index or ``None`` if absent."""
    out: dict[str, tuple[int | None, ...]] = {}
    for asset, bars in series.items():
        lookup = {bar.open_time: index for index, bar in enumerate(bars)}
        out[asset] = tuple(lookup.get(stamp) for stamp in grid)
    return out


def _move(bars: Sequence[MarketBar], slots: Sequence[int | None], slot: int) -> Decimal | None:
    """Return one market's close-to-close move into ``slot``, or ``None`` if it cannot be had."""
    if slot <= 0:
        return None
    here, before = slots[slot], slots[slot - 1]
    if here is None or before is None:
        return None
    earlier = bars[before].close
    return None if earlier == ZERO else bars[here].close / earlier - ONE


# --- The allocation -------------------------------------------------------------------------------


class Allocation(DomainModel):
    """The declared allocation, as a record so a run cannot be filed under other numbers."""

    universe_size: int
    """How many markets may be funded at once; also the denominator of the per-market cap."""
    sleeves: int
    """How many rules the portfolio holds."""

    @property
    def per_signal(self) -> Decimal:
        """Return the share of the account one active signal receives."""
        return ONE / (Decimal(self.universe_size) * Decimal(self.sleeves))

    @property
    def per_asset_cap(self) -> Decimal:
        """Return the most any one market may hold across every sleeve long in it."""
        return ONE / Decimal(self.universe_size)


def targets_for(
    masks: Mapping[Holding, Sequence[bool]],
    eligible: Sequence[frozenset[str]],
    allocation: Allocation,
) -> tuple[dict[Holding, Decimal], ...]:
    """Return the target weights for every grid slot.

    Args:
        masks: Per holding, whether that sleeve wanted that market at each slot.
        eligible: Per slot, which markets the point-in-time liquidity rule admits.
        allocation: The declared fractions.

    Returns:
        One mapping per slot, holding to weight, omitting anything unfunded. A market whose
        sleeves together want more than the cap is scaled back proportionally, so the cap binds
        on the market rather than on whichever sleeve is examined first.
    """
    slots = len(eligible)
    out: list[dict[Holding, Decimal]] = []
    for slot in range(slots):
        wanted: dict[Holding, Decimal] = {
            holding: allocation.per_signal
            for holding, mask in masks.items()
            if mask[slot] and holding[1] in eligible[slot]
        }
        by_asset: dict[str, Decimal] = {}
        for (_, asset), weight in wanted.items():
            by_asset[asset] = by_asset.get(asset, ZERO) + weight
        for holding in list(wanted):
            total = by_asset[holding[1]]
            if total > allocation.per_asset_cap:
                wanted[holding] *= allocation.per_asset_cap / total
        out.append(wanted)
    return tuple(out)


def deployed(targets: Sequence[Mapping[Holding, Decimal]]) -> tuple[Decimal, ...]:
    """Return the share of the account at work at each slot."""
    return tuple(sum(slot.values(), start=ZERO) for slot in targets)


def normalise_to(
    targets: Sequence[Mapping[Holding, Decimal]],
    reference: Sequence[Decimal],
) -> tuple[dict[Holding, Decimal], ...]:
    """Return ``targets`` rescaled so each slot deploys what ``reference`` says it should.

    The corrected breadth probe. Holding aggregate exposure equal bar by bar leaves breadth as
    the only thing varying between two runs -- without it, a narrower universe both concentrates
    the book *and* changes how much of the account is at work, and the two effects cannot be told
    apart afterwards.

    A slot with no active signal stays in cash: there is nothing to spread the reference exposure
    across, and inventing a position to match a number would be the opposite of the point. Those
    slots are counted and reported rather than smoothed over.
    """
    out: list[dict[Holding, Decimal]] = []
    for slot, wanted in enumerate(targets):
        total = sum(wanted.values(), start=ZERO)
        want = reference[slot] if slot < len(reference) else ZERO
        if not wanted or total <= ZERO or want <= ZERO:
            out.append({})
            continue
        share = want / Decimal(len(wanted))
        out.append(dict.fromkeys(wanted, share))
    return tuple(out)


# --- Records --------------------------------------------------------------------------------------


class SleeveContribution(DomainModel):
    """What one sleeve, or one market, or one holding added over the whole run."""

    name: Text
    gross_profit: Decimal
    cost: Decimal
    bars_held: int
    episodes: int

    @property
    def net_profit(self) -> Decimal:
        """Return the contribution after the costs of trading it."""
        return self.gross_profit - self.cost


class PortfolioRun(DomainModel):
    """Everything one portfolio simulation produced, before any of it is judged."""

    equity_curve: tuple[EquityPoint, ...]
    by_sleeve: tuple[SleeveContribution, ...]
    by_asset: tuple[SleeveContribution, ...]
    bars: int
    bars_held: int
    turnover: Decimal
    cost_paid: Decimal
    initial_equity: Decimal
    episode_returns: tuple[Decimal, ...]
    """Net return of each closed holding episode, for a profit factor over the portfolio."""

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
        """Return how many bars the portfolio held nothing at all."""
        return self.bars - self.bars_held


@dataclass
class _Book:
    """The mutable state of one portfolio run."""

    equity: Decimal = INITIAL_EQUITY
    actual: dict[Holding, Decimal] = field(default_factory=dict)
    gross: dict[Holding, Decimal] = field(default_factory=dict)
    spent: dict[Holding, Decimal] = field(default_factory=dict)
    bars_of: dict[Holding, int] = field(default_factory=dict)
    counts: dict[Holding, int] = field(default_factory=dict)
    open_profit: dict[Holding, Decimal] = field(default_factory=dict)
    open_cost: dict[Holding, Decimal] = field(default_factory=dict)
    closed: list[Decimal] = field(default_factory=list)
    curve: list[EquityPoint] = field(default_factory=list)
    turnover: Decimal = ZERO
    paid: Decimal = ZERO
    bars: int = 0
    bars_held: int = 0


def simulate_portfolio(
    series: Series,
    targets: Sequence[Mapping[Holding, Decimal]],
    *,
    cost_basis_points: Decimal,
    start: datetime | None = None,
    holdings: Sequence[Holding] | None = None,
) -> PortfolioRun:
    """Run one portfolio over pre-computed target weights and record what it did.

    Args:
        series: One bar series per market.
        targets: Per grid slot, the weight each holding should carry over the following bar.
        cost_basis_points: Cost charged on each side of each weight change.
        start: Ignore slots before this instant, for an out-of-sample window.
        holdings: Every holding the portfolio *declared*, funded or not. Given, attribution
            covers all of them, so a sleeve that never fired is reported at zero rather than
            vanishing -- which matters, because a missing sleeve would make the combined
            portfolio look single-sleeved instead of showing that one component did nothing.

    Returns:
        The run, with no judgement applied to it.
    """
    grid = align(series)
    slots = positions(series, grid)
    rate = cost_basis_points / BASIS
    keys = sorted(
        set(holdings) if holdings is not None else {holding for slot in targets for holding in slot}
    )
    book = _Book(
        gross=dict.fromkeys(keys, ZERO),
        spent=dict.fromkeys(keys, ZERO),
        bars_of=dict.fromkeys(keys, 0),
        counts=dict.fromkeys(keys, 0),
    )
    with localcontext() as ctx:
        ctx.prec = WORKING_PRECISION
        for slot in range(len(grid) - 1):
            if start is not None and grid[slot] < start:
                continue
            _rebalance(book, targets[slot], rate)
            book.bars += 1
            if book.actual:
                book.bars_held += 1
            _hold(book, series, slots, slot)
            book.curve.append(EquityPoint(at=grid[slot + 1], equity=book.equity))
        _close_out(book)
    return _finish(book, keys)


def _rebalance(book: _Book, target: Mapping[Holding, Decimal], rate: Decimal) -> None:
    """Charge the cost of moving to ``target``, opening and closing holding episodes.

    **Sorted, and the sort is load bearing.** Each charge is taken off the equity standing at
    the moment it is levied, so the order the holdings are visited in decides how much of the
    total each one is charged. The equity that comes out is the same either way -- it is the
    same factors multiplied in a different sequence -- which is why the run's return, drawdown,
    turnover and total fees are unaffected. What is affected is attribution: ``spent``,
    ``by_asset``, ``by_sleeve`` and every episode's net result. Iterating a set left that order
    to tuple hashing, so the same evidence measured twice in two processes attributed the same
    costs differently in the sixth significant digit. Sorted, the answer is the answer.
    """
    for holding in sorted(set(book.actual) | set(target)):
        before = book.actual.get(holding, ZERO)
        after = target.get(holding, ZERO)
        if before == after:
            continue
        charge = book.equity * abs(after - before) * rate
        book.turnover += abs(after - before)
        book.paid += charge
        book.equity -= charge
        book.spent[holding] = book.spent.get(holding, ZERO) + charge
        if before == ZERO:
            book.open_profit[holding] = ZERO
            book.open_cost[holding] = charge
        elif after == ZERO:
            _finish_episode(book, holding, charge)
        else:
            book.open_cost[holding] = book.open_cost.get(holding, ZERO) + charge
    book.actual = {holding: weight for holding, weight in target.items() if weight > ZERO}


def _finish_episode(book: _Book, holding: Holding, charge: Decimal) -> None:
    """Close one holding's episode and record its net result."""
    profit = book.open_profit.pop(holding, ZERO)
    cost = book.open_cost.pop(holding, ZERO) + charge
    book.closed.append(profit - cost)
    book.counts[holding] = book.counts.get(holding, 0) + 1


def _hold(
    book: _Book,
    series: Series,
    slots: Mapping[str, Sequence[int | None]],
    slot: int,
) -> None:
    """Apply one bar's market move to every holding and let the weights drift with it."""
    if not book.actual:
        return
    opening = book.equity
    values: dict[Holding, Decimal] = {}
    cash = opening * (ONE - sum(book.actual.values(), start=ZERO))
    for holding, weight in book.actual.items():
        asset = holding[1]
        move = _move(series[asset], slots[asset], slot + 1)
        stake = opening * weight
        profit = ZERO if move is None else stake * move
        values[holding] = stake + profit
        book.gross[holding] = book.gross.get(holding, ZERO) + profit
        book.open_profit[holding] = book.open_profit.get(holding, ZERO) + profit
        book.bars_of[holding] = book.bars_of.get(holding, 0) + 1
    book.equity = sum(values.values(), start=ZERO) + cash
    book.actual = (
        {holding: value / book.equity for holding, value in values.items()}
        if book.equity > ZERO
        else {}
    )


def _close_out(book: _Book) -> None:
    """Mark every still-open episode to the final bar, so nothing goes uncounted."""
    for holding in list(book.open_profit):
        _finish_episode(book, holding, ZERO)


def _finish(book: _Book, keys: Sequence[Holding]) -> PortfolioRun:
    """Aggregate the book into a run, both by sleeve and by market."""

    def gather(index: int) -> tuple[SleeveContribution, ...]:
        names = sorted({holding[index] for holding in keys})
        out: list[SleeveContribution] = []
        for name in names:
            mine = [holding for holding in keys if holding[index] == name]
            out.append(
                SleeveContribution(
                    name=name,
                    gross_profit=sum((book.gross.get(h, ZERO) for h in mine), start=ZERO),
                    cost=sum((book.spent.get(h, ZERO) for h in mine), start=ZERO),
                    bars_held=sum(book.bars_of.get(h, 0) for h in mine),
                    episodes=sum(book.counts.get(h, 0) for h in mine),
                )
            )
        return tuple(out)

    return PortfolioRun(
        equity_curve=tuple(book.curve),
        by_sleeve=gather(0),
        by_asset=gather(1),
        bars=book.bars,
        bars_held=book.bars_held,
        turnover=book.turnover,
        cost_paid=book.paid,
        initial_equity=INITIAL_EQUITY,
        episode_returns=tuple(book.closed),
    )
