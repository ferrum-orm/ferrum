"""Integration tests for Phase 4 connection runtime: timeouts, health, shutdown."""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import os

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.errors import FerrumConnectionError, FerrumTimeoutError, map_db_error

from .backends import Backend


async def _seed_payload_rows(
    conn: ferrum.connection.Connection,
    backend: Backend,
    table_name: str,
    *,
    rows: int = 2000,
) -> None:
    driver = conn._require_driver()
    q = backend.quote
    chunk_size = 500
    for chunk_start in range(0, rows, chunk_size):
        chunk_end = min(chunk_start + chunk_size, rows)
        values_sql = ", ".join(f"('row-{i}')" for i in range(chunk_start, chunk_end))
        await driver.execute(f"INSERT INTO {q(table_name)} (payload) VALUES {values_sql}")


@pytest.mark.integration
async def test_health_check_returns_true(db_conn: ferrum.connection.Connection) -> None:
    assert await db_conn.health_check() is True


@pytest.mark.integration
async def test_query_timeout_on_live_backend(
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    if backend.name == "sqlite":
        pytest.skip("SQLite driver ignores query_timeout pool knobs")

    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None

    table_name = f"ferrum_runtime_timeout_{unique_suffix}"

    class Slow(ferrum.Model):
        id: int = 0
        payload: str = ""

        class Meta:
            table = table_name

    q = backend.quote
    col_ddl = ", ".join(
        [
            f"{q('id')} {backend.types['pk_serial']}",
            f"{q('payload')} {backend.types['text']} NOT NULL",
        ]
    )
    create_sql = f"CREATE TABLE {q(table_name)} ({col_ddl})"
    drop_sql = f"DROP TABLE IF EXISTS {q(table_name)}"

    async with ferrum.connect(dsn) as seed_conn:
        await seed_conn._require_driver().execute(create_sql)
        try:
            await _seed_payload_rows(seed_conn, backend, table_name, rows=2000)

            conn = Connection(dsn, query_timeout=0.001)
            await conn.open()
            try:
                with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
                    await Slow.objects.all(conn)
            finally:
                await conn.close()
        finally:
            await seed_conn._require_driver().execute(drop_sql)


@pytest.mark.integration
async def test_graceful_shutdown_rejects_new_work(backend: Backend) -> None:
    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None

    conn = Connection(dsn, drain_timeout=2.0)
    await conn.open()
    conn._lifecycle.stop_accepting()
    with pytest.raises(FerrumConnectionError, match="shutting down"):
        conn._require_driver()
    await conn.close()


@pytest.mark.integration
async def test_acquire_timeout_on_exhausted_pool(backend: Backend) -> None:
    if backend.name == "sqlite":
        pytest.skip("SQLite driver uses a single connection, not a pool")

    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None

    conn = Connection(dsn, min_size=1, max_size=1, acquire_timeout=0.2)
    await conn.open()
    try:
        async with conn.acquire():
            with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
                async with conn.acquire():
                    pass
    finally:
        await conn.close()


@pytest.mark.integration
async def test_statement_timeout_cancels_long_query(pg_dsn: str) -> None:
    conn = Connection(pg_dsn, statement_timeout=300)
    await conn.open()
    try:
        with pytest.raises(FerrumTimeoutError):
            try:
                await conn._driver.fetchval("SELECT pg_sleep(2)")
            except Exception as exc:
                raise map_db_error(exc) from None
    finally:
        await conn.close()
