"""Integration tests for connection failure, closed pool, and timeout semantics."""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import asyncio

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.errors import FerrumConnectionError

from .backends import Backend
from .schema import Column, transient_table


@pytest.mark.integration
@pytest.mark.parametrize(
    ("dsn", "secret"),
    [
        ("postgresql://ferrum_test_user:supersecret@127.0.0.1:59999/nodb", "supersecret"),
        ("mysql://ferrum_test_user:supersecret@127.0.0.1:59999/nodb", "supersecret"),
        (
            "mssql://ferrum_test_user:supersecret@127.0.0.1:59999/nodb"
            "?driver=ODBC+Driver+18+for+SQL+Server",
            "supersecret",
        ),
    ],
)
async def test_connection_failure_redacts_dsn(dsn: str, secret: str) -> None:
    """Bad host/port must raise FerrumConnectionError without leaking the DSN."""
    if dsn.startswith("mysql://"):
        pytest.importorskip("asyncmy")
    elif dsn.startswith("mssql://"):
        pytest.importorskip("aioodbc")

    conn = Connection(dsn)
    with pytest.raises(FerrumConnectionError) as exc_info:
        await conn.open()

    message = str(exc_info.value)
    assert "FERR-E101" in message
    assert secret not in message.lower()
    assert "://" not in message


@pytest.mark.integration
async def test_query_on_unopened_pool_raises_connection_error() -> None:
    class Widget(ferrum.Model):
        id: int = 0

    conn = Connection("postgresql://unused@127.0.0.1/unused")
    with pytest.raises(FerrumConnectionError, match="not open"):
        await Widget.objects.count(conn)


async def _seed_payload_rows(
    conn: ferrum.connection.Connection,
    backend: Backend,
    table_name: str,
    *,
    rows: int = 5000,
) -> None:
    """Portable batched INSERT for stress/cancellation tests."""
    driver = conn._require_driver()
    q = backend.quote
    chunk_size = 500
    for chunk_start in range(0, rows, chunk_size):
        chunk_end = min(chunk_start + chunk_size, rows)
        values_sql = ", ".join(f"('row-{i}')" for i in range(chunk_start, chunk_end))
        await driver.execute(f"INSERT INTO {q(table_name)} (payload) VALUES {values_sql}")


@pytest.mark.integration
async def test_asyncio_timeout_at_python_await(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
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

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("payload", "text", null=False),
        ],
    ) as conn:
        await _seed_payload_rows(conn, backend, table_name)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(Slow.objects.all(conn), timeout=0)


@pytest.mark.integration
async def test_cancellation_propagates_from_await(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_cancel_{unique_suffix}"

    class Task(ferrum.Model):
        id: int = 0
        payload: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("payload", "text", null=False),
        ],
    ) as conn:
        await _seed_payload_rows(conn, backend, table_name)

        task = asyncio.create_task(Task.objects.all(conn))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
