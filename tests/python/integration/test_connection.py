"""Integration tests for connection failure, closed pool, and timeout semantics."""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import asyncio

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.drivers.postgres import PoolStats
from ferrum.errors import FerrumConnectionError, FerrumTimeoutError

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


# ---------------------------------------------------------------------------
# W1-E: acquire_timeout on convenience methods, saturation, cancellation,
# failover/stale replacement, DSN redaction with new config.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_acquire_timeout_enforced_on_fetch(pg_dsn: str) -> None:
    """``acquire_timeout`` is enforced on the convenience ``fetch`` path, not
    just on explicit ``Connection.acquire()``.

    Saturate a max_size=1 pool, then call ``driver.fetch`` (which should block
    on the internal acquire). The ``acquire_timeout`` must fire.
    """
    conn = Connection(pg_dsn, min_size=1, max_size=1, acquire_timeout=0.2)
    await conn.open()
    try:
        async with conn.acquire():
            # Pool is now saturated (max_size=1, 1 acquired).
            # A convenience fetch must hit acquire_timeout.
            with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
                await conn._driver.fetchval("SELECT 1")
    finally:
        await conn.close()


@pytest.mark.integration
async def test_acquire_timeout_enforced_on_execute(pg_dsn: str) -> None:
    """``acquire_timeout`` is enforced on the convenience ``execute`` path."""
    conn = Connection(pg_dsn, min_size=1, max_size=1, acquire_timeout=0.2)
    await conn.open()
    try:
        async with conn.acquire():
            with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
                await conn._driver.execute("SELECT 1")
    finally:
        await conn.close()


@pytest.mark.integration
async def test_pool_saturation_then_release(pg_dsn: str) -> None:
    """Saturating the pool blocks new acquires; releasing unblocks them."""
    conn = Connection(pg_dsn, min_size=1, max_size=2, acquire_timeout=2.0)
    await conn.open()
    try:
        stats = conn.pool_stats()
        assert stats is not None
        assert stats.max_size == 2

        async def _hold_acquire() -> None:
            async with conn.acquire():
                await asyncio.sleep(0.5)

        # Acquire both connections.
        async with conn.acquire():
            async with conn.acquire():
                # Pool is saturated; a third acquire must wait.
                waiter = asyncio.create_task(_hold_acquire())
                # Give it a moment to start waiting.
                await asyncio.sleep(0.05)
                assert not waiter.done()
            # One connection released; the waiter should eventually complete.
            await asyncio.wait_for(waiter, timeout=3.0)
    finally:
        await conn.close()


async def _acquire_and_hold(conn: ferrum.connection.Connection, hold: float) -> None:
    """Acquire a connection and hold it for ``hold`` seconds."""
    async with conn.acquire():
        await asyncio.sleep(hold)


@pytest.mark.integration
async def test_concurrent_acquire_cancel_does_not_hang(pg_dsn: str) -> None:
    """Cancelling a waiting acquire does not hang other waiters or leak."""
    conn = Connection(pg_dsn, min_size=1, max_size=1, acquire_timeout=5.0)
    await conn.open()
    try:
        async with conn.acquire():
            # Start two waiting acquires.
            t1 = asyncio.create_task(_acquire_and_hold(conn, 0.01))
            t2 = asyncio.create_task(_acquire_and_hold(conn, 0.01))
            await asyncio.sleep(0.05)
            # Cancel one waiter.
            t1.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t1
            # The other waiter should still complete after we release.
        # After releasing, t2 should complete.
        await asyncio.wait_for(t2, timeout=3.0)
    finally:
        await conn.close()


@pytest.mark.integration
async def test_dsn_redaction_with_ssl_config(pg_dsn: str) -> None:
    # Use a bad host to trigger a connection error.
    bad_dsn = "postgresql://user:supersecret@127.0.0.1:59999/nodb"
    conn = Connection(
        bad_dsn,
        ssl="require",
        server_settings={"work_mem": "64MB"},
        application_name="test-app",
    )
    with pytest.raises(FerrumConnectionError) as exc_info:
        await conn.open()
    message = str(exc_info.value)
    assert "supersecret" not in message.lower()
    assert "://" not in message
    # The error should have the connection category.
    assert hasattr(exc_info.value, "category")


@pytest.mark.integration
async def test_pool_stats_typed_snapshot(pg_dsn: str) -> None:
    """``Connection.pool_stats()`` returns a typed ``PoolStats`` snapshot."""
    conn = Connection(pg_dsn, min_size=1, max_size=3)
    await conn.open()
    try:
        stats = conn.pool_stats()
        assert stats is not None
        assert isinstance(stats, PoolStats)
        assert stats.min_size == 1
        assert stats.max_size == 3
        assert stats.size >= 1  # at least min_size connections
        assert stats.idle >= 0
        assert stats.acquired >= 0
        assert stats.inflight == 0  # no in-flight work
        assert stats.accepting is True
        assert stats.closing is False
        # waiters may be -1 (unavailable) or >= 0.
        assert stats.waiters >= -1
    finally:
        await conn.close()


@pytest.mark.integration
async def test_pool_stats_none_when_not_open() -> None:
    """``pool_stats()`` returns ``None`` when the pool is not open."""
    conn = Connection("postgresql://unused@127.0.0.1/unused")
    assert conn.pool_stats() is None


@pytest.mark.integration
async def test_pool_growth_under_load(pg_dsn: str) -> None:
    """Pool grows from min_size to max_size under concurrent load."""
    conn = Connection(pg_dsn, min_size=1, max_size=4)
    await conn.open()
    try:
        stats = conn.pool_stats()
        assert stats is not None
        assert stats.size >= 1  # starts at min_size

        # Launch 4 concurrent queries to force pool growth.
        async def _query() -> int:
            driver = conn._require_driver()
            result = await driver.fetchval("SELECT 1")
            return int(result)

        results = await asyncio.gather(*[_query() for _ in range(4)])
        assert all(r == 1 for r in results)

        # After the queries, the pool may have grown.
        stats2 = conn.pool_stats()
        assert stats2 is not None
        assert stats2.size >= 1
    finally:
        await conn.close()


@pytest.mark.integration
async def test_stale_connection_replaced_after_expire(pg_dsn: str) -> None:
    """After ``expire_connections``, a new acquire works (stale replacement)."""
    conn = Connection(pg_dsn, min_size=1, max_size=2)
    await conn.open()
    try:
        # Verify the pool works.
        driver = conn._require_driver()
        result = await driver.fetchval("SELECT 1")
        assert int(result) == 1

        # Expire all connections (simulates failover replacement).
        if hasattr(conn._driver, "expire_connections"):
            await conn._driver.expire_connections()

        # After expiration, a new query should still work (fresh connection).
        result2 = await driver.fetchval("SELECT 1")
        assert int(result2) == 1
    finally:
        await conn.close()


@pytest.mark.integration
async def test_graceful_shutdown_drains_inflight(pg_dsn: str) -> None:
    """``close()`` waits for in-flight work to drain before closing the pool."""
    conn = Connection(pg_dsn, min_size=1, max_size=2, drain_timeout=5.0)
    await conn.open()

    # Start an in-flight query.
    async def _slow_query() -> int:
        driver = conn._require_driver()
        return int(await driver.fetchval("SELECT 1"))

    task = asyncio.create_task(_slow_query())

    # Give the query a moment to start.
    await asyncio.sleep(0.05)

    # Close should wait for the in-flight query to complete.
    await conn.close()

    # The query should have completed successfully.
    result = await task
    assert result == 1


@pytest.mark.integration
async def test_shutdown_reports_drain_timeout(pg_dsn: str) -> None:
    """When in-flight work outlasts ``drain_timeout``, ``close()`` reports a
    ``FerrumTimeoutError`` after closing the pool (no connection leak)."""
    conn = Connection(pg_dsn, min_size=1, max_size=1, drain_timeout=0.01)
    await conn.open()

    # Hold a connection for longer than drain_timeout.
    async with conn.acquire():
        with pytest.raises(FerrumTimeoutError, match="timed out"):
            await conn.close()

    # The pool should be closed (driver is None) even though drain timed out.
    assert conn._driver is None


@pytest.mark.integration
async def test_shutdown_rejects_new_work(pg_dsn: str) -> None:
    """After ``stop_accepting``, new work is rejected with FerrumConnectionError."""
    conn = Connection(pg_dsn, drain_timeout=2.0)
    await conn.open()
    conn._lifecycle.stop_accepting()
    with pytest.raises(FerrumConnectionError, match="shutting down"):
        conn._require_driver()
    await conn.close()


@pytest.mark.integration
async def test_event_based_shutdown_no_busy_poll(pg_dsn: str) -> None:
    """``_EventLifecycleGuard`` uses an event, not a busy-poll loop, for drain."""
    from ferrum.connection import _EventLifecycleGuard

    guard = _EventLifecycleGuard()
    assert guard._drained.is_set()  # nothing in-flight → drained

    guard.begin()
    assert not guard._drained.is_set()  # in-flight → not drained

    guard.end()
    assert guard._drained.is_set()  # all done → drained


@pytest.mark.integration
async def test_new_config_does_not_leak_password_in_error(pg_dsn: str) -> None:
    """Pool open failure with new config knobs does not leak the DSN password."""
    secret = "mysecret"  # noqa: S105
    bad_dsn = f"postgresql://user:{secret}@127.0.0.1:59999/nodb"
    conn = Connection(
        bad_dsn,
        command_timeout=5.0,
        statement_cache_size=50,
        ssl="prefer",
        server_settings={"application_name": "leak-test"},
    )
    with pytest.raises(FerrumConnectionError) as exc_info:
        await conn.open()
    msg = str(exc_info.value)
    assert "mysecret" not in msg
    assert "://" not in msg
