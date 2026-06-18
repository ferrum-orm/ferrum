"""Benchmark: CRUD path overhead against live PostgreSQL.

Measures end-to-end ORM overhead (compile → execute → hydrate) excluding one-time
schema setup. Requires ``FERRUM_TEST_DSN``.
"""

from __future__ import annotations

import pytest

import ferrum


@pytest.mark.benchmark
async def test_create_overhead(
    benchmark: pytest.BenchmarkFixture,
    bench_conn: ferrum.connection.Connection,
) -> None:
    pytest.importorskip("ferrum._native", reason="Rust extension not built")

    class BenchWidget(ferrum.Model):
        id: int = 0
        name: str = ""
        active: bool = True

        class Meta:
            table = "ferrum_bench_widget"

    pool = bench_conn._pool
    assert pool is not None

    async with pool.acquire() as raw_conn:
        await raw_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ferrum_bench_widget (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE
            )
            """
        )

    async def create_one() -> None:
        await BenchWidget.objects.create(bench_conn, name="bench-item", active=True)

    try:
        await benchmark.pedantic(create_one, rounds=20, iterations=1)
    finally:
        async with pool.acquire() as raw_conn:
            await raw_conn.execute("DROP TABLE IF EXISTS ferrum_bench_widget")


@pytest.mark.benchmark
async def test_filter_get_overhead(
    benchmark: pytest.BenchmarkFixture,
    bench_conn: ferrum.connection.Connection,
) -> None:
    pytest.importorskip("ferrum._native", reason="Rust extension not built")

    class BenchItem(ferrum.Model):
        id: int = 0
        label: str = ""

        class Meta:
            table = "ferrum_bench_item"

    pool = bench_conn._pool
    assert pool is not None

    async with pool.acquire() as raw_conn:
        await raw_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ferrum_bench_item (
                id SERIAL PRIMARY KEY,
                label TEXT NOT NULL
            )
            """
        )
        row = await raw_conn.fetchrow(
            "INSERT INTO ferrum_bench_item (label) VALUES ($1) RETURNING id",
            "target",
        )
        target_id = row["id"]

    async def fetch_one() -> None:
        item = await BenchItem.objects.filter(id=target_id).get(bench_conn)
        assert item.label == "target"

    try:
        await benchmark.pedantic(fetch_one, rounds=30, iterations=1)
    finally:
        async with pool.acquire() as raw_conn:
            await raw_conn.execute("DROP TABLE IF EXISTS ferrum_bench_item")


@pytest.mark.benchmark
def test_native_compile_overhead(benchmark: pytest.BenchmarkFixture) -> None:
    """Rust compile round-trip without database I/O."""

    _native = pytest.importorskip("ferrum._native", reason="Rust extension not built")

    class Probe(ferrum.Model):
        id: int = 0
        email: str = ""
        active: bool = True

    qs = Probe.objects.filter(active=True).order_by("-id").limit(25)
    metadata_json = Probe.get_metadata().to_metadata_json()
    ir_json = qs.to_ir_json()

    def compile_once() -> None:
        result = _native.compile_query(metadata_json, ir_json, "postgres")
        assert "sql_text" in result

    benchmark(compile_once)
