"""SQLite driver via aiosqlite."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import unquote, urlparse

from ferrum.errors import FerrumConfigError, FerrumConnectionError, map_db_error


def _sqlite_path_from_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    if parsed.path in ("", "/") and parsed.netloc == ":memory:":
        return ":memory:"
    if parsed.path in ("", "/"):
        return ":memory:"
    # sqlite:///absolute/path or sqlite://relative
    path = unquote(parsed.path.lstrip("/"))
    if parsed.netloc and parsed.netloc != ":memory:":
        path = f"{parsed.netloc}/{path}" if path else parsed.netloc
    return path or ":memory:"


def _row_to_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {k: row[k] for k in row}
    return dict(row)


class AiosqliteDriver:
    """aiosqlite connection driver (single connection; pool sizes ignored)."""

    dialect = "sqlite"

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        del min_size, max_size  # SQLite serializes writes; pooling is not meaningful.
        self._dsn = dsn
        self._db_path = _sqlite_path_from_dsn(dsn)
        self._conn: Any = None

    async def open(self) -> None:
        try:
            import aiosqlite  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FerrumConfigError(
                "SQLite driver not installed. Install with: uv add 'ferrum-orm[sqlite]' "
                "[FERR-C001]"
            ) from exc

        try:
            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
        except Exception as exc:
            raise FerrumConnectionError(
                f"Failed to connect to SQLite database: {type(exc).__name__} [FERR-E101]"
            ) from None

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> Any:
        if self._conn is None:
            raise FerrumConnectionError(
                "SQLite connection is not open. "
                "Use 'async with ferrum.connect(...) as conn:' first. [FERR-E101]"
            )
        return self._conn

    async def fetch(self, sql: str, *params: object) -> list[Any]:
        conn = self._require_conn()
        try:
            async with conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [_row_to_dict(r) for r in rows]
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        conn = self._require_conn()
        try:
            async with conn.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                return _row_to_dict(row) if row is not None else None
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
        conn = self._require_conn()
        try:
            async with conn.execute(sql, params) as cursor:
                await conn.commit()
                op = sql.strip().split()[0].upper()
                return f"{op} {cursor.rowcount}"
        except Exception as exc:
            raise map_db_error(exc) from None

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        yield self._require_conn()

    async def release(self, raw_conn: Any) -> None:
        del raw_conn
