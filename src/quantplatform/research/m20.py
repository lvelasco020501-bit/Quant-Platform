"""M20 — per-bar detection after a reset, and a global cap set where it can bind.

M19 ended with two concrete defects, both of them mine rather than the data's:

* **The breaker acted late after a restart.** The first halt came from the engine's own
  breaker, evaluated on every bar; every later halt was found by a wrapper that could only
  look when the strategy asked to trade. A single halt let the loss run to **17.17% against a
  10% limit**.
* **The budgets never bound.** The cap sat at 20% while the worst drawdown reached 19.16%,
  and the reset allowance counted a rolling year while the chains it was meant to stop ran
  over three and four years. All 48 runs reproduced G exactly.

Both are fixed here, and the fixes are independent.

**The per-bar fix.** :class:`~quantplatform.research.recovery.RecoveryLatchRiskEngine` now
defines ``observe_bar``, which the backtest engine offers the same marked snapshot its own
breaker reads, on every bar. Detection before and after a reset is now the identical
arithmetic on the identical data, so an overshoot can only be one bar's move rather than
whatever the gap between two decisions allowed.

**The policies, and why these four only.**

======  ====================================================================================
 key     rule
======  ====================================================================================
 G       cooldown 30 days, local reset. The reference, unchanged from M17
 H12     G, plus a cap of 12% on the loss from the original peak
 H15     G, plus a cap of 15%
 J       G, plus the 15% cap **and** at most two local resets per rolling three years
======  ====================================================================================

* **12% and 15% straddle the damage actually observed.** M18's ratchets reached 19.16% and
  19.09%, with chains giving up 17.27% and 14.34%. A cap has to sit below that to be tested
  at all, which is exactly what 20% failed to do. Two values rather than one because a
  single number would say only "bound" or "did not"; a pair says where the mechanism starts
  to bite. Neither was chosen by looking at a return, and neither may be.
* **J pairs the allowance with the *looser* cap, on purpose.** With the 12% cap the cap would
  bind first and the allowance would once again never be exercised — M19's mistake repeated.
  At 15% there is room for the allowance to be the binding constraint, so what it adds can
  be seen.
* **The allowance counts three years, not one.** M18's chains spanned 3.5 and 4.3 years.

**Order of work, and the reason for it.** XRP and ADA first, because those are the only two
markets where a chain of restarts ever occurred; the other four are run only if the fix holds
there. A policy that cannot contain the ratchet where the ratchet happens is not worth six
markets of compute.

**The gates** are M19's, unchanged, including the requirement added after those runs that a
budget must actually have bound for its result to count as evidence. Return is not an input.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Final

from quantplatform.research.m17 import POLICIES
from quantplatform.research.recovery import Recovery, RecoveryPolicy

__all__ = [
    "CAPS",
    "FIRST_MARKETS",
    "POLICIES_M20",
    "RESET_ALLOWANCE",
    "RESET_WINDOW",
    "THEN_MARKETS",
]

CAPS: Final[tuple[Decimal, Decimal]] = (Decimal("0.12"), Decimal("0.15"))
RESET_ALLOWANCE: Final[int] = 2
RESET_WINDOW: Final[timedelta] = timedelta(days=3 * 365)
"""Three years: the span M18's reset chains actually covered, which a year could not see."""

FIRST_MARKETS: Final[tuple[str, ...]] = ("XRPUSDT", "ADAUSDT")
"""The only two markets where a chain of restarts ever happened."""

THEN_MARKETS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
"""Run only if the fix holds on the two that matter."""

_G: Final[RecoveryPolicy] = next(p for p in POLICIES if p.key == "G")

POLICIES_M20: Final[tuple[RecoveryPolicy, ...]] = (
    _G,
    RecoveryPolicy(
        key="H12",
        label="local reset under a 12% global cap",
        drawdown_pct=_G.drawdown_pct,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=_G.cooldown,
        global_drawdown_cap=CAPS[0],
    ),
    RecoveryPolicy(
        key="H15",
        label="local reset under a 15% global cap",
        drawdown_pct=_G.drawdown_pct,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=_G.cooldown,
        global_drawdown_cap=CAPS[1],
    ),
    RecoveryPolicy(
        key="J",
        label="local reset under a 15% cap and two resets per three years",
        drawdown_pct=_G.drawdown_pct,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=_G.cooldown,
        global_drawdown_cap=CAPS[1],
        max_resets_per_year=RESET_ALLOWANCE,
        reset_window=RESET_WINDOW,
    ),
)
"""G is the reference the three budgeted rules have to improve on; all four share its local
limit and cooldown exactly, so every difference is the budget and the per-bar fix."""
