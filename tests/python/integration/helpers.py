"""Shared helpers for live PostgreSQL integration tests."""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from ferrum.connection import Connection


@contextlib.asynccontextmanager
async def transient_table(
    pg_conn: Connection,
    *,
    create_sql: str,
    drop_sql: str,
) -> AsyncIterator[Connection]:
    """Create a table before the block and drop it afterward."""
    pool = raw_pool(pg_conn)
    async with pool.acquire() as raw:
        await raw.execute(create_sql)
    try:
        yield pg_conn
    finally:
        async with pool.acquire() as raw:
            await raw.execute(drop_sql)


def raw_pool(pg_conn: Connection):
    """Return the underlying asyncpg pool, asserting it is open."""
    pool = getattr(pg_conn._require_driver(), "_pool", None)
    assert pool is not None
    return pool


async def seed_bulk_text_rows(pool, table_name: str, *, rows: int = 5000) -> None:
    """Insert generated rows for cancellation/timeout stress tests."""
    sql = (
        f'INSERT INTO "{table_name}" (payload) '
        f"SELECT md5(g::text) FROM generate_series(1, {rows}) g"
    )
    async with pool.acquire() as raw:
        await raw.execute(sql)


async def seed_int_rows(pool, table_name: str, *values: int) -> None:
    """Insert integer rows for CRUD setup."""
    placeholders = ", ".join(f"(${i})" for i in range(1, len(values) + 1))
    sql = f'INSERT INTO "{table_name}" (val) VALUES {placeholders}'
    async with pool.acquire() as raw:
        await raw.execute(sql, *values)
