"""Ferrum connection pool and DSN configuration.

Wraps dialect-specific async drivers with:
- Redacted diagnostics: connection errors report host/port/database/username
  and an error category, never the password or full DSN (CRED-1).
- Async context-manager interface for connection lifecycle.
- ``FERRUM_DATABASE_URL`` environment variable auto-detection (DX blocker B-5).

This module owns the async I/O path; no SQL building or Rust calls happen here.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from ferrum.drivers import DriverProtocol, get_driver_for_dsn
from ferrum.errors import FerrumConfigError, FerrumConnectionError, FerrumError, map_db_error


def _redacted_dsn_info(dsn: str) -> dict[str, str]:
    """Extract safe connection diagnostics from a DSN — never the password."""
    try:
        parsed = urlparse(dsn)
        default_port = "5432" if parsed.scheme.startswith("postgres") else "3306"
        if parsed.scheme.startswith("sqlite"):
            default_port = "0"
        return {
            "host": parsed.hostname or ("memory" if ":memory:" in dsn else "unknown"),
            "port": str(parsed.port or default_port),
            "database": (parsed.path or "").lstrip("/") or "unknown",
            "username": parsed.username or "unknown",
        }
    except Exception:
        return {"host": "unknown", "port": "unknown", "database": "unknown", "username": "unknown"}


class Connection:
    """A managed async database connection (pool or single connection).

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
        self._driver: DriverProtocol | None = None

    @property
    def dialect(self) -> str:
        """Dialect for Rust SQL compilation: ``postgres``, ``mysql``, or ``sqlite``."""
        if self._driver is not None:
            return self._driver.dialect
        scheme = urlparse(self._dsn).scheme.lower()
        if scheme in ("postgresql", "postgres"):
            return "postgres"
        if scheme in ("mysql", "mysql+asyncmy"):
            return "mysql"
        if scheme in ("sqlite", "sqlite+aiosqlite"):
            return "sqlite"
        return "postgres"

    def _require_driver(self) -> DriverProtocol:
        """Return the open driver or raise FerrumConnectionError if not open."""
        if self._driver is None:
            raise FerrumConnectionError(
                "Connection is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open it first. "
                "[FERR-E101]"
            )
        return self._driver

    async def open(self) -> None:
        """Open the database connection (pool or single connection)."""
        self._driver = get_driver_for_dsn(
            self._dsn, min_size=self._min_size, max_size=self._max_size
        )
        try:
            await self._driver.open()
        except FerrumError:
            self._driver = None
            raise
        except Exception as exc:
            self._driver = None
            diag = _redacted_dsn_info(self._dsn)
            raise FerrumConnectionError(
                f"Failed to connect at {diag['host']}:{diag['port']} "
                f"(database={diag['database']}, username={diag['username']}): "
                f"{type(exc).__name__} [FERR-E101]"
            ) from None

    async def close(self) -> None:
        """Close the database connection."""
        if self._driver is not None:
            try:
                await self._driver.close()
            except FerrumError:
                raise
            except Exception as exc:
                raise map_db_error(exc) from None
            finally:
                self._driver = None

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        """Acquire a raw driver connection for a transaction or batch of statements."""
        driver = self._require_driver()
        acquire_cm = getattr(driver, "acquire", None)
        if acquire_cm is not None and callable(acquire_cm):
            async with acquire_cm() as raw_conn:
                yield raw_conn
            return
        yield driver

    async def release(self, raw_conn: Any) -> None:  # noqa: ANN401
        """Release a raw connection back to the pool when supported."""
        driver = self._require_driver()
        release_fn = getattr(driver, "release", None)
        if release_fn is not None and callable(release_fn):
            try:
                await release_fn(raw_conn)
            except FerrumError:
                raise
            except Exception as exc:
                raise map_db_error(exc) from None

    async def __aenter__(self) -> Connection:
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
    """Async context manager that yields an open Ferrum connection.

    If ``dsn`` is omitted, the ``FERRUM_DATABASE_URL`` environment variable is
    used. Raises ``FerrumConfigError`` if neither is provided (DX blocker B-5).
    """
    conn = Connection(dsn, min_size=min_size, max_size=max_size)
    try:
        await conn.open()
        yield conn
    finally:
        await conn.close()
