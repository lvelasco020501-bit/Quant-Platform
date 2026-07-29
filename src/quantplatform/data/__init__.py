"""Market data acquisition, integrity validation, normalisation and repositories.

Responsibilities: retrieve bars from providers, detect missing, duplicated, stale and
out-of-order records, normalise them into :class:`~quantplatform.core.models.market.MarketBar`
and persist both the source and the normalised form. Implemented in phase 2.
"""
