"""Integration tests for W1-F: RLS, platform admin, schema tenancy, and shards.

Live PostgreSQL only. Covers the ratified AGENTS.md §5a contract:
- platform_admin_transaction sets only an admin GUC (no fake tenant).
- schema_transaction sets a transaction-local search_path and resets it.
- GUC/search_path reset on rollback and on pool reuse.
- Cancellation does not leak GUC or search_path onto a pooled connection.
- Cross-tenant and cross-schema isolation.
- ConnectionRegistry/ShardRouter over independent PostgreSQL pools with
  bounded parallel startup/health/close and per-shard stats.
- mysql/sqlite/mssql DSNs are rejected by the registry.
"""

# ruff: noqa: S608 — identifiers are test-controlled, never user input.

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumConfigError, FerrumConnectionError
from ferrum.routing import ConnectionRegistry, PoolConfig, ShardRouter
from ferrum.session import (
    ALLOWED_SCHEMA_NAMES,
    current_setting,
    platform_admin_transaction,
    schema_transaction,
    tenant_transaction,
)

pytestmark = pytest.mark.integration


async def _fetchval(conn: ferrum.connection.Connection, sql: str, *args: Any) -> Any:
    """Run a raw scalar query on the pool (acquires a pooled connection)."""
    return await conn._require_driver().fetchval(sql, *args)


async def _execute(conn: ferrum.connection.Connection, sql: str, *args: Any) -> None:
    await conn._require_driver().execute(sql, *args)


# ---------------------------------------------------------------------------
# platform_admin_transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_admin_sets_admin_guc_no_tenant(
    pg_conn: ferrum.connection.Connection,
) -> None:
    async with platform_admin_transaction(pg_conn) as tx:
        admin = await current_setting(tx, "app.platform_admin")
        assert admin == "true"
        # No tenant-id GUC was set.
        tenant = await current_setting(tx, "app.team_id")
        assert tenant is None


@pytest.mark.asyncio
async def test_platform_admin_guc_resets_after_commit(
    pg_conn: ferrum.connection.Connection,
) -> None:
    async with platform_admin_transaction(pg_conn):
        pass  # commits on clean exit
    leaked = await _fetchval(pg_conn, "SELECT current_setting('app.platform_admin', true)")
    assert leaked in (None, "")


@pytest.mark.asyncio
async def test_platform_admin_guc_resets_on_rollback(
    pg_conn: ferrum.connection.Connection,
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with platform_admin_transaction(pg_conn):
            raise RuntimeError("boom")
    leaked = await _fetchval(pg_conn, "SELECT current_setting('app.platform_admin', true)")
    assert leaked in (None, "")


# ---------------------------------------------------------------------------
# schema_transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_transaction_sets_search_path(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    schema = f"ts_{unique_suffix}"
    await _execute(pg_conn, f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    try:
        allow = frozenset({schema, "public"})
        async with schema_transaction(pg_conn, schema, allowed_schemas=allow) as tx:
            sp = await current_setting(tx, "search_path")
            assert sp is not None
            # transaction-local search_path must include the schema.
            assert schema in sp
        # After commit, search_path resets on the pooled connection.
        default_sp = await _fetchval(pg_conn, "SELECT current_setting('search_path')")
        assert schema not in default_sp
    finally:
        await _execute(pg_conn, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.asyncio
async def test_schema_transaction_search_path_resets_on_rollback(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    schema = f"tr_{unique_suffix}"
    await _execute(pg_conn, f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    try:
        allow = frozenset({schema, "public"})
        with pytest.raises(RuntimeError, match="boom"):
            async with schema_transaction(pg_conn, schema, allowed_schemas=allow):
                raise RuntimeError("boom")
        default_sp = await _fetchval(pg_conn, "SELECT current_setting('search_path')")
        assert schema not in default_sp
    finally:
        await _execute(pg_conn, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.asyncio
async def test_schema_transaction_rejects_unregistered_schema(
    pg_conn: ferrum.connection.Connection,
) -> None:
    # "public" is in the default allowlist; a valid-but-unlisted identifier fails.
    with pytest.raises(FerrumCompileError) as exc_info:
        async with schema_transaction(pg_conn, "tenant_unlisted"):
            pass  # pragma: no cover
    assert exc_info.value.category == "schema_not_allowed"


@pytest.mark.asyncio
async def test_schema_transaction_rejects_injection(
    pg_conn: ferrum.connection.Connection,
) -> None:
    with pytest.raises(FerrumCompileError) as exc_info:
        async with schema_transaction(
            pg_conn,
            "public; DROP TABLE x",
            allowed_schemas=frozenset({"public", "public; DROP TABLE x"}),
        ):
            pass  # pragma: no cover
    assert exc_info.value.category == "invalid_identifier"


# ---------------------------------------------------------------------------
# Cross-tenant isolation (RLS GUC pattern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_guc_isolation(
    pg_conn: ferrum.connection.Connection,
) -> None:
    t_a, t_b = uuid.uuid4(), uuid.uuid4()
    async with tenant_transaction(pg_conn, t_a, guc_name="app.team_id") as tx:
        assert await current_setting(tx, "app.team_id") == str(t_a)
    # After tenant A's tx commits, a tenant B tx must NOT see A's GUC.
    async with tenant_transaction(pg_conn, t_b, guc_name="app.team_id") as tx:
        assert await current_setting(tx, "app.team_id") == str(t_b)
    # And the pool has no leaked tenant GUC.
    leaked = await _fetchval(pg_conn, "SELECT current_setting('app.team_id', true)")
    assert leaked in (None, "")


@pytest.mark.asyncio
async def test_cross_schema_isolation_via_search_path(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    s1 = f"cx1_{unique_suffix}"
    s2 = f"cx2_{unique_suffix}"
    await _execute(pg_conn, f'CREATE SCHEMA IF NOT EXISTS "{s1}"')
    await _execute(pg_conn, f'CREATE SCHEMA IF NOT EXISTS "{s2}"')
    await _execute(pg_conn, f'CREATE TABLE "{s1}".t(val int)')
    await _execute(pg_conn, f'CREATE TABLE "{s2}".t(val int)')
    await _execute(pg_conn, f'INSERT INTO "{s1}".t VALUES (1)')
    await _execute(pg_conn, f'INSERT INTO "{s2}".t VALUES (2)')
    try:
        allow = frozenset({s1, s2, "public"})
        async with schema_transaction(pg_conn, s1, allowed_schemas=allow) as tx:
            val = await _fetchval(tx, "SELECT val FROM t")
            assert val == 1
        async with schema_transaction(pg_conn, s2, allowed_schemas=allow) as tx:
            val = await _fetchval(tx, "SELECT val FROM t")
            assert val == 2
        # No search_path leak after the second tx commits.
        default_sp = await _fetchval(pg_conn, "SELECT current_setting('search_path')")
        assert s1 not in default_sp and s2 not in default_sp
    finally:
        await _execute(pg_conn, f'DROP SCHEMA IF EXISTS "{s1}" CASCADE')
        await _execute(pg_conn, f'DROP SCHEMA IF EXISTS "{s2}" CASCADE')


# ---------------------------------------------------------------------------
# Cancellation must not leak GUC or search_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_does_not_leak_admin_guc(
    pg_conn: ferrum.connection.Connection,
) -> None:
    started = asyncio.Event()
    done = asyncio.Event()

    async def _hold_admin() -> None:
        async with platform_admin_transaction(pg_conn) as tx:
            started.set()
            # A cancellable long await on the pinned transaction connection.
            await tx._require_driver().execute("SELECT pg_sleep(2)")

    task = asyncio.create_task(_hold_admin())
    await started.wait()
    await asyncio.sleep(0.05)  # let the sleep start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    done.set()

    # The pooled connection must not carry the admin GUC after cancellation.
    leaked = await _fetchval(pg_conn, "SELECT current_setting('app.platform_admin', true)")
    assert leaked in (None, "")


@pytest.mark.asyncio
async def test_cancellation_does_not_leak_search_path(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    schema = f"cn_{unique_suffix}"
    await _execute(pg_conn, f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    try:
        started = asyncio.Event()

        async def _hold_schema() -> None:
            async with schema_transaction(
                pg_conn, schema, allowed_schemas=frozenset({schema, "public"})
            ) as tx:
                started.set()
                await tx._require_driver().execute("SELECT pg_sleep(2)")

        task = asyncio.create_task(_hold_schema())
        await started.wait()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        default_sp = await _fetchval(pg_conn, "SELECT current_setting('search_path')")
        assert schema not in default_sp
    finally:
        await _execute(pg_conn, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# ---------------------------------------------------------------------------
# ConnectionRegistry / ShardRouter over independent PostgreSQL pools
# ---------------------------------------------------------------------------


def _pg_dsn() -> str:
    dsn = os.environ.get("FERRUM_TEST_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_DSN not set")
    return dsn


@pytest.mark.asyncio
async def test_registry_rejects_non_postgres_dsn() -> None:
    with pytest.raises(FerrumConfigError):
        ConnectionRegistry({"a": PoolConfig(dsn="mysql://u@h/db")})


@pytest.mark.asyncio
async def test_registry_start_health_close_and_stats() -> None:
    dsn = _pg_dsn()
    registry = ConnectionRegistry(
        {
            "shard_0": PoolConfig(dsn=dsn, max_size=3, application_name="ferrum_t_0"),
            "shard_1": PoolConfig(dsn=dsn, max_size=3, application_name="ferrum_t_1"),
        },
        parallelism=2,
    )
    await registry.start()
    try:
        assert registry.names == frozenset({"shard_0", "shard_1"})
        c0 = registry.get("shard_0")
        c1 = registry.get("shard_1")
        # Independent pools → distinct Connection objects.
        assert c0 is not c1
        # Each pool can serve a query.
        assert await _fetchval(c0, "SELECT 1") == 1
        assert await _fetchval(c1, "SELECT 1") == 1
        # Per-shard stats are available.
        stats = registry.stats()
        assert set(stats) == {"shard_0", "shard_1"}
        assert stats["shard_0"] is not None
        assert stats["shard_1"] is not None
        # Health check reports both healthy.
        health = await registry.health_check(timeout=5.0)
        assert health == {"shard_0": True, "shard_1": True}
    finally:
        await registry.close()
    # After close, get raises.
    with pytest.raises(FerrumConfigError):
        registry.get("shard_0")


@pytest.mark.asyncio
async def test_shard_router_resolves_and_transacts() -> None:
    dsn = _pg_dsn()
    registry = ConnectionRegistry(
        {
            "a": PoolConfig(dsn=dsn, max_size=2, application_name="ferrum_rt_a"),
            "b": PoolConfig(dsn=dsn, max_size=2, application_name="ferrum_rt_b"),
        },
    )
    await registry.start()
    router: ShardRouter[str] = ShardRouter(registry, resolver=lambda key: "a" if key < "m" else "b")
    try:
        assert router.connection_for("alice").dialect == "postgres"
        assert router.connection_for("zoe").dialect == "postgres"
        # Distinct pools for distinct shards.
        assert router.connection_for("alice") is not router.connection_for("zoe")
        # A transaction on the resolved shard commits.
        async with router.transaction_for("alice") as tx:
            assert await _fetchval(tx, "SELECT 7") == 7
        # Unknown shard (resolver returns a bad name) raises.
        bad_router: ShardRouter[str] = ShardRouter(registry, resolver=lambda key: "nope")
        with pytest.raises(FerrumConfigError):
            bad_router.connection_for("k")
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_registry_start_rolls_back_on_failure() -> None:
    dsn = _pg_dsn()
    # A bad DSN to a closed port triggers an open failure for "broken".
    bad_dsn = dsn.rsplit(":", 1)[0] + ":1/nonexistent_db_xyz"
    registry = ConnectionRegistry(
        {
            "good": PoolConfig(dsn=dsn, max_size=1),
            "broken": PoolConfig(dsn=bad_dsn, max_size=1),
        },
    )
    with pytest.raises((FerrumConnectionError, FerrumConfigError, RuntimeError)):
        await registry.start()
    # The good pool that opened must have been closed; registry is unusable.
    with pytest.raises(FerrumConfigError):
        registry.get("good")


# ---------------------------------------------------------------------------
# Module-level schema allowlist sanity (default contains "public")
# ---------------------------------------------------------------------------


def test_default_schema_allowlist_has_public() -> None:
    assert "public" in ALLOWED_SCHEMA_NAMES
