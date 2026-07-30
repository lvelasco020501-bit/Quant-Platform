"""Alembic migrations against a real database.

These run the actual migration scripts rather than ``metadata.create_all``, because the
migration is the production path to a schema and is therefore what needs proving. They are
synchronous tests on purpose: Alembic's online mode calls :func:`asyncio.run` internally,
which cannot be nested inside an already-running event loop.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from tests.integration.conftest import TEST_DSN_ENV_VAR

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {"market_bars", "ingestion_runs", "data_quality_findings"}


def _reset_schema(dsn: str) -> None:
    """Drop every Phase 2 table and Alembic's own version table.

    A shared external database (the PostgreSQL path) carries state between tests: other
    integration tests drop the mapped tables but leave ``alembic_version`` stamped at head,
    which would make ``upgrade`` a silent no-op here and prove nothing. Each migration test
    therefore establishes its own empty starting schema rather than assuming one.
    """
    engine = create_engine(_sync_dsn(dsn))
    try:
        with engine.begin() as connection:
            for table in (*sorted(EXPECTED_TABLES), "alembic_version"):
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
    finally:
        engine.dispose()


@pytest.fixture
def migration_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the platform's configuration at an empty database for the migration run."""
    dsn = os.environ.get(TEST_DSN_ENV_VAR) or f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}"
    monkeypatch.setenv("QP_DATABASE__DSN", dsn)
    monkeypatch.chdir(PROJECT_ROOT)
    _reset_schema(dsn)
    try:
        yield dsn
    finally:
        _reset_schema(dsn)


@pytest.fixture
def alembic_config(migration_dsn: str) -> Config:  # noqa: ARG001 - fixture ordering only
    """Return the project's Alembic configuration."""
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _sync_dsn(dsn: str) -> str:
    """Return a synchronous DSN for inspection, mirroring the async one used to migrate."""
    return dsn.replace("+aiosqlite", "").replace("+psycopg", "+psycopg")


def _table_names(dsn: str) -> set[str]:
    """Return the tables that currently exist in the database."""
    engine = create_engine(_sync_dsn(dsn))
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_every_phase_two_table(alembic_config: Config, migration_dsn: str) -> None:
    command.upgrade(alembic_config, "head")
    assert _table_names(migration_dsn) >= EXPECTED_TABLES


def test_downgrade_removes_every_phase_two_table(
    alembic_config: Config, migration_dsn: str
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    remaining = _table_names(migration_dsn)
    assert not (EXPECTED_TABLES & remaining)


def test_upgrade_downgrade_upgrade_is_repeatable(
    alembic_config: Config, migration_dsn: str
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    assert _table_names(migration_dsn) >= EXPECTED_TABLES


def test_migrated_schema_carries_the_natural_key_constraint(
    alembic_config: Config, migration_dsn: str
) -> None:
    command.upgrade(alembic_config, "head")

    engine = create_engine(_sync_dsn(migration_dsn))
    try:
        constraints = inspect(engine).get_unique_constraints("market_bars")
    finally:
        engine.dispose()

    names = {constraint["name"] for constraint in constraints}
    assert "uq_market_bars_natural_key" in names


def test_migrated_price_columns_are_not_floating_point(
    alembic_config: Config, migration_dsn: str
) -> None:
    command.upgrade(alembic_config, "head")

    engine = create_engine(_sync_dsn(migration_dsn))
    try:
        columns = {
            column["name"]: column["type"] for column in inspect(engine).get_columns("market_bars")
        }
    finally:
        engine.dispose()

    for name in ("open", "high", "low", "close", "volume"):
        assert not isinstance(columns[name], sqlalchemy.Float), name


def test_migrated_findings_table_references_ingestion_runs(
    alembic_config: Config, migration_dsn: str
) -> None:
    command.upgrade(alembic_config, "head")

    engine = create_engine(_sync_dsn(migration_dsn))
    try:
        foreign_keys = inspect(engine).get_foreign_keys("data_quality_findings")
    finally:
        engine.dispose()

    assert any(key["referred_table"] == "ingestion_runs" for key in foreign_keys)
