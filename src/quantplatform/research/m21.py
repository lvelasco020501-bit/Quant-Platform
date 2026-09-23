"""M21 — an account stop: closing open exposure when the global drawdown cap breaks.

M20 settled what does not work. A cap that only gates entries was crossed on every market
that reached it — 12.49% against a 12% cap, 15.49% against 15% — because the position already
open went on losing while the gate held the door shut. Gating cannot bound a drawdown; only
closing can.

**The mechanism needed nothing new.** ``RiskActionKind.CLOSE`` already exists, ``RiskAction``
carries a free-text reason, and the backtest engine asks the risk engine on every bar what
must happen to open exposure, then authorises the answer *before* any strategy intent and
with the administrative vetoes withdrawn. So the account stop is a research risk engine
returning CLOSE actions tagged ``GLOBAL_DRAWDOWN_FORCED_EXIT``, and the strategy has no say
in it. Nothing in production changed for this milestone.

**Order within the bar**, which is what makes the stop exact: the engine values the account,
updates its breakers, offers the bar to the research engine (M20's fix), and only then asks
what to do with open exposure. The breach is therefore found and acted on within the same
bar, not a decision later.

**The three caps.** 12% and 15% are M20's, kept so the two milestones are comparable; 20% is
production's own total-drawdown limit, carried as the reference rather than as a candidate.
None was chosen by looking at a return, and none may be.

======  =====================================================================================
 key     rule
======  =====================================================================================
 S12     G, plus an account stop at 12%: gate entries **and** close what is open
 S15     the same at 15%
 S20     the same at 20%, production's own limit, for reference
======  =====================================================================================

All three keep G's local recovery exactly — 10% local limit, 30-day cooldown, reference
restarting at the reopening — so the only difference from M20's H12/H15 is that the cap now
closes rather than merely blocks.

**What a backtest cannot prove**, recorded here rather than papered over: that the latch
survives a restart, and that a crash between the decision and the fill leaves no orphan. Both
are properties of the paper and live runtime, and both are stated as production requirements
in the milestone document. What the harness can prove — the close fires on the breach bar,
once per position, cannot be vetoed, pays real costs, and leaves the account flat — is tested.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from quantplatform.research.m17 import POLICIES
from quantplatform.research.recovery import Recovery, RecoveryPolicy

__all__ = ["CAPS", "FIRST_MARKETS", "POLICIES_M21", "THEN_MARKETS"]

CAPS: Final[tuple[Decimal, Decimal, Decimal]] = (
    Decimal("0.12"),
    Decimal("0.15"),
    Decimal("0.20"),
)

FIRST_MARKETS: Final[tuple[str, ...]] = ("XRPUSDT", "ADAUSDT", "BNBUSDT")
"""Where M18 and M20 saw the breaker engage and the ratchet happen."""

THEN_MARKETS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

_G: Final[RecoveryPolicy] = next(p for p in POLICIES if p.key == "G")

POLICIES_M21: Final[tuple[RecoveryPolicy, ...]] = tuple(
    RecoveryPolicy(
        key=f"S{int(cap * 100)}",
        label=f"local reset, account stop at {cap:.0%}",
        drawdown_pct=_G.drawdown_pct,
        recovery=Recovery.COOLDOWN_AND_RESTART,
        cooldown=_G.cooldown,
        global_drawdown_cap=cap,
        close_positions_on_cap=True,
    )
    for cap in CAPS
)
"""S12, S15 and S20. Same local rule as G; the cap is the only thing that differs, and it
now closes what is open instead of only refusing what is new."""
