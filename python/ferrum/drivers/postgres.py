"""PostgreSQL driver via asyncpg."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ferrum.drivers.protocol import ChunkStreamProtocol, CompiledQuery
from ferrum.drivers.streaming import AsyncpgChunkStream
from ferrum.errors import FerrumConfigError, FerrumConnectionError, map_db_error

if TYPE_CHECKING:
    from ssl import SSLContext


def _encode_json_param(value: Any) -> str:
    """Encode a Python value for asyncpg ``json`` / ``jsonb`` text codecs.

    Ferrum's bind path already ``json.dumps`` JSON fields to text before the
    driver (with SQL ``::jsonb`` casts). Accept pre-serialized strings so a
    codec encoder never double-encodes those binds if a future path sends the
    parameter as a native json/jsonb OID.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, separators=(",", ":"))


def _decode_json_param(value: str) -> Any:
    """Decode asyncpg ``json`` / ``jsonb`` text into native Python objects."""
    return json.loads(value)


async def _register_json_codecs(conn: Any) -> None:
    """Make JSONB/JSON columns hydrate as ``dict`` / ``list``, not ``str``.

    asyncpg's default is to return ``json``/``jsonb`` as text. Without this,
    ``model_construct`` leaves JSON fields as strings and Pydantic consumers
    that expect ``dict`` fail at the agent/MCP boundary.
    """
    for type_name in ("jsonb", "json"):
        await conn.set_type_codec(
            type_name,
            schema="pg_catalog",
            encoder=_encode_json_param,
            decoder=_decode_json_param,
            format="text",
        )


class _BoundConnection:
    """Execution surface pinned to a single raw connection inside a transaction.

    QuerySet terminals only call ``fetch``/``fetchrow``/``fetchval``/``execute`` on
    the object returned by ``Connection._require_driver()``; binding those to one
    pinned ``asyncpg`` connection (instead of acquiring a fresh pooled connection
    per statement) is what makes multiple terminals share a transaction.

    Errors are mapped through the same ``map_db_error`` seam as the pooled driver
    (ADR-006) so callers see the sanitized Ferrum taxonomy either way.
    """

    dialect = "postgres"

    def __init__(self, raw_conn: Any) -> None:
        self._raw = raw_conn

    async def fetch(self, sql: str, *params: object) -> list[Any]:
        try:
            return await self._raw.fetch(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        try:
            return await self._raw.fetchrow(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchval(self, sql: str, *params: object) -> Any:
        try:
            return await self._raw.fetchval(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def execute(self, sql: str, *params: object) -> str:
        try:
            return await self._raw.execute(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

    def open_stream(
        self,
        compiled: CompiledQuery,
        *,
        chunk_size: int,
        query_timeout: float | None,
    ) -> ChunkStreamProtocol:
        @contextlib.asynccontextmanager
        async def _source() -> AsyncGenerator[Any, None]:
            yield self._raw

        return AsyncpgChunkStream(
            _source,
            compiled,
            chunk_size=chunk_size,
            query_timeout=query_timeout,
        )

    @contextlib.asynccontextmanager
    async def savepoint(self) -> AsyncGenerator[_BoundConnection, None]:
        """Nested transaction = PostgreSQL SAVEPOINT (asyncpg auto-detects nesting).

        Rolls the savepoint back on any exception (including cancellation) and
        releases it on clean exit, independently of the enclosing transaction.
        """
        async with self._raw.transaction():
            yield _BoundConnection(self._raw)


def _redacted_diag(dsn: str) -> dict[str, str]:
    """Extract safe connection diagnostics from a DSN — never the password.

    Returns only host, port, database, username (the §3 credential allowlist).
    The password and full DSN are never included in any diagnostic field.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(dsn)
        return {
            "host": parsed.hostname or "unknown",
            "port": str(parsed.port or 5432),
            "database": (parsed.path or "").lstrip("/") or "unknown",
            "username": parsed.username or "unknown",
        }
    except Exception:
        return {
            "host": "unknown",
            "port": "unknown",
            "database": "unknown",
            "username": "unknown",
        }


@dataclass(frozen=True)
class PoolStats:
    """Typed snapshot of pool state at a point in time.

    Fields that are not available from the underlying pool are reported as
    ``-1`` (sentinel meaning "unavailable") so callers can always compare
    numerically without ``None`` checks.

    Attributes:
        size: Total connections in the pool (open + being opened).
        idle: Connections currently idle (not acquired).
        acquired: Connections currently acquired by callers.
        waiters: Coroutines waiting to acquire (``-1`` if the driver does not
            expose this).
        min_size: Minimum pool size configured at open time.
        max_size: Maximum pool size configured at open time.
        inflight: Ferrum-tracked in-flight operations (queries, transactions,
            streams) — not the same as ``acquired`` which counts raw pool
            connections.
        accepting: Whether the pool is accepting new work (``False`` during
            shutdown).
        closing: Whether the pool is in the process of shutting down.
    """

    size: int
    idle: int
    acquired: int
    waiters: int
    min_size: int
    max_size: int
    inflight: int
    accepting: bool
    closing: bool


class AsyncpgDriver:
    """asyncpg pool-backed driver.

    All acquire paths honor ``acquire_timeout``: convenience
    ``fetch``/``fetchrow``/``fetchval``/``execute``, ``transaction()``,
    ``open_stream()``, and the ``acquire()`` context manager.

    Pool configuration knobs:
    - ``min_size`` / ``max_size``: pool sizing bounds.
    - ``acquire_timeout``: seconds to wait for a pooled connection.
    - ``statement_timeout_ms``: server-side ``statement_timeout`` (milliseconds).
    - ``max_lifetime``: legacy alias for ``max_idle_lifetime`` (idle recycling).
    - ``max_idle_lifetime``: recycle idle connections after this many seconds
      (asyncpg ``max_inactive_connection_lifetime``).
    - ``max_connection_age``: hard max age — connections older than this are
      recycled regardless of idle state (tracked, enforced via
      ``expire_connections()``).
    - ``command_timeout``: per-command timeout in seconds (asyncpg
      ``command_timeout``).
    - ``statement_cache_size``: asyncpg prepared-statement cache size.
    - ``ssl``: SSL configuration (``True``, mode string, ``SSLContext``, or
      ``None``).
    - ``server_settings``: PostgreSQL GUC overrides (``dict[str, str]``).
    - ``application_name``: folded into ``server_settings`` as
      ``application_name``.
    """

    dialect = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        acquire_timeout: float | None = None,
        statement_timeout_ms: int | None = None,
        max_lifetime: float | None = None,
        max_idle_lifetime: float | None = None,
        max_connection_age: float | None = None,
        command_timeout: float | None = None,
        statement_cache_size: int | None = None,
        ssl: bool | str | SSLContext | None = None,
        server_settings: dict[str, str] | None = None,
        application_name: str | None = None,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._acquire_timeout = acquire_timeout
        self._statement_timeout_ms = statement_timeout_ms
        # max_lifetime is the legacy name for max_idle_lifetime (asyncpg's
        # max_inactive_connection_lifetime). Both map to the same knob;
        # max_idle_lifetime takes precedence when both are provided.
        self._max_idle_lifetime: float | None = (
            max_idle_lifetime if max_idle_lifetime is not None else max_lifetime
        )
        # max_connection_age is a hard age limit — connections older than this
        # are recycled regardless of idle state. asyncpg doesn't enforce this
        # directly; we track connection birth times and expire when exceeded.
        self._max_connection_age = max_connection_age
        self._command_timeout = command_timeout
        self._statement_cache_size = statement_cache_size
        self._ssl = ssl
        self._server_settings: dict[str, str] = dict(server_settings) if server_settings else {}
        if application_name is not None:
            self._server_settings.setdefault("application_name", application_name)
        self._pool: Any = None
        self._extra_codecs: list[dict[str, Any]] = []
        # Track connection birth times for max_connection_age enforcement.
        self._conn_birth_times: dict[int, float] = {}

    async def open(self) -> None:
        try:
            import asyncpg  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FerrumConfigError(
                "PostgreSQL driver not installed. Install with: uv add 'ferrum-orm[pg]' [FERR-C001]"
            ) from exc

        diag = _redacted_diag(self._dsn)
        pool_kwargs: dict[str, Any] = {
            "min_size": self._min_size,
            "max_size": self._max_size,
        }
        if self._max_idle_lifetime is not None:
            pool_kwargs["max_inactive_connection_lifetime"] = self._max_idle_lifetime
        if self._command_timeout is not None:
            pool_kwargs["command_timeout"] = self._command_timeout
        if self._statement_cache_size is not None:
            pool_kwargs["statement_cache_size"] = self._statement_cache_size
        if self._ssl is not None:
            pool_kwargs["ssl"] = self._ssl
        if self._server_settings:
            pool_kwargs["server_settings"] = self._server_settings

        statement_timeout_ms = self._statement_timeout_ms
        extra_codecs = self._extra_codecs
        driver_ref = self

        async def _init_conn(conn: Any) -> None:
            await _register_json_codecs(conn)
            for codec in extra_codecs:
                await conn.set_type_codec(**codec)
            if statement_timeout_ms is not None:
                await conn.execute(f"SET statement_timeout = {statement_timeout_ms}")
            # Track birth time for max_connection_age enforcement.
            if driver_ref._max_connection_age is not None:
                driver_ref._conn_birth_times[id(conn)] = time.monotonic()

        pool_kwargs["init"] = _init_conn
        try:
            self._pool = await asyncpg.create_pool(self._dsn, **pool_kwargs)
        except Exception as exc:
            raise FerrumConnectionError(
                f"Failed to connect to PostgreSQL at {diag['host']}:{diag['port']} "
                f"(database={diag['database']}, username={diag['username']}): "
                f"{type(exc).__name__} [FERR-E101]",
                category="connection",
            ) from None

    async def add_type_codec(
        self,
        type_name: str,
        *,
        schema: str,
        encoder: Any,
        decoder: Any,
        format: str = "text",
    ) -> None:
        """Register an asyncpg type codec on every connection in the pool.

        asyncpg exposes ``set_type_codec`` on a connection, not on a pool, so
        registering directly against one acquired connection only covers that
        connection — every other pooled connection keeps the default codec and
        the same query succeeds or fails depending on which one it lands on.
        Recording the codec here applies it from the pool ``init`` hook, and
        expiring the pool forces already-initialized connections to be replaced
        so the registration is uniform.
        """
        if any(c["typename"] == type_name and c["schema"] == schema for c in self._extra_codecs):
            return
        self._extra_codecs.append(
            {
                "typename": type_name,
                "schema": schema,
                "encoder": encoder,
                "decoder": decoder,
                "format": format,
            }
        )
        if self._pool is not None:
            await self._pool.expire_connections()

    async def close(self) -> None:
        if self._pool is not None:
            pool = self._pool
            self._pool = None
            self._conn_birth_times.clear()
            # If connections are still acquired (e.g. drain timeout fired
            # upstream), force-terminate to avoid a deadlock waiting for
            # releases that will never happen. Otherwise, graceful close.
            holders = getattr(pool, "_holders", [])
            if any(getattr(h, "_in_use", False) for h in holders):
                pool.terminate()
            else:
                await pool.close()

    def _require_driver(self) -> Any:
        if self._pool is None:
            raise FerrumConnectionError(
                "Connection pool is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open the pool first. "
                "[FERR-E101]",
                category="config",
            )
        return self._pool

    def _acquire_cm(self) -> Any:
        """Return an async context manager that acquires with ``acquire_timeout``.

        This is the single acquire seam used by every convenience method and
        ``transaction()``, so the timeout is enforced on every path — not just
        on explicit ``Connection.acquire()``.
        """
        pool = self._require_driver()
        if self._acquire_timeout is not None:
            return pool.acquire(timeout=self._acquire_timeout)
        return pool.acquire()

    async def _expire_connections_safe(self) -> None:
        """Expire all pooled connections (failover/stale replacement).

        Called after a failover-category error so the next acquire gets a fresh
        connection instead of a stale one. Safe to call when the pool is closed
        or closing — it is a no-op in that case.
        """
        if self._pool is not None:
            with contextlib.suppress(Exception):
                await self._pool.expire_connections()

    async def _handle_post_error(self, mapped: Exception) -> None:
        """Expire pool connections after a failover-category error.

        No unconditional pre-ping — only on detected failover. This is the
        failover-safe replacement path: stale connections are recycled so the
        next acquire does not land on a dead connection.
        """
        category = getattr(mapped, "category", None)
        if category == "failover":
            await self._expire_connections_safe()

    async def fetch(self, sql: str, *params: object) -> list[Any]:
        try:
            async with self._acquire_cm() as raw_conn:
                return await raw_conn.fetch(sql, *params)
        except Exception as exc:
            mapped = map_db_error(exc)
            await self._handle_post_error(mapped)
            raise mapped from None

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        try:
            async with self._acquire_cm() as raw_conn:
                return await raw_conn.fetchrow(sql, *params)
        except Exception as exc:
            mapped = map_db_error(exc)
            await self._handle_post_error(mapped)
            raise mapped from None

    async def fetchval(self, sql: str, *params: object) -> Any:
        try:
            async with self._acquire_cm() as raw_conn:
                return await raw_conn.fetchval(sql, *params)
        except Exception as exc:
            mapped = map_db_error(exc)
            await self._handle_post_error(mapped)
            raise mapped from None

    async def execute(self, sql: str, *params: object) -> str:
        try:
            async with self._acquire_cm() as raw_conn:
                return await raw_conn.execute(sql, *params)
        except Exception as exc:
            mapped = map_db_error(exc)
            await self._handle_post_error(mapped)
            raise mapped from None

    def open_stream(
        self,
        compiled: CompiledQuery,
        *,
        chunk_size: int,
        query_timeout: float | None,
    ) -> ChunkStreamProtocol:
        pool = self._require_driver()

        @contextlib.asynccontextmanager
        async def _source() -> AsyncGenerator[Any, None]:
            if self._acquire_timeout is not None:
                async with pool.acquire(timeout=self._acquire_timeout) as raw_conn:
                    async with raw_conn.transaction():
                        yield raw_conn
            else:
                async with pool.acquire() as raw_conn:
                    async with raw_conn.transaction():
                        yield raw_conn

        return AsyncpgChunkStream(
            _source,
            compiled,
            chunk_size=chunk_size,
            query_timeout=query_timeout,
        )

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        pool = self._require_driver()
        try:
            if self._acquire_timeout is not None:
                async with pool.acquire(timeout=self._acquire_timeout) as raw_conn:
                    yield raw_conn
            else:
                async with pool.acquire() as raw_conn:
                    yield raw_conn
        except Exception as exc:
            mapped = map_db_error(exc)
            await self._handle_post_error(mapped)
            raise mapped from None

    async def release(self, raw_conn: Any) -> None:
        pool = self._require_driver()
        try:
            await pool.release(raw_conn)
        except Exception as exc:
            raise map_db_error(exc) from None

    @contextlib.asynccontextmanager
    async def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> AsyncGenerator[_BoundConnection, None]:
        """Pin one pooled connection and run a transaction on it.

        Delegates BEGIN/COMMIT/ROLLBACK to asyncpg's own transaction context
        manager, which commits on clean exit and rolls back on any exception —
        including ``CancelledError`` — before the connection is returned to the
        pool. ``isolation`` is passed to asyncpg's typed API (never interpolated);
        the caller (``Connection.transaction``) validates it against an allowlist.
        ``acquire_timeout`` is enforced on the pool acquire.
        """
        pool = self._require_driver()
        if self._acquire_timeout is not None:
            async with pool.acquire(timeout=self._acquire_timeout) as raw_conn:
                async with raw_conn.transaction(
                    isolation=isolation, readonly=readonly, deferrable=deferrable
                ):
                    yield _BoundConnection(raw_conn)
        else:
            async with pool.acquire() as raw_conn:
                async with raw_conn.transaction(
                    isolation=isolation, readonly=readonly, deferrable=deferrable
                ):
                    yield _BoundConnection(raw_conn)

    def pool_stats(self, *, inflight: int = 0, accepting: bool = True) -> PoolStats:
        """Return a typed snapshot of pool state.

        Combines asyncpg pool internals (defensive ``getattr`` on private
        attributes) with Ferrum lifecycle state passed from ``Connection``.

        Args:
            inflight: Ferrum-tracked in-flight operations (from ``_LifecycleGuard``).
            accepting: Whether the pool is accepting new work (from
                ``_LifecycleGuard.accepting``).

        Returns:
            A ``PoolStats`` snapshot. When the pool is not open, all fields
            are zero/``False`` and ``closing`` is ``True``.
        """
        pool = self._pool
        if pool is None:
            return PoolStats(
                size=0,
                idle=0,
                acquired=0,
                waiters=-1,
                min_size=self._min_size,
                max_size=self._max_size,
                inflight=inflight,
                accepting=False,
                closing=True,
            )

        # asyncpg pool internals — defensive getattr for version portability.
        holders = getattr(pool, "_holders", [])
        size = len(holders)
        acquired = sum(1 for h in holders if getattr(h, "_in_use", False))
        idle = size - acquired
        closing = bool(getattr(pool, "_closing", False))

        return PoolStats(
            size=size,
            idle=idle,
            acquired=acquired,
            waiters=-1,  # asyncpg does not expose waiter count
            min_size=self._min_size,
            max_size=self._max_size,
            inflight=inflight,
            accepting=accepting and not closing,
            closing=closing,
        )

    async def expire_connections(self) -> None:
        """Expire all pooled connections so the next acquire creates fresh ones.

        Used for failover replacement, stale-connection recycling, and
        ``max_connection_age`` enforcement.
        """
        await self._expire_connections_safe()
