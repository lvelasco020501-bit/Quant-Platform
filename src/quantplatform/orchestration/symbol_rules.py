"""Keeping the venue's trading rules current for the length of a run.

The risk engine refuses to size an order against rules older than its freshness budget —
twenty-four hours by default. That check is correct and stays exactly as it is: rules a day
old may describe limits the venue has since changed, and trading on them is guessing. What
was missing is the other half of the arrangement. A process that fetched once at startup and
ran for a week spent six of those days having every intent refused, not because anything was
wrong with the market but because nobody re-read the rulebook.

This module re-reads it. The schedule lives here and nowhere else: not in the strategy, which
must not know that venues have rulebooks; not in the risk engine, which enforces the budget
and would be marking its own homework if it also refreshed what it measures; not in the
broker, which is downstream of the decision. A composition root owns the loop, and everything
below reads the store.

**Refresh cannot rescue a failing venue, and does not try.** If the metadata endpoint is
unreachable the previous rules stay in force and the failure is counted. Nothing here marks
old rules fresh, extends the budget, or waves an intent through — if the outage lasts past
the freshness budget the risk engine refuses every order exactly as it does today, which is
the correct outcome and the one an operator needs to see coming.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from quantplatform.core.clock import Clock
from quantplatform.core.errors import ConfigurationError, QuantPlatformError
from quantplatform.core.interfaces import SymbolRulesProvider
from quantplatform.core.logging_config import get_logger
from quantplatform.core.models.market import SymbolRules
from quantplatform.core.models.orders import Order
from quantplatform.core.models.telemetry import SymbolRulesTelemetry
from quantplatform.core.symbol_rules import SymbolRulesStore

__all__ = ["SymbolRulesRefresher"]

_LOGGER = get_logger(__name__)


class SymbolRulesRefresher:
    """Re-fetches venue trading rules on a schedule and publishes them atomically.

    Satisfies :class:`~quantplatform.core.interfaces.SymbolRulesMaintainer`, so the paper
    runner can drive it without knowing any of this.
    """

    def __init__(
        self,
        *,
        store: SymbolRulesStore,
        provider: SymbolRulesProvider,
        clock: Clock,
        refresh_interval_seconds: float,
        stale_after_seconds: int,
        symbols: Sequence[str] | None = None,
        open_orders: Callable[[], Sequence[Order]] | None = None,
    ) -> None:
        """Wire a refresher.

        Args:
            store: The one store every trading component reads. Replaced in place, by
                reference, which is what makes a refresh visible to all of them at once.
            provider: Read-only source of venue metadata.
            clock: Injected time source. Nothing here reads a wall clock, which is what lets
                a test cross seven days in milliseconds.
            refresh_interval_seconds: How old the rules may get before a refresh is due.
            stale_after_seconds: The risk engine's freshness budget, carried into telemetry
                so a report can grade staleness without being configured with the number a
                second time.
            symbols: Symbols to fetch. Defaults to whatever the store already holds, which
                is the right answer in every deployment and spares the caller repeating it.
            open_orders: Optional source of currently working orders, consulted only after
                the venue's limits actually change. Read, never written: this class reports
                that a working order no longer satisfies the rules and stops there.

        Raises:
            ConfigurationError: If the schedule could not keep the rules fresh — see
                :meth:`_validate_schedule` for the rule and why it is fatal rather than
                clamped.
        """
        self._store = store
        self._provider = provider
        self._clock = clock
        self._interval = float(refresh_interval_seconds)
        self._stale_after = int(stale_after_seconds)
        self._symbols = tuple(symbols) if symbols is not None else tuple(sorted(store))
        self._open_orders = open_orders

        self._validate_schedule()

        self._attempts = 0
        self._successes = 0
        self._failures = 0
        self._consecutive_failures = 0
        self._rule_changes = 0
        self._working_order_conflicts = 0
        self._last_refresh_at: datetime | None = store.oldest_updated_at
        self._last_failure_reason: str | None = None

    def _validate_schedule(self) -> None:
        """Refuse a schedule that cannot keep the rules inside the freshness budget.

        The rule is ``0 < refresh_interval < stale_threshold``, and every part of it earns
        its place. A non-positive interval would refetch on every bar, hammering a public
        endpoint until it throttles. An interval at or above the budget guarantees the very
        outage this module exists to prevent: rules would reach the risk engine's limit
        before the next refresh was even due, and the run would stop trading on schedule.

        Fatal rather than silently clamped. A deployment configured to refresh once a week
        has a mistaken belief about how the platform behaves, and quietly correcting it to
        six hours would leave that belief in place to cause the next surprise.

        Raises:
            ConfigurationError: If the interval is not strictly between zero and the
                freshness budget.
        """
        if self._interval <= 0:
            raise ConfigurationError(
                "the symbol rules refresh interval must be strictly positive",
                refresh_interval_seconds=self._interval,
            )
        if self._stale_after <= 0:
            raise ConfigurationError(
                "the symbol rules staleness budget must be strictly positive for refresh "
                "scheduling to mean anything",
                stale_after_seconds=self._stale_after,
            )
        if self._interval >= self._stale_after:
            raise ConfigurationError(
                "the symbol rules refresh interval must be strictly below the staleness "
                "budget, or the rules expire before the next refresh is due and the risk "
                "engine refuses every order until one lands",
                refresh_interval_seconds=self._interval,
                stale_after_seconds=self._stale_after,
            )

    # --- The port ---------------------------------------------------------------------------

    def maintain(self) -> SymbolRulesTelemetry:
        """Refresh if due, then report. Never raises.

        Returns:
            The current reading, taken after any refresh this call performed so the age it
            reports is the age of the rules now in force.
        """
        if self.is_due():
            self.refresh()
        return self.telemetry()

    def is_due(self) -> bool:
        """Return whether the stored rules have reached the refresh interval."""
        return self._store.age_seconds(self._clock.now()) >= self._interval

    def refresh(self) -> bool:
        """Fetch and publish a new set of rules, keeping the old ones if anything fails.

        Every failure mode is treated identically and deliberately so: an unreachable
        endpoint, a throttled request, a malformed document and a symbol the venue has
        delisted all mean the same thing operationally — *the rules could not be replaced* —
        and the response to all of them is to keep what is already known to be good.

        Returns:
            Whether the stored rules were replaced.
        """
        self._attempts += 1
        try:
            fetched = self._provider.fetch(self._symbols)
            changed = self._store.replace(dict(fetched))
        except (QuantPlatformError, OSError, ValueError) as exc:
            self._record_failure(exc)
            return False
        self._record_success(changed)
        return True

    def telemetry(self) -> SymbolRulesTelemetry:
        """Return an immutable reading of the refresh mechanism and the rules' age."""
        return SymbolRulesTelemetry(
            refresh_attempts=self._attempts,
            refresh_successes=self._successes,
            refresh_failures=self._failures,
            consecutive_failures=self._consecutive_failures,
            rule_changes=self._rule_changes,
            working_order_conflicts=self._working_order_conflicts,
            last_refresh_at=self._last_refresh_at,
            last_failure_reason=self._last_failure_reason,
            age_seconds=self._store.age_seconds(self._clock.now()),
            stale_after_seconds=self._stale_after,
        )

    # --- Bookkeeping ------------------------------------------------------------------------

    def _record_success(self, changed: Sequence[str]) -> None:
        """Count a successful refresh and note anything the venue changed."""
        self._successes += 1
        self._consecutive_failures = 0
        self._last_failure_reason = None
        self._last_refresh_at = self._clock.now()
        if not changed:
            return
        self._rule_changes += 1
        _LOGGER.warning(
            "venue trading rules changed",
            extra={"symbols": list(changed)},
        )
        self._report_working_order_conflicts(changed)

    def _record_failure(self, exc: Exception) -> None:
        """Count a failed refresh, leaving the previous rules and their age untouched.

        The age is left alone on purpose. Marking rules fresh because an attempt was made
        would hide exactly the condition that matters, and the risk engine would keep
        trading on rules nobody has actually re-read.
        """
        self._failures += 1
        self._consecutive_failures += 1
        self._last_failure_reason = f"{type(exc).__name__}: {exc}"
        _LOGGER.warning(
            "symbol rules refresh failed; keeping the last known-good rules",
            extra={
                "error": type(exc).__name__,
                "consecutive_failures": self._consecutive_failures,
                "age_seconds": self._store.age_seconds(self._clock.now()),
            },
        )

    def _report_working_order_conflicts(self, changed: Sequence[str]) -> None:
        """Note working orders that the venue's new limits would no longer accept.

        Reported, never repaired. Amending or replacing a live order is an execution
        decision with real consequences — a cancel-and-resubmit at a changed tick size is a
        new order at a new price — and making it silently, from a metadata refresh, is not
        something an operator could have anticipated or audited. So the condition is
        surfaced and left for a human, which is the whole of what this phase promises.
        """
        if self._open_orders is None:
            return
        affected = set(changed)
        try:
            orders = tuple(self._open_orders())
        except Exception:
            _LOGGER.warning("could not read working orders after a venue rule change")
            return
        for order in orders:
            if order.symbol not in affected:
                continue
            violations = _violations(order, self._store.current(order.symbol))
            if not violations:
                continue
            self._working_order_conflicts += 1
            _LOGGER.warning(
                "a working order no longer satisfies the venue's refreshed rules; it has "
                "been left exactly as it is",
                extra={
                    "order_id": str(order.order_id),
                    "symbol": order.symbol,
                    "violations": violations,
                },
            )


def _violations(order: Order, rules: SymbolRules) -> list[str]:
    """Return the ways a working order breaches a set of rules."""
    violations: list[str] = []
    quantity = order.quantity
    if quantity < rules.min_quantity:
        violations.append("quantity below the minimum")
    if rules.max_quantity is not None and quantity > rules.max_quantity:
        violations.append("quantity above the maximum")
    if rules.quantity_step > 0 and quantity % rules.quantity_step != 0:
        violations.append("quantity is not a multiple of the step size")
    price = order.limit_price
    if price is not None and rules.price_tick > 0 and price % rules.price_tick != 0:
        violations.append("limit price is not a multiple of the tick size")
    return violations
