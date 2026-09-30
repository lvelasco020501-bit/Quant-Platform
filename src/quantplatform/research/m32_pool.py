"""The candidate pool for M32's survivorship correction, declared as data and nothing else.

Separated from :mod:`~quantplatform.research.m32` so this list can be read and argued with on
its own. It is the most contestable object in the milestone: choosing which markets *could* have
been held is itself an act performed in 2026, and no amount of point-in-time machinery downstream
undoes a pool assembled with hindsight.

So the selection rule is written down rather than the selection:

    Every Binance USDT spot market that was at some point among the most significant crypto
    assets by market value, **including every one that later collapsed, was delisted, or faded
    out of relevance**, and whose archives begin early enough to matter.

The direction of the bias that remains is the point. The twenty-two markets added to M30's six
are overwhelmingly the faded and the dead: assets that led the 2017-18 cycle and never returned,
and two that were top-ten and went to approximately zero. A momentum rule ranks by trailing
return, so it is precisely the rule that would have rotated into LUNA in April 2022 and into FTT
before November 2022. **Adding these can only make the candidate's job harder.**

What this pool still cannot fix, said plainly: it contains no market that was liquid on Binance
and has since been removed from the archive entirely, and it contains nothing from outside
Binance. A genuinely unbiased universe would be reconstructed from a point-in-time listing
snapshot, which this project does not have. This is a large correction in the honest direction,
not a proof of its absence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from quantplatform.core.models.base import DomainModel, Text

__all__ = ["ERAS", "POOL", "Era", "PoolMember"]


class Era(StrEnum):
    """Why a market is in the pool. Reporting order is declaration order."""

    INCUMBENT = "incumbent"
    """One of M30's six. Already downloaded and validated by M16."""
    FADED = "faded"
    """Led the 2017-18 cycle by market value and never came back to prominence."""
    COLLAPSED = "collapsed"
    """Was a top-ten asset and went to approximately zero inside the sample."""
    LATER = "later"
    """Became significant after 2020, and so must be absent from the early rankings."""


ERAS: Final[tuple[Era, ...]] = tuple(Era)


class PoolMember(DomainModel):
    """One market in the pool, with why it is here and what is expected of its history."""

    raw: Text
    """Binance's symbol, e.g. ``LTCUSDT``."""
    era: Era
    note: Text
    """What this market is doing in the pool, in one phrase, for the report to quote."""


POOL: Final[tuple[PoolMember, ...]] = (
    # --- M30's six, unchanged and already validated ------------------------------------------
    PoolMember(raw="BTCUSDT", era=Era.INCUMBENT, note="the reference asset throughout"),
    PoolMember(raw="ETHUSDT", era=Era.INCUMBENT, note="second by value for the whole sample"),
    PoolMember(raw="BNBUSDT", era=Era.INCUMBENT, note="the venue's own token"),
    PoolMember(raw="SOLUSDT", era=Era.INCUMBENT, note="lists 2020-09, absent from earlier ranks"),
    PoolMember(raw="ADAUSDT", era=Era.INCUMBENT, note="lists 2018-05"),
    PoolMember(raw="XRPUSDT", era=Era.INCUMBENT, note="lists 2018-06"),
    # --- Led the 2017-18 cycle and faded -----------------------------------------------------
    PoolMember(raw="LTCUSDT", era=Era.FADED, note="top five in 2017, marginal by 2024"),
    PoolMember(raw="BCHUSDT", era=Era.FADED, note="top five in 2017-18 after the fork"),
    PoolMember(raw="EOSUSDT", era=Era.FADED, note="raised the largest ICO of the cycle"),
    PoolMember(raw="TRXUSDT", era=Era.FADED, note="top ten in 2018"),
    PoolMember(raw="XLMUSDT", era=Era.FADED, note="top ten in 2018"),
    PoolMember(raw="NEOUSDT", era=Era.FADED, note="top ten in early 2018"),
    PoolMember(raw="IOTAUSDT", era=Era.FADED, note="top ten in early 2018"),
    PoolMember(raw="DASHUSDT", era=Era.FADED, note="top ten in 2017"),
    PoolMember(raw="ETCUSDT", era=Era.FADED, note="top twenty for most of the sample"),
    PoolMember(raw="XMRUSDT", era=Era.FADED, note="top twenty; privacy delistings expected"),
    PoolMember(raw="ZECUSDT", era=Era.FADED, note="top twenty in 2017; delistings expected"),
    PoolMember(raw="QTUMUSDT", era=Era.FADED, note="top twenty in early 2018"),
    PoolMember(raw="OMGUSDT", era=Era.FADED, note="top twenty in 2018"),
    PoolMember(raw="VETUSDT", era=Era.FADED, note="top twenty in 2018 and 2021"),
    PoolMember(raw="ONTUSDT", era=Era.FADED, note="top twenty in 2018"),
    # --- Went to approximately zero inside the sample ----------------------------------------
    PoolMember(
        raw="LUNAUSDT",
        era=Era.COLLAPSED,
        note="top ten until it went to zero in May 2022; the single most important member",
    ),
    PoolMember(
        raw="FTTUSDT",
        era=Era.COLLAPSED,
        note="the FTX token, strong until November 2022, then worthless",
    ),
    # --- Became significant later ------------------------------------------------------------
    PoolMember(raw="DOGEUSDT", era=Era.LATER, note="lists 2019-07, dominant in 2021"),
    PoolMember(raw="LINKUSDT", era=Era.LATER, note="lists 2019-01"),
    PoolMember(raw="DOTUSDT", era=Era.LATER, note="lists 2020-08"),
    PoolMember(raw="ATOMUSDT", era=Era.LATER, note="lists 2019-04"),
    PoolMember(raw="AVAXUSDT", era=Era.LATER, note="lists 2020-09"),
    PoolMember(raw="MATICUSDT", era=Era.LATER, note="lists 2019-04"),
    PoolMember(raw="UNIUSDT", era=Era.LATER, note="lists 2020-09"),
)
"""Twenty-nine markets: M30's six, fifteen that faded, two that went to zero, and seven that
arrived later and must therefore be absent from the early rankings.

A member whose archives turn out not to exist, or to stop early, is **documented as such and
kept in the record** rather than quietly dropped -- a delisting is data about what was tradeable,
and silently removing it would reintroduce exactly the bias this pool exists to remove."""
