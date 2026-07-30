"""Market data acquisition, integrity validation, normalisation and ingestion.

Phase 2 implements the historical slice: read a canonical CSV file
(:mod:`~quantplatform.data.csv_loader`), validate every record and the dataset as a whole
(:mod:`~quantplatform.data.validation`), normalise survivors into
:class:`~quantplatform.core.models.market.MarketBar`
(:mod:`~quantplatform.data.normalization`), and persist them with full provenance
(:mod:`~quantplatform.data.ingestion`).

The layer only ever *reports* what it finds; it never repairs. Missing bars are not
synthesised, prices are not interpolated, out-of-order input is detected before any sorting
happens, and a candle that conflicts with one already stored is recorded rather than
overwritten. Real-time and exchange-backed sources are out of scope for this phase.
"""
