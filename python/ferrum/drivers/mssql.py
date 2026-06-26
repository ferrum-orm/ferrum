"""Microsoft SQL Server driver via aioodbc (thin parity).

Matches the MySQL/SQLite feature level: no transactions, upsert, bulk_update,
RLS, or pgvector. T-SQL emits ``OUTPUT INSERTED.*`` for returning data, so an
INSERT followed by ``fetchrow`` returns the new row directly — no
``LAST_INSERT_ID`` round-trip is needed.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import parse_qs, urlparse

from ferrum.errors import FerrumConfigError, FerrumConnectionError, map_db_error

_DEFAULT_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"


def _redacted_diag(dsn: str) -> dict[str, str]:
    try:
        parsed = urlparse(dsn)
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


def _odbc_connection_string(dsn: str) -> str:
    """Build an ODBC connection string from a ``mssql://`` / ``sqlserver://`` URL.

    The password is taken from the URL only to assemble the driver connection
    string; it is never logged or surfaced in diagnostics (CRED-1).
    """
    parsed = urlparse(dsn)
    query = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items()}

    driver = query.get("driver", _DEFAULT_ODBC_DRIVER)
    encrypt = query.get("encrypt", "yes")
    trust = query.get("trustservercertificate", "no")

    host = parsed.hostname or "localhost"
    port = parsed.port or 1433
    database = (parsed.path or "").lstrip("/")

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={host},{port}",
    ]
    if database:
        parts.append(f"DATABASE={database}")
    if parsed.username:
        parts.append(f"UID={parsed.username}")
    if parsed.password:
        parts.append(f"PWD={parsed.password}")
    parts.append(f"Encrypt={encrypt}")
    parts.append(f"TrustServerCertificate={trust}")
    return ";".join(parts)


async def _fetch_dicts(cursor: Any) -> list[dict[str, Any]]:
    columns = [col[0] for col in cursor.description] if cursor.description else []
    rows = await cursor.fetchall()
    return [dict(zip(columns, row, strict=False)) for row in rows] if rows else []


async def _fetchone_dict(cursor: Any) -> dict[str, Any] | None:
    columns = [col[0] for col in cursor.description] if cursor.description else []
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(zip(columns, row, strict=False))


class AioodbcDriver:
    """aioodbc pool-backed driver for SQL Server (thin parity)."""

    dialect = "mssql"

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
        # Thin parity: timeout/lifetime knobs are accepted for a uniform driver
        # constructor contract but not yet wired into the aioodbc pool.
        del acquire_timeout, statement_timeout_ms, max_lifetime
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None

    async def open(self) -> None:
        try:
            import aioodbc  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FerrumConfigError(
                "MSSQL driver not installed. Install with: uv add 'ferrum-orm[mssql]' "
                "(and the system ODBC driver, e.g. msodbcsql18). [FERR-C001]"
            ) from exc

        diag = _redacted_diag(self._dsn)
        try:
            self._pool = await aioodbc.create_pool(
                dsn=_odbc_connection_string(self._dsn),
                minsize=self._min_size,
                maxsize=self._max_size,
                autocommit=True,
            )
        except Exception as exc:
            raise FerrumConnectionError(
                f"Failed to connect to SQL Server at {diag['host']}:{diag['port']} "
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
                    return await _fetch_dicts(cursor)
        except Exception as exc:
            raise map_db_error(exc) from None

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        pool = self._require_driver()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql, params)
                    return await _fetchone_dict(cursor)
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
                    rowcount = cursor.rowcount
                    op = sql.strip().split()[0].upper()
                    return f"{op} {rowcount}"
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
