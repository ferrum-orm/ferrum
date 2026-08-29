"""Integration tests for pool exhaustion and concurrent QuerySet execution."""

from __future__ import annotations

import asyncio
import os

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.errors import FerrumConfigError, FerrumTimeoutError

from .backends import Backend, Capability
from .schema import Column, transient_table


async def _skip_if_driver_unavailable(dsn: str, backend_name: str) -> None:
    try:
        conn = ferrum.connect(dsn)
        await conn.__aenter__()
        await conn.__aexit__(None, None, None)
    except FerrumConfigError as exc:
        pytest.skip(f"Backend {backend_name!r} driver not available: {exc}")


@pytest.mark.integration
async def test_pool_exhaustion_blocks_until_timeout(
    backend: Backend,
) -> None:
    """When all pool slots are held, acquire waits until the configured timeout."""
    if backend.name == "sqlite":
        pytest.skip("SQLite driver uses a single connection, not a pool")

    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None
    await _skip_if_driver_unavailable(dsn, backend.name)

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
@pytest.mark.requires_capability(Capability.TRANSACTIONS)
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
@pytest.mark.requires_capability(Capability.TRANSACTIONS)
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


# ---------------------------------------------------------------------------
# select_for_update concurrent locking (§5a — live PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_select_for_update_blocks(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Two concurrent FOR UPDATE on the same row: second blocks until first commits."""
    from .backends import POSTGRES
    from .schema import Column, transient_table

    table_name = f"ferrum_int_concurrent_for_update_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        name: str = ""
        balance: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        pg_conn,
        table_name,
        backend=POSTGRES,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
            Column("balance", "int", null=False, default="0"),
        ],
    ):
        await Item.objects.create(pg_conn, name="item", balance=100)

        holder_done = asyncio.Event()
        second_read_order: list[int] = []

        async def hold_and_update() -> None:
            async with pg_conn.transaction() as tx:
                rows = await Item.objects.filter(name="item").select_for_update().all(tx)
                assert len(rows) == 1
                row = rows[0]
                row.balance = 200
                await Item.objects.filter(id=row.id).update(tx, balance=200)
                await asyncio.sleep(0.2)
            holder_done.set()

        async def read_after() -> None:
            await asyncio.sleep(0.05)  # let holder acquire the lock first
            async with pg_conn.transaction() as tx:
                rows = await Item.objects.filter(name="item").select_for_update().all(tx)
                second_read_order.append(rows[0].balance)

        hold_task = asyncio.ensure_future(hold_and_update())
        read_task = asyncio.ensure_future(read_after())
        await asyncio.gather(hold_task, read_task)
        assert holder_done.is_set()
        # The second read should see the committed balance (200), proving it
        # blocked until the holder committed.
        assert second_read_order == [200]


@pytest.mark.integration
async def test_concurrent_advisory_lock_exclusive(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    """Concurrent advisory_xact_lock on the same key: second attempt fails via try_lock."""
    key = 12345
    holder_done = asyncio.Event()

    async def hold_lock() -> None:
        async with pg_conn.transaction() as tx:
            await tx.advisory_xact_lock(key)
            await holder_done.wait()
        holder_done.set()

    async def try_lock_while_held() -> bool:
        await asyncio.sleep(0.05)  # let the holder acquire first
        async with pg_conn.transaction() as tx:
            acquired = await tx.advisory_try_xact_lock(key)
        holder_done.set()  # release the holder
        return acquired

    hold_task = asyncio.ensure_future(hold_lock())
    try_result = await try_lock_while_held()
    await hold_task
    assert try_result is False


@pytest.mark.integration
async def test_concurrent_advisory_lock_no_leak(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    """Advisory locks do not leak connections after release."""
    stats_before = pg_conn.pool_stats()

    async with pg_conn.advisory_lock(42) as locked:
        await locked._require_driver().fetchval("SELECT 1")

    stats_after = pg_conn.pool_stats()
    if stats_before is not None and stats_after is not None:
        assert stats_after.acquired == stats_before.acquired
        assert stats_after.inflight == stats_before.inflight


@pytest.mark.integration
async def test_run_transaction_replay_no_leak(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """run_transaction with replay does not leak connections."""
    from ferrum.runtime import TransactionRetryPolicy

    from .backends import POSTGRES
    from .schema import Column, transient_table

    table_name = f"ferrum_int_concurrent_run_tx_{unique_suffix}"

    class Counter(ferrum.Model):
        id: int = 0
        value: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        pg_conn,
        table_name,
        backend=POSTGRES,
        columns=[
            Column("id", "pk_serial"),
            Column("value", "int", null=False, default="0"),
        ],
    ):
        stats_before = pg_conn.pool_stats()
        calls = 0

        async def fn(tx: ferrum.Transaction) -> int:
            nonlocal calls
            calls += 1
            await Counter.objects.create(tx, value=calls)
            return calls

        result = await pg_conn.run_transaction(
            fn, retry=TransactionRetryPolicy(max_attempts=3, backoff_base=0.0)
        )
        assert result == 1
        assert calls == 1

        stats_after = pg_conn.pool_stats()
        if stats_before is not None and stats_after is not None:
            assert stats_after.acquired == stats_before.acquired
            assert stats_after.inflight == stats_before.inflight
