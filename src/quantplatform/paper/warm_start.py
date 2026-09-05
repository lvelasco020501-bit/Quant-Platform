"""Deciding whether a persisted market history may be used, and refusing when it may not.

Every rule here is a refusal. That is the shape the problem has: a history that is right
lets a session skip its warm-up, and a history that is subtly wrong lets it trade on
indicators computed from candles that are not the ones it thinks. The second failure is
silent, which is why none of these checks are advisory.

The rule that is not about data quality is the one that matters most. **A session whose
snapshot carries financial state cannot have its market history reused.** Not because the
candles are wrong — they are fine — but because starting a fresh session from them presents
an unreconciled account as a recovery: the old session's books were never closed, its
position is orphaned in a snapshot nobody looked at, and an operator seeing a warm, healthy
new session may reasonably believe the system recovered. It did not. Somebody has to close
those books, and this refusal is what makes them.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantplatform.core.enums import MarketType, Timeframe
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.warm_start import MarketHistory

__all__ = ["WarmStartDecision", "evaluate_warm_start"]


@dataclass(frozen=True)
class WarmStartDecision:
    """Whether market context may be restored, and why."""

    history: MarketHistory | None
    """The history to apply, or ``None`` when none will be."""

    reason: str
    """Plain language, always populated. On a refusal it names the condition and its values,
    because an operator reading "warm-start refused" and nothing else has to open a shell to
    learn anything at all."""

    refused: bool = False
    """``True`` only when a history existed and was rejected. A deployment with no history
    at all is not a refusal — it is an ordinary cold start."""

    @property
    def applied(self) -> bool:
        """Return whether market context will be restored."""
        return self.history is not None


def evaluate_warm_start(  # noqa: PLR0911 - each return is one named refusal
    history: MarketHistory | None,
    *,
    source_state: PaperSessionState | None,
    symbol: str,
    market_type: MarketType,
    timeframe: Timeframe,
    required_history: int,
) -> WarmStartDecision:
    """Decide whether a persisted history may seed this session's market context.

    Args:
        history: What was loaded, already structurally validated, or ``None``.
        source_state: The snapshot of the session that wrote the history. ``None`` when no
            snapshot exists, which is refused: absence is not proof of innocence.
        symbol: The instrument this session is configured for.
        market_type: The market this session is configured for.
        timeframe: The candle interval this session is configured for.
        required_history: Candles the strategy declares it needs before it may signal.

    Returns:
        The decision, carrying the history when it may be used and the reason either way.
    """
    if history is None:
        return WarmStartDecision(
            history=None,
            reason="no persisted market history was found; starting cold, which is normal "
            "for a session that has never run",
        )

    if source_state is None:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                f"the session that wrote this history ({history.manifest.source_session_id!r}) "
                "has no snapshot, so there is no way to show it was financially clean. "
                "Absence of evidence is refused rather than assumed"
            ),
        )

    carried = source_state.financial_state_carried
    if carried:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                f"the session that wrote this history ({history.manifest.source_session_id!r}) "
                f"carries financial state ({', '.join(carried)}). Starting fresh from its "
                "candles would present an unreconciled account as a recovery: those books "
                "are still open and need closing by a person, not by a restart"
            ),
        )

    manifest = history.manifest
    if manifest.symbol != symbol:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                f"the history describes {manifest.symbol!r} but this session trades "
                f"{symbol!r}; candles from another instrument would seed every indicator "
                "with the wrong prices"
            ),
        )
    if manifest.market_type is not market_type:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                f"the history describes the {manifest.market_type.value} market but this "
                f"session trades {market_type.value}"
            ),
        )
    if manifest.timeframe is not timeframe:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                f"the history holds {manifest.timeframe.value} candles but this session "
                f"trades {timeframe.value}; a window of the wrong interval is a different "
                "indicator wearing the same name"
            ),
        )

    if source_state.bars_processed != history.bars_count:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                f"the snapshot reports {source_state.bars_processed} processed candles but "
                f"the history holds {history.bars_count}; the two artefacts disagree about "
                "what the session saw, and the shorter one cannot be assumed correct"
            ),
        )
    last_bar = source_state.last_bar
    if last_bar is not None and last_bar.close_time != history.last_bar_close_time:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                "the history does not end at the candle the snapshot names as its last; the "
                "two artefacts describe different points in time"
            ),
        )

    if history.bars_count < required_history:
        return WarmStartDecision(
            history=None,
            refused=True,
            reason=(
                f"the history holds {history.bars_count} candles but the strategy needs "
                f"{required_history} before it may signal. A partial warm-start is refused "
                "rather than applied: it would leave the session believing it was ready"
            ),
        )

    return WarmStartDecision(
        history=history,
        reason=(
            f"restored {history.bars_count} candles of {manifest.symbol} "
            f"{manifest.timeframe.value} from session "
            f"{manifest.source_session_id!r}, which carried no financial state"
        ),
    )
