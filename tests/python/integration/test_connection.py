"""Integration tests for connection failure, closed pool, and timeout semantics."""

from __future__ import annotations

import asyncio

import pytest
from helpers import raw_pool, seed_bulk_text_rows, transient_table

import ferrum
from ferrum.connection import Connection
from ferrum.errors import FerrumConnectionError


@pytest.mark.integration
async def test_connection_failure_redacts_dsn() -> None:
    """Bad host/port must raise FerrumConnectionError without leaking the DSN."""
    bad_dsn = "postgresql://ferrum_test_user:supersecret@127.0.0.1:59999/nodb"

    conn = Connection(bad_dsn)
    with pytest.raises(FerrumConnectionError) as exc_info:
        await conn.open()

    message = str(exc_info.value)
    assert "FERR-E101" in message
    assert "supersecret" not in message.lower()
    assert "postgresql://" not in message


@pytest.mark.integration
async def test_query_on_unopened_pool_raises_connection_error() -> None:
    class Widget(ferrum.Model):
        id: int = 0

    conn = Connection("postgresql://unused@127.0.0.1/unused")
    with pytest.raises(FerrumConnectionError, match="not open"):
        await Widget.objects.count(conn)


@pytest.mark.integration
async def test_asyncio_timeout_at_python_await(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Zero-second asyncio budget cancels the driver await on a non-trivial scan."""
    table_name = f"ferrum_int_timeout_{unique_suffix}"

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

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        pool = raw_pool(pg_conn)
        await seed_bulk_text_rows(pool, table_name)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(Slow.objects.all(pg_conn), timeout=0)


@pytest.mark.integration
async def test_cancellation_propagates_from_await(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_cancel_{unique_suffix}"

    class Task(ferrum.Model):
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

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        pool = raw_pool(pg_conn)
        await seed_bulk_text_rows(pool, table_name)

        task = asyncio.create_task(Task.objects.all(pg_conn))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
