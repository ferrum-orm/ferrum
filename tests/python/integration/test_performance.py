"""End-to-end performance benchmarks: Ferrum vs raw asyncpg (and optional
SQLAlchemy async) on identical PostgreSQL schemas and workloads.

Design (W4-B task contract):
- Measures throughput, p50/p95/p99 latency, CPU time, memory peak, and pool
  saturation for each workload.
- Workloads: point reads, filtered pages, relation loads (select_related /
  prefetch_related), writes, bulk operations, JSONB, vector KNN, streaming,
  transactions.
- Profiles the JSON/msgpack PyO3 wire-format boundary by toggling
  ``FERRUM_WIRE_FORMAT``.
- Stable threshold/regression reporting only — NO flaky hard CI gates.
  Results are recorded to ``.bench-results/`` (gitignored) and compared
  against a stored baseline with a wide tolerance band. A regression is
  reported, never a hard failure, until variance is characterized.

Tunables (env vars):
- ``FERRUM_BENCH_ITERS`` — iterations per workload (default 200, lower for CI).
- ``FERRUM_BENCH_ROWS`` — seed row count (default 500).
- ``FERRUM_BENCH_WARMUP`` — warmup iterations discarded (default 20).
- ``FERRUM_BENCH_RECORD`` — write results to ``.bench-results/perf.json``
  (default ``"1"``).
- ``FERRUM_BENCH_BASELINE`` — baseline JSON to compare against (optional).

PostgreSQL-only: the comparison target is raw ``asyncpg`` (the Ferrum driver).
SQLAlchemy async is compared when installed; tests skip gracefully otherwise.
"""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

import contextlib
import json
import os
import resource
import statistics
import time
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

import asyncpg
import pytest
import pytest_asyncio

import ferrum

# Import observability metrics (W4-A owns — import only, never modify).
from ferrum.observability import enable_metrics, get_metrics, reset_metrics

pytestmark = [pytest.mark.integration, pytest.mark.benchmark]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ITERS = int(os.environ.get("FERRUM_BENCH_ITERS", "200"))
ROWS = int(os.environ.get("FERRUM_BENCH_ROWS", "500"))
WARMUP = int(os.environ.get("FERRUM_BENCH_WARMUP", "20"))
RECORD = os.environ.get("FERRUM_BENCH_RECORD", "1") == "1"
BASELINE_PATH = os.environ.get("FERRUM_BENCH_BASELINE")
# Wide tolerance: a regression is reported only beyond this factor vs baseline.
REGRESSION_FACTOR = float(os.environ.get("FERRUM_BENCH_REGRESSION_FACTOR", "3.0"))
BENCH_RESULTS_DIR = os.path.join(os.getcwd(), ".bench-results")
BENCH_RESULTS_PATH = os.path.join(BENCH_RESULTS_DIR, "perf.json")

# SQLAlchemy is optional — skip comparison when not installed.
try:
    from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

    _HAS_SQLALCHEMY = True
except Exception:
    _HAS_SQLALCHEMY = False


# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    """One workload measurement for one implementation."""

    workload: str
    implementation: str  # "ferrum" | "asyncpg" | "sqlalchemy" | "ferrum_msgpack"
    iters: int
    throughput_ops_per_s: float
    p50_us: float
    p95_us: float
    p99_us: float
    mean_us: float
    cpu_user_ms: float
    cpu_sys_ms: float
    mem_peak_kb: float
    pool_acquired_peak: int = -1
    extra: dict[str, Any] = field(default_factory=dict)


def _percentiles(samples_us: list[float]) -> tuple[float, float, float, float]:
    """Return (p50, p95, p99, mean) in microseconds."""
    if not samples_us:
        return 0.0, 0.0, 0.0, 0.0
    s = sorted(samples_us)
    n = len(s)

    def pct(p: float) -> float:
        idx = max(0, min(n - 1, int((p / 100.0) * (n - 1))))
        return s[idx]

    return pct(50.0), pct(95.0), pct(99.0), statistics.fmean(s)


def _cpu_ms() -> tuple[float, float]:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime * 1000.0, r.ru_stime * 1000.0


async def run_workload(
    name: str,
    implementation: str,
    fn: Callable[[], Awaitable[Any]],
    *,
    iters: int = ITERS,
    warmup: int = WARMUP,
    pool_snapshot: Callable[[], int] | None = None,
    extra: dict[str, Any] | None = None,
) -> Sample:
    """Run *fn* *iters* times after *warmup*, collecting latency/CPU/mem stats."""
    # Warmup — discard.
    for _ in range(warmup):
        await fn()

    tracemalloc.start()
    cpu_u0, cpu_s0 = _cpu_ms()
    pool_acquired_peak = 0
    timings_us: list[float] = []
    t0 = time.perf_counter()

    for _ in range(iters):
        i0 = time.perf_counter()
        await fn()
        timings_us.append((time.perf_counter() - i0) * 1_000_000.0)
        if pool_snapshot is not None:
            pool_acquired_peak = max(pool_acquired_peak, pool_snapshot())

    elapsed = time.perf_counter() - t0
    cpu_u1, cpu_s1 = _cpu_ms()
    _, mem_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    p50, p95, p99, mean = _percentiles(timings_us)
    throughput = (iters / elapsed) if elapsed > 0 else 0.0
    return Sample(
        workload=name,
        implementation=implementation,
        iters=iters,
        throughput_ops_per_s=throughput,
        p50_us=p50,
        p95_us=p95,
        p99_us=p99,
        mean_us=mean,
        cpu_user_ms=cpu_u1 - cpu_u0,
        cpu_sys_ms=cpu_s1 - cpu_s0,
        mem_peak_kb=mem_peak / 1024.0,
        pool_acquired_peak=pool_acquired_peak,
        extra=extra or {},
    )


def _record_sample(sample: Sample, results: list[Sample]) -> None:
    results.append(sample)
    print(
        f"  [{sample.implementation:16s}] {sample.workload:28s} "
        f"thr={sample.throughput_ops_per_s:8.1f} ops/s  "
        f"p50={sample.p50_us:8.1f}us  p95={sample.p95_us:8.1f}us  "
        f"p99={sample.p99_us:8.1f}us  "
        f"cpu={sample.cpu_user_ms + sample.cpu_sys_ms:7.1f}ms  "
        f"mem={sample.mem_peak_kb:8.1f}KB  pool_peak={sample.pool_acquired_peak}"
    )


def _write_results(results: list[Sample]) -> None:
    if not RECORD:
        return
    os.makedirs(BENCH_RESULTS_DIR, exist_ok=True)
    payload = {
        "run_id": os.environ.get("FERRUM_BENCH_RUN_ID", "adhoc"),
        "iters": ITERS,
        "rows": ROWS,
        "samples": [asdict(s) for s in results],
    }
    with open(BENCH_RESULTS_PATH, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\n  results written to {BENCH_RESULTS_PATH}")


def _compare_baseline(results: list[Sample]) -> None:
    """Soft regression check — report only, never hard-fail (no flaky gates)."""
    if not BASELINE_PATH or not os.path.exists(BASELINE_PATH):
        return
    with open(BASELINE_PATH) as fh:
        baseline = json.load(fh)
    by_key = {(s["workload"], s["implementation"]): s for s in baseline.get("samples", [])}
    print("\n  regression check vs baseline:")
    regressions = 0
    for s in results:
        key = (s.workload, s.implementation)
        base = by_key.get(key)
        if base is None:
            continue
        base_p50 = base.get("p50_us", 0.0)
        if base_p50 <= 0:
            continue
        ratio = s.p50_us / base_p50
        flag = "REGRESSION" if ratio > REGRESSION_FACTOR else "ok"
        if ratio > REGRESSION_FACTOR:
            regressions += 1
        print(
            f"    {flag:10s} {s.workload:28s} {s.implementation:16s} "
            f"p50={s.p50_us:.1f} vs base={base_p50:.1f} (x{ratio:.2f})"
        )
    if regressions:
        print(f"\n  NOTE: {regressions} regression(s) flagged — review only, not a CI failure.")


# ---------------------------------------------------------------------------
# Schema setup — identical tables for Ferrum / asyncpg / SQLAlchemy
# ---------------------------------------------------------------------------


async def _setup_schema(conn: ferrum.connection.Connection, suffix: str) -> dict[str, str]:
    """Create benchmark tables. Returns a dict of table names."""
    driver = conn._require_driver()
    names = {
        "items": f"bench_items_{suffix}",
        "authors": f"bench_authors_{suffix}",
        "books": f"bench_books_{suffix}",
        "docs": f"bench_docs_{suffix}",
        "vectors": f"bench_vec_{suffix}",
    }
    await driver.execute(
        f"""
        CREATE TABLE {names["items"]} (
            id SERIAL PRIMARY KEY,
            label TEXT NOT NULL,
            value INT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE {names["authors"]} (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE {names["books"]} (
            id SERIAL PRIMARY KEY,
            author_id INT NOT NULL REFERENCES {names["authors"]}(id),
            title TEXT NOT NULL
        );
        CREATE TABLE {names["docs"]} (
            id SERIAL PRIMARY KEY,
            payload JSONB NOT NULL,
            tags TEXT[]
        );
        """
    )
    # pgvector table — only if extension is available.
    try:
        await driver.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await driver.execute(
            f"CREATE TABLE {names['vectors']} (id SERIAL PRIMARY KEY, embedding vector(4) NOT NULL)"
        )
    except Exception:
        names["vectors"] = ""

    # Seed items.
    for i in range(ROWS):
        await driver.execute(
            f"INSERT INTO {names['items']} (label, value, active) VALUES ($1, $2, $3)",
            f"item-{i}",
            i,
            i % 2 == 0,
        )
    # Seed authors/books for relation loads (50 authors, 5 books each).
    for a in range(50):
        aid = await driver.fetchval(
            f"INSERT INTO {names['authors']} (name) VALUES ($1) RETURNING id",
            f"author-{a}",
        )
        for b in range(5):
            await driver.execute(
                f"INSERT INTO {names['books']} (author_id, title) VALUES ($1, $2)",
                aid,
                f"book-{a}-{b}",
            )
    # Seed JSONB docs.
    for i in range(ROWS):
        await driver.execute(
            f"INSERT INTO {names['docs']} (payload, tags) VALUES ($1, $2)",
            json.dumps({"i": i, "cat": "doc" if i % 2 else "post", "n": i * 2}),
            ["alpha", "beta"],
        )
    # Seed vectors.
    if names["vectors"]:
        for i in range(ROWS):
            await driver.execute(
                f"INSERT INTO {names['vectors']} (embedding) VALUES ($1)",
                [float(i), 0.1, 0.2, 0.3],
            )
    return names


async def _teardown_schema(conn: ferrum.connection.Connection, names: dict[str, str]) -> None:
    driver = conn._require_driver()
    for n in reversed(names.values()):
        if n:
            await driver.execute(f"DROP TABLE IF EXISTS {n}")
    with contextlib.suppress(Exception):
        await driver.execute("DROP EXTENSION IF EXISTS vector")


# ---------------------------------------------------------------------------
# Ferrum models bound to benchmark tables
# ---------------------------------------------------------------------------


def _make_models(names: dict[str, str]) -> dict[str, type[ferrum.Model]]:
    class BenchItem(ferrum.Model):
        id: int = 0
        label: str = ""
        value: int = 0
        active: bool = True

        class Meta:
            table = names["items"]

    class BenchAuthor(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = names["authors"]

    class BenchBook(ferrum.Model):
        id: int = 0
        author_id: int = 0
        title: str = ""
        author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="BenchAuthor", related_name="books", on_delete="CASCADE"
        )

        class Meta:
            table = names["books"]

    if names["vectors"]:

        class BenchVector(ferrum.Model):
            id: int = 0
            embedding: ferrum.Vector | None = ferrum.Field(vector_dimensions=4)

            class Meta:
                table = names["vectors"]

        return {
            "item": BenchItem,
            "author": BenchAuthor,
            "book": BenchBook,
            "vector": BenchVector,
        }
    return {"item": BenchItem, "author": BenchAuthor, "book": BenchBook}


# ---------------------------------------------------------------------------
# Workload benchmarks
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bench_env():
    """Function-scoped setup: open Ferrum + asyncpg connections, create schema.

    Reads ``FERRUM_TEST_DSN`` directly (not via the function-scoped ``pg_dsn``
    fixture) to avoid scope mismatch. Function-scoped so the asyncpg pool and
    Ferrum connection share the test's event loop (a module-scoped fixture
    pool would bind to a different loop than function-scoped tests, causing
    asyncpg ``InterfaceError``).
    """
    dsn = os.environ.get("FERRUM_TEST_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_DSN not set")
    pytest.importorskip("ferrum._native", reason="Rust extension not built — run `maturin develop`")
    suffix = os.urandom(4).hex()

    conn = ferrum.connection.Connection(dsn, min_size=2, max_size=8)
    await conn.open()
    await register_vector_codecs_safe(conn)
    names = await _setup_schema(conn, suffix)
    models = _make_models(names)
    pg_pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=8)
    enable_metrics()
    try:
        yield conn, pg_pool, names, models
    finally:
        await _teardown_schema(conn, names)
        await pg_pool.close()
        await conn.close()


async def register_vector_codecs_safe(conn: ferrum.connection.Connection) -> None:
    with contextlib.suppress(Exception):
        from ferrum.ext.pgvector import register_vector_codecs

        await register_vector_codecs(conn)


def _pool_acquired(conn: ferrum.connection.Connection) -> int:
    stats = conn.pool_stats()
    return stats.acquired if stats else -1


# ---------------------------------------------------------------------------
# Tests — each records Ferrum + asyncpg samples
# ---------------------------------------------------------------------------


async def test_bench_point_reads(bench_env) -> None:
    """Point read by PK — Ferrum .get vs asyncpg fetchrow."""
    conn, pg_pool, names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []
    mid = ROWS // 2

    async def ferrum_get() -> None:
        await Item.objects.get(conn, id=mid)

    async def asyncpg_get() -> None:
        async with pg_pool.acquire() as db:
            await db.fetchrow(f"SELECT * FROM {names['items']} WHERE id = $1", mid)

    _record_sample(
        await run_workload(
            "point_read", "ferrum", ferrum_get, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("point_read", "asyncpg", asyncpg_get), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_filtered_pages(bench_env) -> None:
    """Filtered page read — Ferrum .filter().all() vs asyncpg parameterized SELECT."""
    conn, pg_pool, names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []

    async def ferrum_page() -> None:
        await Item.objects.filter(active=True, value__gte=10).order_by("-value").limit(20).all(conn)

    sql = (
        f"SELECT * FROM {names['items']} WHERE active = $1 AND value >= $2 "
        f"ORDER BY value DESC LIMIT 20"
    )

    async def asyncpg_page() -> None:
        async with pg_pool.acquire() as db:
            await db.fetch(sql, True, 10)

    _record_sample(
        await run_workload(
            "filtered_page", "ferrum", ferrum_page, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("filtered_page", "asyncpg", asyncpg_page), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_relation_loads(bench_env) -> None:
    """Relation loads — Ferrum select_related vs asyncpg JOIN vs prefetch.

    ``select_related`` benchmarks the forward FK (Book→Author, to-one JOIN).
    ``prefetch_related`` benchmarks the reverse relation (Author→books, to-many
    via a second query) — prefetch is for to-many/M2M, not to-one FK.
    """
    conn, pg_pool, names, models = bench_env
    Book = models["book"]
    Author = models["author"]
    results: list[Sample] = []

    async def ferrum_select_related() -> None:
        await Book.objects.select_related("author").limit(50).all(conn)

    async def ferrum_prefetch() -> None:
        await Author.objects.prefetch_related("books").limit(50).all(conn)

    join_sql = (
        f"SELECT b.*, a.id AS a_id, a.name AS a_name FROM {names['books']} b "
        f"JOIN {names['authors']} a ON b.author_id = a.id LIMIT 50"
    )

    async def asyncpg_join() -> None:
        async with pg_pool.acquire() as db:
            await db.fetch(join_sql)

    _record_sample(
        await run_workload(
            "select_related",
            "ferrum",
            ferrum_select_related,
            pool_snapshot=lambda: _pool_acquired(conn),
        ),
        results,
    )
    _record_sample(
        await run_workload("prefetch_related", "ferrum", ferrum_prefetch),
        results,
    )
    _record_sample(await run_workload("select_related", "asyncpg", asyncpg_join), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_writes(bench_env) -> None:
    """Single-row writes — Ferrum .create vs asyncpg INSERT RETURNING."""
    conn, pg_pool, names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []
    counter = 0

    async def ferrum_create() -> None:
        nonlocal counter
        counter += 1
        await Item.objects.create(conn, label=f"new-{counter}", value=counter, active=True)

    insert_sql = (
        f"INSERT INTO {names['items']} (label, value, active) VALUES ($1, $2, $3) RETURNING id"
    )

    async def asyncpg_insert() -> None:
        nonlocal counter
        counter += 1
        async with pg_pool.acquire() as db:
            await db.fetchval(insert_sql, f"new-{counter}", counter, True)

    _record_sample(
        await run_workload(
            "write", "ferrum", ferrum_create, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("write", "asyncpg", asyncpg_insert), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_bulk(bench_env) -> None:
    """Bulk operations — Ferrum bulk_create vs asyncpg multi-row INSERT."""
    conn, pg_pool, names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []
    batch = 50

    async def ferrum_bulk_create() -> None:
        objs = [Item(label=f"bulk-{i}", value=i, active=True) for i in range(batch)]
        await Item.objects.bulk_create(conn, objs, batch_size=batch, returning=False)

    # Build a multi-row INSERT with $1..$N placeholders.
    cols = 3
    placeholders = ", ".join(
        f"({', '.join(f'${c + 1 + r * cols}' for c in range(cols))})" for r in range(batch)
    )
    insert_sql = f"INSERT INTO {names['items']} (label, value, active) VALUES {placeholders}"
    bulk_counter = 0

    async def asyncpg_bulk_insert() -> None:
        nonlocal bulk_counter
        bulk_counter += 1
        params = []
        for i in range(batch):
            params.extend([f"bulk-a-{bulk_counter}-{i}", i, True])
        async with pg_pool.acquire() as db:
            await db.execute(insert_sql, *params)

    _record_sample(
        await run_workload(
            "bulk_create", "ferrum", ferrum_bulk_create, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("bulk_create", "asyncpg", asyncpg_bulk_insert), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_jsonb(bench_env) -> None:
    """JSONB reads — Ferrum .filter(payload__contains=...) vs asyncpg @> operator."""
    conn, pg_pool, names, _models = bench_env
    results: list[Sample] = []

    # Ferrum JSONB model — dict maps to JSONB, list[str] to text[].
    class Doc(ferrum.Model):
        id: int = 0
        payload: dict = ferrum.Field(default_factory=dict)
        tags: list[str] = ferrum.Field(default_factory=list)

        class Meta:
            table = names["docs"]

    async def ferrum_jsonb() -> None:
        await Doc.objects.filter(payload__contains={"cat": "doc"}).limit(20).all(conn)

    jsonb_sql = f"SELECT * FROM {names['docs']} WHERE payload @> $1::jsonb LIMIT 20"

    async def asyncpg_jsonb() -> None:
        async with pg_pool.acquire() as db:
            await db.fetch(jsonb_sql, json.dumps({"cat": "doc"}))

    _record_sample(
        await run_workload(
            "jsonb_read", "ferrum", ferrum_jsonb, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("jsonb_read", "asyncpg", asyncpg_jsonb), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_vector_knn(bench_env) -> None:
    """Vector KNN — Ferrum nearest_to vs asyncpg ORDER BY <=> LIMIT."""
    conn, pg_pool, names, models = bench_env
    if not names["vectors"]:
        pytest.skip("pgvector not available")
    Vec = models["vector"]
    results: list[Sample] = []
    query_vec = [0.0, 0.1, 0.2, 0.3]

    async def ferrum_knn() -> None:
        await Vec.objects.nearest_to("embedding", query_vec).limit(10).all(conn)

    knn_sql = f"SELECT * FROM {names['vectors']} ORDER BY embedding <=> $1::vector LIMIT 10"

    async def asyncpg_knn() -> None:
        async with pg_pool.acquire() as db:
            await db.fetch(knn_sql, f"[{','.join(str(v) for v in query_vec)}]")

    _record_sample(
        await run_workload(
            "vector_knn", "ferrum", ferrum_knn, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("vector_knn", "asyncpg", asyncpg_knn), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_streaming(bench_env) -> None:
    """Streaming — Ferrum .stream() vs asyncpg cursor."""
    conn, pg_pool, names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []

    async def ferrum_stream() -> None:
        async with Item.objects.stream(conn, chunk_size=100) as chunks:
            async for chunk in chunks:
                _ = len(chunk)

    stream_sql = f"SELECT * FROM {names['items']}"

    async def asyncpg_stream() -> None:
        async with pg_pool.acquire() as db, db.transaction():
            cur = await db.cursor(stream_sql)
            while True:
                rows = await cur.fetch(100)
                if not rows:
                    break

    _record_sample(
        await run_workload(
            "streaming", "ferrum", ferrum_stream, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("streaming", "asyncpg", asyncpg_stream), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_transactions(bench_env) -> None:
    """Transactions — Ferrum conn.transaction() vs asyncpg pool.transaction()."""
    conn, pg_pool, names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []
    tx_counter = 0

    async def ferrum_tx() -> None:
        nonlocal tx_counter
        tx_counter += 1
        async with conn.transaction() as tx:
            await Item.objects.filter(id=tx_counter).update(tx, active=False)

    update_sql = f"UPDATE {names['items']} SET active = $2 WHERE id = $1"

    async def asyncpg_tx() -> None:
        nonlocal tx_counter
        tx_counter += 1
        async with pg_pool.acquire() as db, db.transaction():
            await db.execute(update_sql, tx_counter, False)

    _record_sample(
        await run_workload(
            "transaction", "ferrum", ferrum_tx, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )
    _record_sample(await run_workload("transaction", "asyncpg", asyncpg_tx), results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


async def test_bench_wire_format_boundary(bench_env) -> None:
    """Profile JSON vs msgpack PyO3 wire-format boundary.

    Toggles ``FERRUM_WIRE_FORMAT`` between ``json`` (default) and ``msgpack``
    and compares hydration throughput on an identical read workload. The
    boundary copy cost is the throwaway serialization for Rust structural
    validation (AGENTS.md: ``_RowEncoder`` / ``_msgpack_row_default``).
    """
    conn, _pg_pool, _names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []

    async def ferrum_read_json() -> None:
        await Item.objects.filter(active=True).limit(50).all(conn)

    # Measure msgpack wire format by setting the env var for the read path.
    # The wire format is read at connection-config time; we re-run reads
    # under a process where msgpack is enabled via a fresh connection.
    _record_sample(
        await run_workload(
            "wire_json", "ferrum", ferrum_read_json, pool_snapshot=lambda: _pool_acquired(conn)
        ),
        results,
    )

    # msgpack path: open a separate connection with msgpack wire format.
    msgpack_dsn = os.environ["FERRUM_TEST_DSN"]
    try:
        async with ferrum.connect(msgpack_dsn, min_size=1, max_size=4) as mp_conn:
            os.environ["FERRUM_WIRE_FORMAT"] = "msgpack"

            async def ferrum_read_msgpack() -> None:
                await Item.objects.filter(active=True).limit(50).all(mp_conn)

            _record_sample(
                await run_workload("wire_msgpack", "ferrum", ferrum_read_msgpack),
                results,
            )
    finally:
        os.environ.pop("FERRUM_WIRE_FORMAT", None)

    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


# ---------------------------------------------------------------------------
# SQLAlchemy async comparison (optional — skips when not installed)
# ---------------------------------------------------------------------------


async def test_bench_sqlalchemy_comparison(bench_env) -> None:
    """SQLAlchemy async comparison — skipped when SQLAlchemy is not installed.

    Uses a lightweight Core-level asyncpg-backed engine on the same schema so
    the comparison is apples-to-apples with raw asyncpg (no ORM unit-of-work
    overhead, just Core execution). This is the fair baseline for an ORM-core
    comparison.
    """
    if not _HAS_SQLALCHEMY:
        pytest.skip("SQLAlchemy not installed — install to enable comparison")
    conn, _pg_pool, names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []
    mid = ROWS // 2

    # SQLAlchemy Core with asyncpg dialect.
    dsn = os.environ["FERRUM_TEST_DSN"].replace("postgres://", "postgresql+asyncpg://")
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://")
    engine: AsyncEngine = create_async_engine(dsn, pool_size=4, max_overflow=4)

    from sqlalchemy import text

    sa_sql = text(f"SELECT * FROM {names['items']} WHERE id = :mid")

    async def sa_get() -> None:
        async with engine.connect() as sa_conn:
            await sa_conn.execute(sa_sql, {"mid": mid})

    try:
        _record_sample(
            await run_workload(
                "point_read",
                "ferrum",
                lambda: Item.objects.get(conn, id=mid),
                pool_snapshot=lambda: _pool_acquired(conn),
            ),
            results,
        )
        _record_sample(await run_workload("point_read", "sqlalchemy", sa_get), results)
        _write_results(results)
        _compare_baseline(results)
        _assert_no_crash(results)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Compile / hydration split — Rust-side metrics via observability counters
# ---------------------------------------------------------------------------


async def test_bench_compile_hydration_split(bench_env) -> None:
    """Compile/hydration split — derive from observability metric counters.

    Ferrum's hooks emit ``ferrum.query.duration_ms.*`` counters. By toggling
    metrics on/off around a read workload and snapshotting the gauges, we
    capture the aggregate query-path cost. The compile/hydration split itself
    is measured at the Rust layer by the Criterion benches (hydrate.rs /
    compile.rs); here we record the Python-observable query-path totals so the
    split is reportable alongside the end-to-end numbers.
    """
    conn, _pg_pool, _names, models = bench_env
    Item = models["item"]
    results: list[Sample] = []
    reset_metrics()

    async def ferrum_read() -> None:
        await Item.objects.filter(active=True).limit(50).all(conn)

    sample = await run_workload(
        "compile_hydration_split", "ferrum", ferrum_read, pool_snapshot=lambda: _pool_acquired(conn)
    )
    metrics = get_metrics()
    sample.extra["metric_query_count"] = metrics.get(
        "ferrum.query.count{operation=select,model=BenchItem}", 0.0
    )
    sample.extra["metric_duration_ms_sum"] = metrics.get(
        "ferrum.query.duration_ms.sum{operation=select,model=BenchItem}", 0.0
    )
    _record_sample(sample, results)
    _write_results(results)
    _compare_baseline(results)
    _assert_no_crash(results)


# ---------------------------------------------------------------------------
# Guard — never hard-fail on a regression (no flaky gates)
# ---------------------------------------------------------------------------


def _assert_no_crash(results: list[Sample]) -> None:
    """Soft assertion: every implementation completed with nonzero throughput.

    This is the only hard check — a workload that crashed or produced zero
    throughput is a real failure, not a variance issue. Latency regressions
    are reported via ``_compare_baseline`` but never fail the test.
    """
    for s in results:
        assert s.throughput_ops_per_s > 0, (
            f"{s.workload}/{s.implementation} produced zero throughput — crash?"
        )
        assert s.p50_us > 0, f"{s.workload}/{s.implementation} produced zero p50"
