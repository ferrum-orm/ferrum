"""Portable declarative DDL for the multi-driver integration suite.

``transient_table`` creates a table before the block and drops it afterwards
using ``conn._require_driver().execute()`` — no asyncpg pool, no dialect
assumptions.  Column types are resolved from the backend's ``types`` mapping
so the same test body can run against every supported backend.

Example::

    from .backends import Capability
    from .schema import Column, transient_table

    async def test_create(db_conn, backend, unique_suffix):
        table = f"ferrum_t_{unique_suffix}"
        async with transient_table(
            db_conn, table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("title", "text", null=False),
                Column("published", "bool", default="false"),
            ],
        ) as conn:
            row = await MyModel.objects.create(conn, title="hello", published=True)
            assert row.id > 0
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ferrum.connection import Connection

from .backends import Backend


@dataclass
class Column:
    """Specification for one column in a portable transient table."""

    name: str
    """Column identifier (quoted by the backend's ``quote`` function)."""

    type_key: str
    """Logical type key resolved against ``Backend.types``.

    Supported keys: ``"pk_serial"``, ``"text"``, ``"int"``, ``"bool"``.
    """

    null: bool = True
    """Add ``NOT NULL`` when *False*.

    Automatically suppressed for ``"pk_serial"`` columns, which are already
    ``NOT NULL`` in all backend DDL variants.
    """

    default: str | None = None
    """SQL literal for a ``DEFAULT`` clause, e.g. ``"false"`` or ``"0"``."""

    extra: str = ""
    """Optional backend-agnostic DDL appended verbatim, e.g. ``"UNIQUE"``."""


def _render_column(col: Column, backend: Backend) -> str:
    """Render one column to a DDL fragment for *backend*."""
    type_ddl = backend.types[col.type_key]
    q = backend.quote
    parts = [q(col.name), type_ddl]
    # pk_serial variants already embed NOT NULL; avoid emitting it twice.
    if not col.null and "PRIMARY KEY" not in type_ddl.upper():
        parts.append("NOT NULL")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    if col.extra:
        parts.append(col.extra)
    return " ".join(parts)


@contextlib.asynccontextmanager
async def transient_table(
    conn: Connection,
    name: str,
    *,
    backend: Backend,
    columns: list[Column],
) -> AsyncIterator[Connection]:
    """Create *name* before the block and ``DROP TABLE IF EXISTS`` it afterwards.

    DDL is derived from *columns* using *backend*'s type mapping and quoting
    convention.  Execution goes through ``conn._require_driver().execute()``
    so no asyncpg pool or backend-specific internals are accessed.

    The context manager yields *conn* unchanged so callers can pass it
    directly to ``Model.objects.*`` terminals::

        async with transient_table(db_conn, tbl, backend=backend, columns=[...]) as conn:
            row = await MyModel.objects.create(conn, ...)
    """
    q = backend.quote
    col_ddl = ", ".join(_render_column(col, backend) for col in columns)
    create_sql = f"CREATE TABLE {q(name)} ({col_ddl})"
    drop_sql = f"DROP TABLE IF EXISTS {q(name)}"

    await conn._require_driver().execute(create_sql)
    try:
        yield conn
    finally:
        await conn._require_driver().execute(drop_sql)
