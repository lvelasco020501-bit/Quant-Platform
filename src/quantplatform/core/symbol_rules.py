"""One shared, replaceable view of the venue's trading rules.

:class:`~quantplatform.core.models.market.SymbolRules` is a point-in-time snapshot, and the
risk engine enforces a freshness budget against it. A process that fetches the rules once at
startup and runs for a week therefore stops trading partway through: the rules go stale, and
every intent is refused for a reason that has nothing to do with the market. Raising the
budget would answer a real check with a lie; the honest answer is to fetch again.

That creates the problem this module exists to solve. The portfolio engine, the simulated
broker and the backtest engine each receive the rules at construction, and each used to keep
its own copy. Refreshing one of those copies would leave three components disagreeing about
the tick size — the broker rounding to a lot size the risk engine no longer believes in.

So the rules live in exactly one place. Every component holds a reference to the same store
and reads through it, which makes divergence unrepresentable rather than merely discouraged.

**Nothing is mutated.** ``SymbolRules`` instances are frozen and stay frozen; a refresh
builds a whole new mapping and swaps it in with a single assignment. A reader holding the
store either sees every old value or every new one, never a half-updated mixture, and a
reader that has already taken a snapshot keeps reading consistent values for as long as it
holds it.

**Reading is public; replacing is not.** The store is a read-only ``Mapping``, which is
exactly what every trading component wants and all any of them may do. :meth:`replace` is a
separate, deliberately conspicuous method that only a composition root calls — see
:mod:`quantplatform.orchestration.symbol_rules`, which owns the schedule, and the
architecture test that fails if any trading domain starts calling it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from types import MappingProxyType

from quantplatform.core.errors import ConfigurationError
from quantplatform.core.models.market import SymbolRules

__all__ = ["SymbolRulesStore", "as_symbol_rules_store"]


class SymbolRulesStore(Mapping[str, SymbolRules]):
    """The current venue rules for every traded symbol.

    Behaves as an ordinary read-only mapping, so it drops straight into everything that
    already accepts ``Mapping[str, SymbolRules]``. The difference is that its contents can
    be replaced wholesale by whoever holds the store, and every holder sees the replacement
    at once.

    Single-threaded by design, in a platform that is single-threaded by design: the swap is
    one attribute assignment, so even under a thread a reader would see one complete
    mapping or the other. What is *not* offered is a lock — there is no scheduler here, and
    inventing one would add concurrency to a system whose reproducibility depends on not
    having any.
    """

    __slots__ = ("_current",)

    def __init__(self, rules: Mapping[str, SymbolRules]) -> None:
        """Seed the store with the rules known at startup.

        Args:
            rules: Venue rules per canonical symbol. Copied, so a caller that keeps and
                mutates its own dictionary cannot alter what the platform trades against.
        """
        self._current: Mapping[str, SymbolRules] = MappingProxyType(dict(rules))

    # --- Mapping ----------------------------------------------------------------------------

    def __getitem__(self, symbol: str) -> SymbolRules:
        """Return the current rules for ``symbol``."""
        return self._current[symbol]

    def __iter__(self) -> Iterator[str]:
        """Iterate the symbols the store knows about."""
        return iter(self._current)

    def __len__(self) -> int:
        """Return how many symbols the store knows about."""
        return len(self._current)

    def __repr__(self) -> str:
        """Return a representation naming the symbols rather than dumping every rule."""
        return f"SymbolRulesStore(symbols={sorted(self._current)!r})"

    # --- Reading ----------------------------------------------------------------------------

    def current(self, symbol: str) -> SymbolRules:
        """Return the rules in force for ``symbol`` right now.

        The named accessor exists so a reader's intent is legible: ``store.current(symbol)``
        says *whatever is true at this instant*, where a bare subscript reads like a lookup
        in a fixed table.

        Args:
            symbol: Canonical symbol.

        Returns:
            The current immutable rules.

        Raises:
            ConfigurationError: If the symbol was never registered. A wiring mistake rather
                than a market condition, so it fails loudly instead of returning ``None``
                for a caller to trip over later.
        """
        rules = self._current.get(symbol)
        if rules is None:
            raise ConfigurationError(
                "no venue trading rules are registered for this symbol",
                symbol=symbol,
                known=sorted(self._current),
            )
        return rules

    def snapshot(self) -> Mapping[str, SymbolRules]:
        """Return the current mapping as an immutable view.

        Useful to a caller that wants a stable set of rules across several reads — a
        refresh between two reads of the store would otherwise be visible mid-calculation.
        """
        return self._current

    @property
    def oldest_updated_at(self) -> datetime | None:
        """Return the fetch time of the *least* recently updated symbol.

        The oldest rather than the newest, because staleness is judged per symbol and the
        worst one is what decides whether the store as a whole still describes the venue.

        Returns:
            The earliest ``updated_at`` among the stored rules, or ``None`` when empty.
        """
        if not self._current:
            return None
        return min(rules.updated_at for rules in self._current.values())

    def age_seconds(self, now: datetime) -> float:
        """Return how stale the oldest stored rules are at ``now``.

        Args:
            now: The current instant, read from an injected clock by the caller. The store
                never reads a wall clock of its own.

        Returns:
            Age in seconds, or ``0.0`` when the store is empty — nothing stored cannot be
            stale, and reporting a large age for an empty store would raise an alarm about
            rules nobody is trading against.
        """
        oldest = self.oldest_updated_at
        if oldest is None:
            return 0.0
        return (now - oldest).total_seconds()

    # --- Replacing --------------------------------------------------------------------------

    def replace(self, rules: Mapping[str, SymbolRules]) -> tuple[str, ...]:
        """Swap in a freshly fetched set of rules, atomically.

        The whole mapping is built and validated before anything is published, so a
        replacement that is going to be refused never becomes half-visible.

        Args:
            rules: The newly fetched rules. Must cover every symbol already known.

        Returns:
            The symbols whose rules actually changed, in sorted order. Empty means the venue
            published the same limits again, which is the ordinary case and worth
            distinguishing from a change nobody noticed.

        Raises:
            ConfigurationError: If the replacement omits a symbol the store already holds.
                Dropping a symbol mid-run would leave the broker unable to price it, turning
                a refresh into an outage; refusing keeps the last known-good rules in place.
        """
        replacement = dict(rules)
        missing = sorted(set(self._current) - set(replacement))
        if missing:
            raise ConfigurationError(
                "refreshed venue rules do not cover every symbol already in use; keeping "
                "the previous rules rather than leaving a traded symbol undefined",
                symbols=missing,
            )
        changed = tuple(
            sorted(
                symbol
                for symbol, updated in replacement.items()
                if _limits_differ(self._current.get(symbol), updated)
            )
        )
        self._current = MappingProxyType(replacement)
        return changed


_COMPARED_LIMITS: tuple[str, ...] = (
    "price_tick",
    "quantity_step",
    "min_quantity",
    "max_quantity",
    "min_notional",
    "max_notional",
)
"""The fields whose change alters what the venue will accept.

``updated_at`` and ``source`` are deliberately absent: they move on every fetch, and
treating a re-fetch of identical limits as a change would report a venue rule change every
few hours and train an operator to ignore the signal.
"""


def _limits_differ(previous: SymbolRules | None, updated: SymbolRules) -> bool:
    """Return whether two rule snapshots impose different constraints."""
    if previous is None:
        return True
    return any(getattr(previous, field) != getattr(updated, field) for field in _COMPARED_LIMITS)


def as_symbol_rules_store(rules: Mapping[str, SymbolRules]) -> SymbolRulesStore:
    """Return a live store for ``rules``, adopting one that already is.

    The normalisation every component that holds venue rules performs at construction. A
    store passed in is kept *by reference*, which is what lets a later refresh reach that
    component; anything else is copied into a new store, which gives exactly the protection
    the old ``dict(symbols)`` copy gave against a caller mutating what it handed over.

    Args:
        rules: Either a shared store or a plain mapping.

    Returns:
        A store the caller may read for the lifetime of the run.
    """
    return rules if isinstance(rules, SymbolRulesStore) else SymbolRulesStore(rules)
