"""MySQL driver via asyncmy."""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from ferrum.errors import FerrumConfigError, FerrumConnectionError, map_db_error

_INSERT_TABLE_RE = re.compile(
    r"INSERT\s+INTO\s+[`\"]?(\w+)[`\"]?",
    re.IGNORECASE,
)


def _redacted_diag(dsn: str) -> dict[str, str]:
    try:
        parsed = urlparse(dsn)
        return {
            "host": parsed.hostname or "unknown",
            "port": str(parsed.port or 3306),
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


def _normalize_mysql_dsn(dsn: str) -> str:
    # asyncmy expects mysql://user:pass@host/db
    return dsn.replace("mysql+asyncmy://", "mysql://", 1)


class AsyncmyDriver:
    """asyncmy pool-backed driver."""

    dialect = "mysql"

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = _normalize_mysql_dsn(dsn)
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None

    async def open(self) -> None:
        try:
            import asyncmy  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FerrumConfigError(
                "MySQL driver not installed. Install with: uv add 'ferrum-orm[mysql]' [FERR-C001]"
            ) from exc

        diag = _redacted_diag(self._dsn)
        try:
            self._pool = await asyncmy.create_pool(
                dsn=self._dsn,
                minsize=self._min_size,
                maxsize=self._max_size,
            )
        except Exception as exc:
            raise FerrumConnectionError(
                f"Failed to connect to MySQL at {diag['host']}:{diag['port']} "
                f"(database={diag['database']}, username={diag['username']}): "
                f"{type(exc).__name__} [FERR-E101]"
            ) from None

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    def _require_driver(self) -> Any:
        if self._pool is None:
            raise FerrumConnectionError(
                "Connection pool is not open. "
                "Use 'async with ferrum.connect(...) as conn:' first. [FERR-E101]"
            )
        return self._pool

    async def fetch(self, sql: str, *params: object) -> list[Any]:
        import asyncmy  # type: ignore[import-untyped]

        pool = self._require_driver()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor(asyncmy.cursors.DictCursor) as cursor:
                    await cursor.execute(sql, params)
                    rows = await cursor.fetchall()
                    return list(rows) if rows else []
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        import asyncmy  # type: ignore[import-untyped]

        upper = sql.strip().upper()
        if upper.startswith("INSERT") and "RETURNING" not in upper:
            await self.execute(sql, *params)
            match = _INSERT_TABLE_RE.search(sql)
            table = match.group(1) if match else None
            if table is None:
                return None
            refetch_sql = f"SELECT * FROM `{table}` WHERE `id` = LAST_INSERT_ID()"
            return await self.fetchrow(refetch_sql)

        pool = self._require_driver()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor(asyncmy.cursors.DictCursor) as cursor:
                    await cursor.execute(sql, params)
                    return await cursor.fetchone()
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchval(self, sql: str, *params: object) -> Any:
        row = await self.fetchrow(sql, *params)
        if row is None:
            return None
        if isinstance(row, dict):
            return next(iter(row.values()), None)
        return row[0] if row else None

    async def execute(self, sql: str, *params: object) -> str:
        pool = self._require_driver()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql, params)
                    await conn.commit()
                    op = sql.strip().split()[0].upper()
                    return f"{op} {cursor.rowcount}"
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
            pool.release(raw_conn)
        except Exception as exc:
            raise map_db_error(exc) from None
