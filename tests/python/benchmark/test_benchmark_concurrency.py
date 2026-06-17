"""Benchmark: concurrent QuerySet execution under pool pressure.

Workloads: 100-1000 concurrent select tasks with a 10-50 connection pool.
Requires ``FERRUM_TEST_DSN``.
"""

from __future__ import annotations

import asyncio

import pytest

import ferrum


@pytest.mark.benchmark
async def test_concurrent_select_100_tasks(
    benchmark: pytest.BenchmarkFixture,
    bench_conn: ferrum.connection.Connection,
) -> None:
    pytest.importorskip("ferrum._native", reason="Rust extension not built")

    class BenchRow(ferrum.Model):
        id: int = 0
        val: int = 0

        class Meta:
            table = "ferrum_bench_concurrent"

    pool = bench_conn._pool
    assert pool is not None

    async with pool.acquire() as raw_conn:
        await raw_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ferrum_bench_concurrent (
                id SERIAL PRIMARY KEY,
                val INT NOT NULL
            )
            """
        )
        await raw_conn.execute(
            "INSERT INTO ferrum_bench_concurrent (val) SELECT generate_series(1, 20)"
        )

    async def run_concurrent() -> int:
        async def one_count() -> int:
            return await BenchRow.objects.count(bench_conn)

        results = await asyncio.gather(*[one_count() for _ in range(100)])
        return sum(results)

    try:
        total = await benchmark.pedantic(run_concurrent, rounds=5, iterations=1)
        assert total == 2000  # 100 tasks x 20 rows each
    finally:
        async with pool.acquire() as raw_conn:
            await raw_conn.execute("DROP TABLE IF EXISTS ferrum_bench_concurrent")


@pytest.mark.benchmark
async def test_concurrent_select_1000_tasks(
    benchmark: pytest.BenchmarkFixture,
    benchmark_dsn: str,
) -> None:
    """Heavier fan-out: 1000 count queries with max pool size 50."""
    pytest.importorskip("ferrum._native", reason="Rust extension not built")

    class BenchRow(ferrum.Model):
        id: int = 0
        val: int = 0

        class Meta:
            table = "ferrum_bench_concurrent_1k"

    async with ferrum.connect(benchmark_dsn, min_size=10, max_size=50) as conn:
        pool = conn._pool
        assert pool is not None

        async with pool.acquire() as raw_conn:
            await raw_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ferrum_bench_concurrent_1k (
                    id SERIAL PRIMARY KEY,
                    val INT NOT NULL
                )
                """
            )
            await raw_conn.execute(
                "INSERT INTO ferrum_bench_concurrent_1k (val) SELECT generate_series(1, 10)"
            )

        async def run_concurrent() -> int:
            async def one_count() -> int:
                return await BenchRow.objects.count(conn)

            results = await asyncio.gather(*[one_count() for _ in range(1000)])
            return sum(results)

        try:
            total = await benchmark.pedantic(run_concurrent, rounds=3, iterations=1)
            assert total == 10_000
        finally:
            async with pool.acquire() as raw_conn:
                await raw_conn.execute("DROP TABLE IF EXISTS ferrum_bench_concurrent_1k")
