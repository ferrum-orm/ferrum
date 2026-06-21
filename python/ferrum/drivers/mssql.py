"""MSSQL driver via aioodbc."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from ferrum.errors import FerrumConfigError, FerrumConnectionError, map_db_error


def _redacted_diag(dsn: str) -> dict[str, str]:
    try:
        parsed = urlparse(dsn)
        if not parsed.hostname and "=" in dsn:
            return {
                "host": "unknown",
                "port": "unknown",
                "database": "unknown",
                "username": "unknown",
            }
        return {
            "host": parsed.hostname or "unknown",
            "port": str(parsed.port or 1433),
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


def _normalize_mssql_dsn(dsn: str) -> str:
    if dsn.startswith("mssql://") or dsn.startswith("mssql+aioodbc://"):
        parsed = urlparse(dsn)
        host = parsed.hostname or "localhost"
        port = parsed.port or 1433
        db = (parsed.path or "").lstrip("/")
        user = parsed.username or ""
        password = parsed.password or ""
        
        driver = "{ODBC Driver 18 for SQL Server}"
        if parsed.query:
            import urllib.parse as up
            qs = up.parse_qs(parsed.query)
            if 'driver' in qs:
                driver = qs['driver'][0]
                if not driver.startswith("{"):
                    driver = f"{{{driver}}}"
                    
        return f"Driver={driver};Server={host},{port};Database={db};UID={user};PWD={password};TrustServerCertificate=yes;"
    return dsn


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row))


class AsyncOdbcDriver:
    """aioodbc pool-backed driver for MSSQL."""

    dialect = "mssql"

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn_url = dsn
        self._dsn = _normalize_mssql_dsn(dsn)
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None

    async def open(self) -> None:
        try:
            import aioodbc  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FerrumConfigError(
                "MSSQL driver not installed. Install with: uv add 'ferrum-orm[mssql]' [FERR-C001]"
            ) from exc

        diag = _redacted_diag(self._dsn_url)
        try:
            self._pool = await aioodbc.create_pool(
                dsn=self._dsn,
                minsize=self._min_size,
                maxsize=self._max_size,
            )
        except Exception as exc:
            raise FerrumConnectionError(
                f"Failed to connect to MSSQL at {diag['host']}:{diag['port']} "
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
        pool = self._require_driver()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql, params)
                    rows = await cursor.fetchall()
                    if not rows:
                        return []
                    columns = [column[0] for column in cursor.description]
                    return [dict(zip(columns, row)) for row in rows]
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        pool = self._require_driver()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql, params)
                    row = await cursor.fetchone()
                    return _row_to_dict(cursor, row)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchval(self, sql: str, *params: object) -> Any:
        pool = self._require_driver()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql, params)
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    return row[0]
        except Exception as exc:
            raise map_db_error(exc) from None

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
