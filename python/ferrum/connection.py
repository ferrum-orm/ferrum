"""Ferrum connection pool and DSN configuration.

Wraps asyncpg with:
- Redacted diagnostics: connection errors report host/port/database/username
  and an error category, never the password or full DSN (CRED-1).
- TLS configuration via ``sslmode``.
- Async context-manager interface for pool lifecycle.

This module owns the async I/O path; no SQL building or Rust calls happen here.
"""

from __future__ import annotations

import contextlib
from typing import AsyncGenerator
from urllib.parse import urlparse

import asyncpg  # type: ignore[import-untyped]

from ferrum.errors import FerrumConnectionError


def _redacted_dsn_info(dsn: str) -> dict[str, str]:
    """Extract safe connection diagnostics from a DSN — never the password.

    Returns a dict with keys: host, port, database, username. Used in error
    messages and Tier A hook payloads (CRED-1).
    """
    try:
        parsed = urlparse(dsn)
        return {
            "host": parsed.hostname or "unknown",
            "port": str(parsed.port or 5432),
            "database": (parsed.path or "").lstrip("/") or "unknown",
            "username": parsed.username or "unknown",
        }
    except Exception:  # noqa: BLE001
        return {"host": "unknown", "port": "unknown", "database": "unknown", "username": "unknown"}


class Connection:
    """A managed asyncpg connection pool.

    Usage::

        async with ferrum.connect("postgresql://user@host/db") as conn:
            results = await MyModel.objects.filter(active=True).all()
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        """Open the connection pool."""
        diag = _redacted_dsn_info(self._dsn)
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
                f"{type(exc).__name__}"
            ) from None  # suppress raw asyncpg exc to avoid leaking DSN in __cause__

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self) -> Connection:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


@contextlib.asynccontextmanager
async def connect(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncGenerator[Connection, None]:
    """Async context manager that yields an open Ferrum connection pool.

    The DSN is never logged or included in default hook payloads (CRED-1).
    Connection errors expose only host/port/database/username/error category.

    Example::

        async with ferrum.connect("postgresql://user@host/db") as conn:
            ...
    """
    conn = Connection(dsn, min_size=min_size, max_size=max_size)
    try:
        await conn.open()
        yield conn
    finally:
        await conn.close()
