"""Persistence: SQLAlchemy models, repositories and Alembic migrations.

Every artefact along the decision path is persisted here so that market data, features,
signals, intents, risk decisions, orders, fills and portfolio snapshots remain traceable.

Phase 2 provides the market-data slice: :mod:`~quantplatform.storage.orm` declares the
``market_bars``, ``ingestion_runs`` and ``data_quality_findings`` tables, and
:mod:`~quantplatform.storage.repository` implements the
:class:`~quantplatform.core.interfaces.MarketBarRepository` and
:class:`~quantplatform.core.interfaces.IngestionRunRepository` ports over them. ORM rows
never leave this package: repository methods always return the domain models declared in
:mod:`quantplatform.core.models.data`.
"""
