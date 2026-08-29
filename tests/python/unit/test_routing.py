"""Unit tests for ferrum.routing — ConnectionRegistry and ShardRouter.

Invariants covered (AGENTS.md §5a):
- ConnectionRegistry rejects non-PostgreSQL DSNs at registration time.
- ConnectionRegistry rejects empty configs and parallelism < 1.
- start() opens all pools with bounded parallelism; rolls back on failure.
- get() raises for unknown shard, not-started shard, and after close().
- close() is idempotent and clears connections.
- health_check() reports per-shard liveness without aborting on one failure.
- stats() returns per-shard PoolStats (None when unavailable).
- ShardRouter resolves a trusted key via the resolver to a Connection.
- ShardRouter.transaction_for opens a transaction on the resolved shard.
- ShardRouter rejects a resolver that returns an unknown shard name.
- No implicit connection selection: the caller supplies the key and resolver.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ferrum.errors import FerrumConfigError
from ferrum.routing import ConnectionRegistry, PoolConfig, ShardRouter

_PG_DSN = "postgresql://user:pass@localhost:5432/db"


def _mock_connection() -> MagicMock:
    conn = MagicMock()
    conn.open = AsyncMock(return_value=None)
    conn.close = AsyncMock(return_value=None)
    conn.health_check = AsyncMock(return_value=True)
    conn.pool_stats = MagicMock(return_value=None)
    return conn


def _patch_connection_map(conns: dict[str, MagicMock]) -> Any:
    """Patch ferrum.routing.Connection to return conns in insertion order.

    Returns the patcher; use as ``with _patch_connection_map(conns): ...``.
    """
    ordered = list(conns.values())

    def _factory(dsn: str, **kw: Any) -> Any:
        _factory.i = getattr(_factory, "i", 0) + 1  # type: ignore[attr-defined]
        return ordered[_factory.i - 1]  # type: ignore[attr-defined]

    return patch("ferrum.routing.Connection", side_effect=_factory)


# ---------------------------------------------------------------------------
# PoolConfig / registration validation
# ---------------------------------------------------------------------------


class TestRegistryConstruction:
    def test_postgres_dsn_accepted(self) -> None:
        ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})

    def test_postgres_scheme_alias_accepted(self) -> None:
        ConnectionRegistry({"a": PoolConfig(dsn="postgres://u@h/db")})

    def test_mysql_dsn_rejected(self) -> None:
        with pytest.raises(FerrumConfigError) as exc_info:
            ConnectionRegistry({"a": PoolConfig(dsn="mysql://u@h/db")})
        assert "PostgreSQL-only" in str(exc_info.value)

    def test_sqlite_dsn_rejected(self) -> None:
        with pytest.raises(FerrumConfigError):
            ConnectionRegistry({"a": PoolConfig(dsn="sqlite:///./test.db")})

    def test_empty_configs_rejected(self) -> None:
        with pytest.raises(FerrumConfigError):
            ConnectionRegistry({})

    def test_parallelism_below_one_rejected(self) -> None:
        with pytest.raises(FerrumConfigError):
            ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)}, parallelism=0)

    def test_empty_shard_name_rejected(self) -> None:
        with pytest.raises(FerrumConfigError):
            ConnectionRegistry({"": PoolConfig(dsn=_PG_DSN)})

    def test_names_property(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN), "b": PoolConfig(dsn=_PG_DSN)})
        assert r.names == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# get() (before/after start)
# ---------------------------------------------------------------------------


class TestRegistryGet:
    @pytest.mark.asyncio
    async def test_get_before_start_raises(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        with pytest.raises(FerrumConfigError) as exc_info:
            r.get("a")
        assert "not started" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_unknown_shard_raises(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        with pytest.raises(FerrumConfigError) as exc_info:
            r.get("zzz")
        assert "not registered" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_after_close_raises(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        await r.close()  # idempotent no-op before start
        with pytest.raises(FerrumConfigError) as exc_info:
            r.get("a")
        assert "closed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# start() / close()
# ---------------------------------------------------------------------------


class TestRegistryStartClose:
    @pytest.mark.asyncio
    async def test_start_opens_all_pools(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN), "b": PoolConfig(dsn=_PG_DSN)})
        conns = {n: _mock_connection() for n in ("a", "b")}
        with _patch_connection_map(conns):
            await r.start()
        conns["a"].open.assert_awaited_once()
        conns["b"].open.assert_awaited_once()
        assert r.get("a") is conns["a"]
        assert r.get("b") is conns["b"]
        await r.close()
        conns["a"].close.assert_awaited_once()
        conns["b"].close.assert_awaited_once()
        with pytest.raises(FerrumConfigError):
            r.get("a")

    @pytest.mark.asyncio
    async def test_start_rolls_back_on_failure(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN), "b": PoolConfig(dsn=_PG_DSN)})
        good = _mock_connection()
        bad = _mock_connection()
        bad.open = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            _patch_connection_map({"a": good, "b": bad}),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await r.start()
        good.close.assert_awaited_once()
        bad.close.assert_not_awaited()
        with pytest.raises(FerrumConfigError):
            r.get("a")

    @pytest.mark.asyncio
    async def test_start_twice_raises(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        conn = _mock_connection()
        with patch("ferrum.routing.Connection", return_value=conn):
            await r.start()
        with pytest.raises(FerrumConfigError) as exc_info:
            await r.start()
        assert "already started" in str(exc_info.value)
        await r.close()

    @pytest.mark.asyncio
    async def test_close_idempotent(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        conn = _mock_connection()
        with patch("ferrum.routing.Connection", return_value=conn):
            await r.start()
        await r.close()
        await r.close()
        conn.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# health_check / stats
# ---------------------------------------------------------------------------


class TestRegistryHealthStats:
    @pytest.mark.asyncio
    async def test_health_check_reports_per_shard(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN), "b": PoolConfig(dsn=_PG_DSN)})
        good = _mock_connection()
        sick = _mock_connection()
        sick.health_check = AsyncMock(side_effect=RuntimeError("down"))
        with _patch_connection_map({"a": good, "b": sick}):
            await r.start()
        results = await r.health_check(timeout=1.0)
        assert results["a"] is True
        assert results["b"] is False
        await r.close()

    @pytest.mark.asyncio
    async def test_stats_returns_per_shard(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        conn = _mock_connection()
        conn.pool_stats = MagicMock(return_value=MagicMock(size=2))
        with patch("ferrum.routing.Connection", return_value=conn):
            await r.start()
        s = r.stats()
        assert set(s) == {"a"}
        assert s["a"] is conn.pool_stats.return_value
        await r.close()


# ---------------------------------------------------------------------------
# ShardRouter
# ---------------------------------------------------------------------------


class TestShardRouter:
    @pytest.mark.asyncio
    async def test_connection_for_resolves_key(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN), "b": PoolConfig(dsn=_PG_DSN)})
        conns = {n: _mock_connection() for n in ("a", "b")}
        with _patch_connection_map(conns):
            await r.start()
        router = ShardRouter(r, resolver=lambda key: "a" if key < "m" else "b")
        assert router.connection_for("alice") is conns["a"]
        assert router.connection_for("zoe") is conns["b"]
        assert router.names == frozenset({"a", "b"})
        await r.close()

    @pytest.mark.asyncio
    async def test_connection_for_unknown_shard_raises(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        conn = _mock_connection()
        with patch("ferrum.routing.Connection", return_value=conn):
            await r.start()
        router = ShardRouter(r, resolver=lambda key: "nope")
        with pytest.raises(FerrumConfigError) as exc_info:
            router.connection_for("k")
        assert "not registered" in str(exc_info.value)
        await r.close()

    @pytest.mark.asyncio
    async def test_transaction_for_opens_transaction_on_resolved_shard(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        conn = _mock_connection()
        yielded_tx = MagicMock()

        @contextlib.asynccontextmanager
        async def _fake_tx(**kw: Any):  # type: ignore[misc]
            yield yielded_tx

        conn.transaction = _fake_tx
        with patch("ferrum.routing.Connection", return_value=conn):
            await r.start()
        router = ShardRouter(r, resolver=lambda key: "a")
        async with router.transaction_for("k", isolation="serializable") as tx:
            assert tx is yielded_tx
        await r.close()

    @pytest.mark.asyncio
    async def test_stats_and_health_delegated(self) -> None:
        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN)})
        conn = _mock_connection()
        with patch("ferrum.routing.Connection", return_value=conn):
            await r.start()
        router = ShardRouter(r, resolver=lambda key: "a")
        assert router.stats() == r.stats()
        assert await router.health_check() == await r.health_check()
        await r.close()
