"""Integration tests for Phase 4 connection runtime: timeouts, health, shutdown."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.errors import FerrumConnectionError, FerrumTimeoutError

from .helpers import raw_pool, seed_bulk_text_rows, transient_table


@pytest.mark.integration
async def test_health_check_returns_true(pg_dsn: str) -> None:
    async with ferrum.connect(pg_dsn) as conn:
        assert await conn.health_check() is True


@pytest.mark.integration
async def test_query_timeout_on_live_pg(
    pg_dsn: str, require_native: None, unique_suffix: str
) -> None:
    table_name = f"ferrum_runtime_timeout_{unique_suffix}"

    class Slow(ferrum.Model):
        id: int = 0
        payload: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            payload TEXT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    conn = Connection(pg_dsn, query_timeout=0.001)
    await conn.open()
    try:
        async with transient_table(conn, create_sql=create_sql, drop_sql=drop_sql):
            pool = raw_pool(conn)
            await seed_bulk_text_rows(pool, table_name, rows=2000)
            with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
                await Slow.objects.all(conn)
    finally:
        await conn.close()


@pytest.mark.integration
async def test_graceful_shutdown_rejects_new_work(pg_dsn: str) -> None:
    conn = Connection(pg_dsn, drain_timeout=2.0)
    await conn.open()
    conn._lifecycle.stop_accepting()
    with pytest.raises(FerrumConnectionError, match="shutting down"):
        conn._require_driver()
    await conn.close()


@pytest.mark.integration
async def test_acquire_timeout_on_exhausted_pool(pg_dsn: str) -> None:
    conn = Connection(pg_dsn, min_size=1, max_size=1, acquire_timeout=0.2)
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
    from ferrum.errors import map_db_error

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
