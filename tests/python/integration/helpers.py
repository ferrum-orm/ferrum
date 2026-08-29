"""Shared helpers for Ferrum integration tests.

All helpers route setup SQL through ``conn._require_driver().execute()``
rather than reaching into asyncpg pool internals, so they work with any
Ferrum-supported driver.

``raw_pool()`` was removed in the harness phase (plan: ``harness``).  Tests
that previously called ``pool = raw_pool(pg_conn)`` are updated in the
``port-core`` phase.  The portable multi-backend DDL helper lives in
``schema.transient_table``; this module's ``transient_table`` variant accepts
caller-supplied ``create_sql`` / ``drop_sql`` and is retained for existing
PostgreSQL-specific tests that carry their own DDL.
"""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator

from ferrum.connection import Connection


@contextlib.asynccontextmanager
async def transient_table(
    conn: Connection,
    *,
    create_sql: str,
    drop_sql: str,
) -> AsyncIterator[Connection]:
    """Create a table before the block and drop it afterward.

    DDL is caller-supplied.  Use ``schema.transient_table`` together with
    ``schema.Column`` for portable multi-backend DDL.  This variant is kept
    for existing PostgreSQL-specific tests whose ``CREATE TABLE`` statements
    contain Postgres-only syntax (``SERIAL``, ``BOOLEAN``, etc.).
    """
    await conn._require_driver().execute(create_sql)
    try:
        yield conn
    finally:
        await conn._require_driver().execute(drop_sql)


async def seed_bulk_text_rows(
    conn: Connection,
    table_name: str,
    *,
    rows: int = 5000,
) -> None:
    """Insert *rows* rows into ``table_name(payload TEXT)`` for stress tests.

    Uses chunked multi-row ``INSERT … VALUES`` with Python-generated MD5 hex
    strings instead of ``generate_series()`` + ``md5()``, so the helper is
    portable across backends.  Values are embedded as SQL string literals; the
    ``ruff: noqa: S608`` directive at the top of this module acknowledges that
    the identifiers and literal values are test-controlled.
    """
    driver = conn._require_driver()
    chunk_size = 500
    for chunk_start in range(0, rows, chunk_size):
        chunk_end = min(chunk_start + chunk_size, rows)
        values_sql = ", ".join(
            f"('{hashlib.md5(str(i).encode()).hexdigest()}')"  # noqa: S324
            for i in range(chunk_start, chunk_end)
        )
        await driver.execute(f'INSERT INTO "{table_name}" (payload) VALUES {values_sql}')


async def seed_int_rows(
    conn: Connection,
    table_name: str,
    *values: int,
) -> None:
    """Insert integer rows into ``table_name(val INT)`` for CRUD setup.

    Values are embedded directly as integer literals — no injection risk for
    ``int`` inputs.
    """
    if not values:
        return
    values_sql = ", ".join(f"({v})" for v in values)
    await conn._require_driver().execute(f'INSERT INTO "{table_name}" (val) VALUES {values_sql}')
