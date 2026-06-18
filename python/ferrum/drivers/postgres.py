"""PostgreSQL driver via asyncpg."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any

from ferrum.errors import FerrumConfigError, FerrumConnectionError, map_db_error


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

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None

    async def open(self) -> None:
        try:
            import asyncpg  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FerrumConfigError(
                "PostgreSQL driver not installed. Install with: uv add 'ferrum-orm[pg]' [FERR-C001]"
            ) from exc

        diag = _redacted_diag(self._dsn)
        try:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
            )
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

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        pool = self._require_driver()
        try:
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
