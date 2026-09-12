"""Indicators for the M13 research families: bounded, pure, and named by a small grammar.

Each is a pure function of the closed bars it is handed, like every other pipeline here, with
one addition that matters on a year of hourly data: the window is **bounded**. The engine
hands a pipeline the entire history on every bar, and an indicator that re-read all of it
would turn a linear backtest quadratic. Every name declares how many bars it needs, the
pipeline slices exactly that many off the end, and nothing older is read.

Names are a grammar rather than one class per indicator, so a strategy's declared features
are also this pipeline's configuration and there is no second list to keep in step:

=====================  ====================================================  ===========
name                   value                                                 bars read
=====================  ====================================================  ===========
``sma_<n>``            mean of the last n closes, current bar included       n
``roc_<n>``            close / close n bars earlier - 1                      n + 1
``stdev_<n>``          population standard deviation of the last n closes    n
``zscore_<n>``         (close - sma_n) / stdev_n                             n
``zscore_prev_<n>``    the same, on the window ending one bar earlier        n + 1
``rvol_<n>``           population std. dev. of the last n one-bar returns    n + 1
``rsi_<n>``            simple-average RSI over the last n changes            n + 1
``er_<n>``             Kaufman efficiency ratio over the last n changes      n + 1
``emab_<n>``           EMA(n), seeded 5n bars back                           5n
``emaslope_<n>_<k>``   relative change of that EMA over its last k steps     5n + k
``volratio_<s>_<l>``   rvol_s / rvol_l                                       l + 1
=====================  ====================================================  ===========

A window too short, or a ratio whose denominator is zero, omits the name rather than
inventing a value — the rule the rest of this package already follows.

**The bounded EMA is an approximation, and named as one.** The existing ``ema_<n>`` recurses
from the first bar of the run; ``emab_<n>`` recurses from a seed 5n bars back, so after 4n
smoothing steps the seed's remaining weight is ``(1 - 2/(n+1))^(4n)``, roughly e^-8. It has a
different name on purpose: the benchmark's full-history EMA and this one are not the same
number, and one name for both would let a comparison quietly mix them.

**RSI is the simple-average form**, not Wilder's recursive one, for the same bounded-window
reason. Stated so that nobody compares it against a charting package and suspects a bug.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from itertools import pairwise
from typing import Final

from quantplatform.core.constants import DECIMAL_WORKING_PRECISION, ZERO
from quantplatform.core.errors import ConfigurationError
from quantplatform.core.models.market import MarketBar

__all__ = ["IndicatorFeatures"]

_GRAMMAR: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?P<single>sma|roc|stdev|zscore_prev|zscore|rvol|rsi|er|emab)_(?P<n>[1-9][0-9]*)"
    r"|(?P<pair>emaslope|volratio)_(?P<a>[1-9][0-9]*)_(?P<b>[1-9][0-9]*))$"
)
_EMA_SEED_MULTIPLE: Final[int] = 5
_ONE_BAR_MORE: Final[frozenset[str]] = frozenset({"roc", "zscore_prev", "rvol", "rsi", "er"})
_HUNDRED: Final[Decimal] = Decimal(100)
_MIDPOINT: Final[Decimal] = Decimal(50)


@dataclass(frozen=True, slots=True)
class _Spec:
    """One parsed feature name."""

    name: str
    kind: str
    first: int
    second: int = 0

    @property
    def history(self) -> int:
        """Return how many bars, ending at the current one, this feature reads."""
        if self.kind == "emab":
            return _EMA_SEED_MULTIPLE * self.first
        if self.kind == "emaslope":
            return _EMA_SEED_MULTIPLE * self.first + self.second
        if self.kind == "volratio":
            return self.second + 1
        if self.kind in _ONE_BAR_MORE:
            return self.first + 1
        return self.first


def _parse(name: str) -> _Spec | None:
    """Return the parsed form of a name this pipeline computes, or ``None``."""
    match = _GRAMMAR.match(name)
    if match is None:
        return None
    if match.group("single") is not None:
        return _Spec(name=name, kind=match.group("single"), first=int(match.group("n")))
    return _Spec(
        name=name,
        kind=match.group("pair"),
        first=int(match.group("a")),
        second=int(match.group("b")),
    )


class IndicatorFeatures:
    """Computes the research indicators a strategy names, over a bounded window."""

    def __init__(self, names: Sequence[str]) -> None:
        """Parse and validate the requested names.

        Args:
            names: Feature names in the grammar documented on this module.

        Raises:
            ConfigurationError: If no name was given, a name is not one this pipeline
                computes, or a volatility ratio does not compare a shorter window to a longer.
        """
        if not names:
            raise ConfigurationError("an indicator pipeline needs at least one feature name")
        specs: list[_Spec] = []
        for name in sorted(set(names)):
            spec = _parse(name)
            if spec is None:
                raise ConfigurationError("not an indicator this pipeline computes", name=name)
            if spec.kind == "volratio" and spec.first >= spec.second:
                raise ConfigurationError(
                    "a volatility ratio compares a shorter window to a longer one", name=name
                )
            specs.append(spec)
        self._specs = tuple(specs)
        self._history = max(spec.history for spec in specs)

    @staticmethod
    def handles(name: str) -> bool:
        """Return whether ``name`` is in this pipeline's grammar."""
        return _parse(name) is not None

    @property
    def feature_names(self) -> Sequence[str]:
        """Return the configured names, sorted. Never ``close``: another pipeline owns it."""
        return tuple(spec.name for spec in self._specs)

    @property
    def required_history(self) -> int:
        """Return the deepest window any configured name reads."""
        return self._history

    def compute(self, bars: Sequence[MarketBar]) -> Mapping[str, Decimal]:
        """Return every configured feature the window can support.

        Args:
            bars: Closed bars in ascending open-time order; only the last
                :attr:`required_history` of them are read.

        Returns:
            One entry per name whose window fits and whose value is defined.
        """
        if not bars:
            return {}
        closes = [bar.close for bar in bars[-self._history :]]
        features: dict[str, Decimal] = {}
        with localcontext() as ctx:
            ctx.prec = DECIMAL_WORKING_PRECISION
            for spec in self._specs:
                if len(closes) < spec.history:
                    continue
                value = _EVALUATORS[spec.kind](closes, spec.first, spec.second)
                if value is not None:
                    features[spec.name] = value
        return features


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, start=ZERO) / Decimal(len(values))


def _deviation(values: Sequence[Decimal]) -> Decimal:
    mean = _mean(values)
    return (
        sum(((value - mean) ** 2 for value in values), start=ZERO) / Decimal(len(values))
    ).sqrt()


def _change(earlier: Decimal, later: Decimal) -> Decimal | None:
    return None if earlier == ZERO else later / earlier - 1


def _zscore(window: Sequence[Decimal]) -> Decimal | None:
    deviation = _deviation(window)
    if deviation == ZERO:
        return None
    return (window[-1] - _mean(window)) / deviation


def _realised_volatility(closes: Sequence[Decimal]) -> Decimal:
    return _deviation([later / earlier - 1 for earlier, later in pairwise(closes)])


def _rsi(closes: Sequence[Decimal]) -> Decimal:
    changes = [later - earlier for earlier, later in pairwise(closes)]
    gain = sum((change for change in changes if change > 0), start=ZERO) / Decimal(len(changes))
    loss = sum((-change for change in changes if change < 0), start=ZERO) / Decimal(len(changes))
    if loss == ZERO:
        return _HUNDRED if gain > ZERO else _MIDPOINT
    return _HUNDRED - _HUNDRED / (1 + gain / loss)


def _efficiency(closes: Sequence[Decimal]) -> Decimal:
    path = sum((abs(later - earlier) for earlier, later in pairwise(closes)), start=ZERO)
    if path == ZERO:
        return ZERO
    return abs(closes[-1] - closes[0]) / path


def _ema_series(closes: Sequence[Decimal], period: int) -> list[Decimal]:
    smoothing = Decimal(2) / Decimal(period + 1)
    average = _mean(closes[:period])
    series = [average]
    for close in closes[period:]:
        average = (close - average) * smoothing + average
        series.append(average)
    return series


def _ema_slope(closes: Sequence[Decimal], period: int, steps: int) -> Decimal | None:
    series = _ema_series(closes, period)
    return _change(series[-1 - steps], series[-1])


def _volatility_ratio(closes: Sequence[Decimal], short: int, long_: int) -> Decimal | None:
    denominator = _realised_volatility(closes[-long_ - 1 :])
    if denominator == ZERO:
        return None
    return _realised_volatility(closes[-short - 1 :]) / denominator


_EVALUATORS: Final[Mapping[str, Callable[[Sequence[Decimal], int, int], Decimal | None]]] = {
    "sma": lambda closes, n, _: _mean(closes[-n:]),
    "roc": lambda closes, n, _: _change(closes[-n - 1], closes[-1]),
    "stdev": lambda closes, n, _: _deviation(closes[-n:]),
    "zscore": lambda closes, n, _: _zscore(closes[-n:]),
    "zscore_prev": lambda closes, n, _: _zscore(closes[-n - 1 : -1]),
    "rvol": lambda closes, n, _: _realised_volatility(closes[-n - 1 :]),
    "rsi": lambda closes, n, _: _rsi(closes[-n - 1 :]),
    "er": lambda closes, n, _: _efficiency(closes[-n - 1 :]),
    "emab": lambda closes, n, _: _ema_series(closes[-_EMA_SEED_MULTIPLE * n :], n)[-1],
    "emaslope": lambda closes, n, k: _ema_slope(closes[-(_EMA_SEED_MULTIPLE * n + k) :], n, k),
    "volratio": _volatility_ratio,
}
"""One evaluator per kind. Each reads only the tail of ``closes`` its spec declared."""
