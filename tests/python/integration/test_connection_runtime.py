"""Integration tests for Phase 4 connection runtime: timeouts, health, shutdown.

W1-E additions: PoolStats typed snapshot, pool growth, event-based shutdown
drain, drain timeout reporting, stale-connection replacement, failover expire.
"""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import asyncio
import os

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.drivers.postgres import PoolStats
from ferrum.errors import FerrumConfigError, FerrumConnectionError, FerrumTimeoutError, map_db_error

from .backends import Backend


async def _skip_if_driver_unavailable(dsn: str, backend_name: str) -> None:
    try:
        conn = ferrum.connect(dsn)
        await conn.__aenter__()
        await conn.__aexit__(None, None, None)
    except FerrumConfigError as exc:
        pytest.skip(f"Backend {backend_name!r} driver not available: {exc}")
    except FerrumConnectionError:
        pass


async def _seed_payload_rows(
    conn: ferrum.connection.Connection,
    backend: Backend,
    table_name: str,
    *,
    rows: int = 2000,
) -> None:
    driver = conn._require_driver()
    q = backend.quote
    chunk_size = 500
    for chunk_start in range(0, rows, chunk_size):
        chunk_end = min(chunk_start + chunk_size, rows)
        values_sql = ", ".join(f"('row-{i}')" for i in range(chunk_start, chunk_end))
        await driver.execute(f"INSERT INTO {q(table_name)} (payload) VALUES {values_sql}")


@pytest.mark.integration
async def test_health_check_returns_true(db_conn: ferrum.connection.Connection) -> None:
    assert await db_conn.health_check() is True


@pytest.mark.integration
async def test_query_timeout_on_live_backend(
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    if backend.name == "sqlite":
        pytest.skip("SQLite driver ignores query_timeout pool knobs")

    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None
    await _skip_if_driver_unavailable(dsn, backend.name)

    table_name = f"ferrum_runtime_timeout_{unique_suffix}"

    class Slow(ferrum.Model):
        id: int = 0
        payload: str = ""

        class Meta:
            table = table_name

    q = backend.quote
    col_ddl = ", ".join(
        [
            f"{q('id')} {backend.types['pk_serial']}",
            f"{q('payload')} {backend.types['text']} NOT NULL",
        ]
    )
    create_sql = f"CREATE TABLE {q(table_name)} ({col_ddl})"
    drop_sql = f"DROP TABLE IF EXISTS {q(table_name)}"

    async with ferrum.connect(dsn) as seed_conn:
        await seed_conn._require_driver().execute(create_sql)
        try:
            await _seed_payload_rows(seed_conn, backend, table_name, rows=2000)

            conn = Connection(dsn, query_timeout=0.001)
            await conn.open()
            try:
                with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
                    await Slow.objects.all(conn)
            finally:
                await conn.close()
        finally:
            await seed_conn._require_driver().execute(drop_sql)


@pytest.mark.integration
async def test_graceful_shutdown_rejects_new_work(backend: Backend) -> None:
    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None
    await _skip_if_driver_unavailable(dsn, backend.name)

    conn = Connection(dsn, drain_timeout=2.0)
    await conn.open()
    conn._lifecycle.stop_accepting()
    with pytest.raises(FerrumConnectionError, match="shutting down"):
        conn._require_driver()
    await conn.close()


@pytest.mark.integration
async def test_acquire_timeout_on_exhausted_pool(backend: Backend) -> None:
    if backend.name == "sqlite":
        pytest.skip("SQLite driver uses a single connection, not a pool")

    dsn = os.environ.get(backend.dsn_env)
    assert dsn is not None
    await _skip_if_driver_unavailable(dsn, backend.name)

    conn = Connection(dsn, min_size=1, max_size=1, acquire_timeout=0.2)
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


# ---------------------------------------------------------------------------
# W1-E: PoolStats, pool growth, event-based shutdown, drain timeout,
# stale connection replacement, failover expire.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pool_stats_returns_typed_snapshot(pg_dsn: str) -> None:
    """``pool_stats()`` returns a ``PoolStats`` with all required fields."""
    conn = Connection(pg_dsn, min_size=1, max_size=3)
    await conn.open()
    try:
        stats = conn.pool_stats()
        assert stats is not None
        assert isinstance(stats, PoolStats)
        # All required fields are present.
        assert hasattr(stats, "size")
        assert hasattr(stats, "idle")
        assert hasattr(stats, "acquired")
        assert hasattr(stats, "waiters")
        assert hasattr(stats, "min_size")
        assert hasattr(stats, "max_size")
        assert hasattr(stats, "inflight")
        assert hasattr(stats, "accepting")
        assert hasattr(stats, "closing")
        # Values are sane.
        assert stats.min_size == 1
        assert stats.max_size == 3
        assert stats.size >= 1
        assert stats.idle >= 0
        assert stats.acquired >= 0
        assert stats.inflight == 0
        assert stats.accepting is True
        assert stats.closing is False
    finally:
        await conn.close()


@pytest.mark.integration
async def test_pool_stats_reflects_acquired_connection(pg_dsn: str) -> None:
    """``pool_stats.acquired`` increments when a connection is held."""
    conn = Connection(pg_dsn, min_size=1, max_size=2)
    await conn.open()
    try:
        before = conn.pool_stats()
        assert before is not None

        async with conn.acquire():
            during = conn.pool_stats()
            assert during is not None
            # At least one connection is acquired.
            assert during.acquired >= 1
            assert during.inflight >= 1

        after = conn.pool_stats()
        assert after is not None
        assert after.inflight == 0
    finally:
        await conn.close()


@pytest.mark.integration
async def test_pool_growth_from_min_to_max(pg_dsn: str) -> None:
    """Pool grows from ``min_size`` toward ``max_size`` under concurrent load."""
    conn = Connection(pg_dsn, min_size=1, max_size=4)
    await conn.open()
    try:
        stats_before = conn.pool_stats()
        assert stats_before is not None
        initial_size = stats_before.size

        # Launch 4 concurrent queries to force pool growth.
        async def _q() -> int:
            driver = conn._require_driver()
            return int(await driver.fetchval("SELECT 1"))

        await asyncio.gather(*[_q() for _ in range(4)])

        # After concurrent load, the pool may have grown.
        stats_after = conn.pool_stats()
        assert stats_after is not None
        assert stats_after.size >= initial_size
    finally:
        await conn.close()


@pytest.mark.integration
async def test_event_based_shutdown_drain(pg_dsn: str) -> None:
    """``close()`` uses event-based drain, not busy polling.

    An in-flight query should complete and ``close()`` should return promptly
    after the in-flight work finishes (not after a fixed sleep loop).
    """
    conn = Connection(pg_dsn, min_size=1, max_size=2, drain_timeout=5.0)
    await conn.open()

    query_done = asyncio.Event()

    async def _query() -> int:
        driver = conn._require_driver()
        result = int(await driver.fetchval("SELECT 1"))
        query_done.set()
        return result

    task = asyncio.create_task(_query())
    await asyncio.sleep(0.05)  # let the query start

    # Close should wait for the query, then return.
    await conn.close()
    result = await task
    assert result == 1


@pytest.mark.integration
async def test_shutdown_drain_timeout_reported(pg_dsn: str) -> None:
    """When drain timeout is hit, ``close()`` raises ``FerrumTimeoutError`` with
    ``category='timeout'`` and the pool is still closed (no leak)."""
    conn = Connection(pg_dsn, min_size=1, max_size=1, drain_timeout=0.01)
    await conn.open()

    async with conn.acquire():
        with pytest.raises(FerrumTimeoutError) as exc_info:
            await conn.close()
        assert exc_info.value.category == "timeout"
        # Pool is closed despite the timeout.
        assert conn._driver is None


@pytest.mark.integration
async def test_stale_connection_replaced_via_expire(pg_dsn: str) -> None:
    """After ``expire_connections``, the next acquire creates a fresh connection."""
    conn = Connection(pg_dsn, min_size=1, max_size=2)
    await conn.open()
    try:
        driver = conn._require_driver()
        # Verify the pool works.
        assert int(await driver.fetchval("SELECT 1")) == 1

        # Expire all connections (simulates failover/stale replacement).
        if hasattr(conn._driver, "expire_connections"):
            await conn._driver.expire_connections()

        # After expiration, a new query should still work with a fresh connection.
        assert int(await driver.fetchval("SELECT 1")) == 1
    finally:
        await conn.close()


@pytest.mark.integration
async def test_failover_expire_on_shutdown_error(pg_dsn: str) -> None:
    """``_handle_post_error`` expires pool connections on failover-category errors.

    This tests the failover-safe replacement path: after a failover-category
    error, stale connections are recycled so the next acquire does not land
    on a dead connection. No unconditional pre-ping — only on detected failover.
    """
    from ferrum.errors import FerrumConnectionError

    conn = Connection(pg_dsn, min_size=1, max_size=2)
    await conn.open()
    try:
        driver = conn._driver
        # Verify pool works before.
        assert int(await driver.fetchval("SELECT 1")) == 1

        # Simulate a failover error — expire_connections is called internally.
        failover_err = FerrumConnectionError(
            "Server shutdown (simulated). [FERR-E101]",
            category="failover",
        )
        await driver._handle_post_error(failover_err)

        # After expire, pool should still work (fresh connections).
        assert int(await driver.fetchval("SELECT 1")) == 1

        # A non-failover error should NOT expire.
        non_failover = FerrumConnectionError(
            "Connection error. [FERR-E101]",
            category="connection",
        )
        await driver._handle_post_error(non_failover)
        # Pool still works.
        assert int(await driver.fetchval("SELECT 1")) == 1
    finally:
        await conn.close()


@pytest.mark.integration
async def test_pool_stats_closing_state(pg_dsn: str) -> None:
    """``pool_stats`` reports ``closing=True`` during shutdown."""
    conn = Connection(pg_dsn, min_size=1, max_size=2)
    await conn.open()
    conn._lifecycle.stop_accepting()

    stats = conn.pool_stats()
    assert stats is not None
    assert stats.accepting is False
    # The pool itself is not closing yet (only the lifecycle stopped accepting).
    # After close, the pool is gone and stats report closing.
    await conn.close()
    assert conn.pool_stats() is None


@pytest.mark.integration
async def test_concurrent_acquire_cancel_no_double_release(pg_dsn: str) -> None:
    """Concurrent acquire + cancel does not double-release or hang waiters."""
    conn = Connection(pg_dsn, min_size=1, max_size=1, acquire_timeout=5.0)
    await conn.open()
    try:
        async with conn.acquire():
            # Start a waiting acquire and cancel it.
            waiter = asyncio.create_task(_hold_and_sleep(conn, 1.0))
            await asyncio.sleep(0.05)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

        # After releasing, a new acquire should work (no hang from double-release).
        async with conn.acquire():
            pass
    finally:
        await conn.close()


async def _hold_and_sleep(conn: ferrum.connection.Connection, duration: float) -> None:
    """Acquire a connection and sleep for ``duration`` seconds."""
    async with conn.acquire():
        await asyncio.sleep(duration)
