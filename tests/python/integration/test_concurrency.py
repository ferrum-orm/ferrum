"""Integration tests for pool exhaustion and concurrent QuerySet execution."""

from __future__ import annotations

import asyncio

import pytest
from helpers import raw_pool, transient_table

import ferrum


@pytest.mark.integration
async def test_pool_exhaustion_blocks_until_timeout(
    pg_dsn: str,
    require_native: None,
) -> None:
    """When all pool slots are held, acquire waits until asyncio cancels it."""
    async with ferrum.connect(pg_dsn, min_size=1, max_size=2) as conn:
        pool = raw_pool(conn)
        holder_a = await pool.acquire()
        holder_b = await pool.acquire()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(pool.acquire(), timeout=0.5)
        finally:
            await pool.release(holder_a)
            await pool.release(holder_b)


@pytest.mark.integration
async def test_concurrent_queryset_counts(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_concurrent_{unique_suffix}"

    class Metric(ferrum.Model):
        id: int = 0
        bucket: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            bucket INT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        for bucket in range(5):
            await Metric.objects.create(pg_conn, bucket=bucket)

        results = await asyncio.gather(
            *[Metric.objects.filter(bucket=i).count(pg_conn) for i in range(5)]
        )
        assert results == [1, 1, 1, 1, 1]

        total = await asyncio.gather(*[Metric.objects.count(pg_conn) for _ in range(10)])
        assert all(n == 5 for n in total)


@pytest.mark.integration
async def test_concurrent_reads_return_consistent_rows(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_concurrent_all_{unique_suffix}"

    class Widget(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        await Widget.objects.create(pg_conn, name="alpha")
        await Widget.objects.create(pg_conn, name="beta")

        batches = await asyncio.gather(*[Widget.objects.all(pg_conn) for _ in range(8)])
        for rows in batches:
            assert len(rows) == 2
            assert {r.name for r in rows} == {"alpha", "beta"}
