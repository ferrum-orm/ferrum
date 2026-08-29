"""Integration tests for pool exhaustion and concurrent QuerySet execution."""

from __future__ import annotations

import asyncio
import os

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.errors import FerrumTimeoutError

from .backends import Backend
from .schema import Column, transient_table


@pytest.mark.integration
async def test_pool_exhaustion_blocks_until_timeout(
    backend: Backend,
) -> None:
    """When all pool slots are held, acquire waits until the configured timeout."""
    if backend.name == "sqlite":
        pytest.skip("SQLite driver uses a single connection, not a pool")

    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None

    conn = Connection(dsn, min_size=1, max_size=2, acquire_timeout=0.5)
    await conn.open()
    try:
        async with conn.acquire(), conn.acquire():
            with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
                async with conn.acquire():
                    pass
    finally:
        await conn.close()


@pytest.mark.integration
async def test_concurrent_queryset_counts(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_concurrent_{unique_suffix}"

    class Metric(ferrum.Model):
        id: int = 0
        bucket: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("bucket", "int", null=False),
        ],
    ) as conn:
        for bucket in range(5):
            await Metric.objects.create(conn, bucket=bucket)

        results = await asyncio.gather(
            *[Metric.objects.filter(bucket=i).count(conn) for i in range(5)]
        )
        assert results == [1, 1, 1, 1, 1]

        total = await asyncio.gather(*[Metric.objects.count(conn) for _ in range(10)])
        assert all(n == 5 for n in total)


@pytest.mark.integration
async def test_concurrent_reads_return_consistent_rows(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_concurrent_all_{unique_suffix}"

    class Widget(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
        ],
    ) as conn:
        await Widget.objects.create(conn, name="alpha")
        await Widget.objects.create(conn, name="beta")

        batches = await asyncio.gather(*[Widget.objects.all(conn) for _ in range(8)])
        for rows in batches:
            assert len(rows) == 2
            assert {r.name for r in rows} == {"alpha", "beta"}
