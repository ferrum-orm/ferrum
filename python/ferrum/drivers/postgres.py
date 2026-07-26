"""PostgreSQL driver via asyncpg."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator
from typing import Any

from ferrum.drivers.protocol import ChunkStreamProtocol, CompiledQuery
from ferrum.drivers.streaming import AsyncpgChunkStream
from ferrum.errors import FerrumConfigError, FerrumConnectionError, map_db_error


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


class AsyncpgDriver:
    """asyncpg pool-backed driver."""

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
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._acquire_timeout = acquire_timeout
        self._statement_timeout_ms = statement_timeout_ms
        self._max_lifetime = max_lifetime
        self._pool: Any = None

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
        if self._max_lifetime is not None:
            pool_kwargs["max_inactive_connection_lifetime"] = self._max_lifetime

        statement_timeout_ms = self._statement_timeout_ms

        async def _init_conn(conn: Any) -> None:
            await _register_json_codecs(conn)
            if statement_timeout_ms is not None:
                await conn.execute(f"SET statement_timeout = {statement_timeout_ms}")

        pool_kwargs["init"] = _init_conn
        try:
            self._pool = await asyncpg.create_pool(self._dsn, **pool_kwargs)
        except Exception as exc:
            raise FerrumConnectionError(
                f"Failed to connect to PostgreSQL at {diag['host']}:{diag['port']} "
                f"(database={diag['database']}, username={diag['username']}): "
                f"{type(exc).__name__} [FERR-E101]"
            ) from None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_driver(self) -> Any:
        if self._pool is None:
            raise FerrumConnectionError(
                "Connection pool is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open the pool first. "
                "[FERR-E101]"
            )
        return self._pool

    async def fetch(self, sql: str, *params: object) -> list[Any]:
        pool = self._require_driver()
        try:
            return await pool.fetch(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        pool = self._require_driver()
        try:
            return await pool.fetchrow(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchval(self, sql: str, *params: object) -> Any:
        pool = self._require_driver()
        try:
            return await pool.fetchval(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def execute(self, sql: str, *params: object) -> str:
        pool = self._require_driver()
        try:
            return await pool.execute(sql, *params)
        except Exception as exc:
            raise map_db_error(exc) from None

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
            raise map_db_error(exc) from None

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
        """
        pool = self._require_driver()
        async with pool.acquire() as raw_conn:
            async with raw_conn.transaction(
                isolation=isolation, readonly=readonly, deferrable=deferrable
            ):
                yield _BoundConnection(raw_conn)
