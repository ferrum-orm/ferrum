"""Ferrum connection pool and DSN configuration.

Wraps asyncpg with:
- Redacted diagnostics: connection errors report host/port/database/username
  and an error category, never the password or full DSN (CRED-1).
- TLS configuration via ``sslmode``.
- Async context-manager interface for pool lifecycle.
- ``FERRUM_DATABASE_URL`` environment variable auto-detection (DX blocker B-5).

This module owns the async I/O path; no SQL building or Rust calls happen here.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

import asyncpg  # type: ignore[import-untyped]

from ferrum.errors import FerrumConfigError, FerrumConnectionError, FerrumError, map_db_error


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
    except Exception:
        return {"host": "unknown", "port": "unknown", "database": "unknown", "username": "unknown"}


class Connection:
    """A managed asyncpg connection pool.

    Usage::

        async with ferrum.connect("postgresql://user@host/db") as conn:
            results = await MyModel.objects.filter(active=True).all(conn)

    The DSN can also be supplied via the ``FERRUM_DATABASE_URL`` environment
    variable when the ``dsn`` argument is omitted.
    """

    def __init__(self, dsn: str | None = None, *, min_size: int = 1, max_size: int = 10) -> None:
        if dsn is None:
            dsn = os.environ.get("FERRUM_DATABASE_URL")
        if dsn is None:
            raise FerrumConfigError(
                "No database URL provided. Pass a DSN to ferrum.connect() or set the "
                "FERRUM_DATABASE_URL environment variable. [FERR-C001]"
            )
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None  # type: ignore[type-arg]

    def _require_pool(self) -> Any:  # noqa: ANN401
        """Return the open pool or raise FerrumConnectionError if not open."""
        if self._pool is None:
            raise FerrumConnectionError(
                "Connection pool is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open the pool first. "
                "[FERR-E101]"
            )
        return self._pool

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
                f"{type(exc).__name__} [FERR-E101]"
            ) from None  # suppress raw asyncpg exc to avoid leaking DSN in __cause__

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            try:
                await self._pool.close()
            except FerrumError:
                raise
            except Exception as exc:
                raise map_db_error(exc) from None
            finally:
                self._pool = None

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        """Acquire a raw asyncpg connection from the pool.

        Yields a single ``asyncpg.Connection`` for use in a single transaction
        or batch of statements. The connection is released back to the pool on
        context manager exit. asyncpg exceptions are mapped to the Ferrum
        taxonomy before escaping (M-1 / ERR-1).

        Example::

            async with conn.acquire() as raw:
                await raw.execute("SELECT 1")
        """
        pool = self._require_pool()
        try:
            async with pool.acquire() as raw_conn:
                yield raw_conn
        except FerrumError:
            raise
        except Exception as exc:
            raise map_db_error(exc) from None

    async def release(self, raw_conn: Any) -> None:  # noqa: ANN401
        """Release a raw asyncpg connection back to the pool.

        Prefer ``acquire()`` context manager for automatic release.
        Only use this for manual acquire/release flows. asyncpg exceptions are
        mapped to the Ferrum taxonomy before escaping (M-1 / ERR-1).
        """
        pool = self._require_pool()
        try:
            await pool.release(raw_conn)
        except FerrumError:
            raise
        except Exception as exc:
            raise map_db_error(exc) from None

    async def __aenter__(self) -> Connection:
        """Open the pool on context entry and return this connection."""
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


@contextlib.asynccontextmanager
async def connect(
    dsn: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncGenerator[Connection, None]:
    """Async context manager that yields an open Ferrum connection pool.

    If ``dsn`` is omitted, the ``FERRUM_DATABASE_URL`` environment variable is
    used. Raises ``FerrumConfigError`` if neither is provided (DX blocker B-5).

    The DSN is never logged or included in default hook payloads (CRED-1).
    Connection errors expose only host/port/database/username/error category.

    Example::

        async with ferrum.connect("postgresql://user@host/db") as conn:
            users = await User.objects.filter(active=True).all(conn)

        # or with FERRUM_DATABASE_URL set in the environment:
        async with ferrum.connect() as conn:
            ...
    """
    conn = Connection(dsn, min_size=min_size, max_size=max_size)
    try:
        await conn.open()
        yield conn
    finally:
        await conn.close()
