"""Custom SQLAlchemy column types.

The platform's non-negotiable rule that money, prices and quantities are never floating
point extends to the database layer: a column that quietly rounds through a binary float
on write would reintroduce exactly the error the domain's :class:`~decimal.Decimal` types
exist to prevent.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = ["ExactNumeric"]

_SQLITE_TEXT_LENGTH = 64
"""Generous headroom for the canonical decimal string stored on SQLite."""


class ExactNumeric(TypeDecorator[Decimal]):
    """A ``NUMERIC(precision, scale)`` column that never loses precision.

    PostgreSQL's ``NUMERIC`` is arbitrary-precision and exact, so this type delegates to it
    directly there. SQLite has no native arbitrary-precision decimal storage: SQLAlchemy's
    plain :class:`~sqlalchemy.Numeric` silently binds Python ``Decimal`` values through a
    C ``double`` on that backend, which measurably rounds values (verified while building
    this module: ``Decimal("50000.123456789012345678")`` round-tripped as
    ``Decimal("50000.123456789013289381")``). That would make SQLite-backed tests validate
    behaviour PostgreSQL does not actually exhibit. This type instead stores the canonical
    decimal string as ``TEXT`` on SQLite and parses it back into an exact ``Decimal`` on
    read, so precision survives the round trip identically on every backend the platform
    tests against.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        super().__init__(precision=precision, scale=scale)
        self._precision = precision
        self._scale = scale

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Return ``TEXT`` on SQLite and native ``NUMERIC`` everywhere else."""
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(_SQLITE_TEXT_LENGTH))
        return dialect.type_descriptor(Numeric(self._precision, self._scale))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Any:  # noqa: ANN401
        """Render the value as a canonical decimal string on SQLite; pass it through elsewhere.

        The ``Any`` return type is inherited from :class:`~sqlalchemy.types.TypeDecorator`,
        whose bind hook must accommodate whatever the DBAPI driver accepts; it is not a
        choice made here.
        """
        if value is None:
            return None
        if dialect.name == "sqlite":
            return format(value, "f")
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:  # noqa: ANN401
        """Parse the stored text back into a ``Decimal`` on SQLite; pass it through elsewhere.

        The ``Any`` input type mirrors :meth:`process_bind_param`'s inherited signature.
        """
        if value is None:
            return None
        if dialect.name == "sqlite":
            return Decimal(value)
        return Decimal(value) if not isinstance(value, Decimal) else value
