"""Ferrum connection pool and DSN configuration.

Wraps dialect-specific async drivers with:
- Redacted diagnostics: connection errors report host/port/database/username
  and an error category, never the password or full DSN (CRED-1).
- Async context-manager interface for connection lifecycle.
- ``FERRUM_DATABASE_URL`` environment variable auto-detection (DX blocker B-5).

This module owns the async I/O path; no SQL building or Rust calls happen here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from ferrum.drivers import get_driver_for_dsn
from ferrum.drivers.protocol import DriverProtocol, QueryExecutorProtocol
from ferrum.errors import (
    FerrumConfigError,
    FerrumConnectionError,
    FerrumError,
    FerrumTimeoutError,
    map_db_error,
)

# PostgreSQL transaction isolation levels accepted by ``Connection.transaction``.
# Validated as a fixed allowlist so an unknown value fails with a clear Ferrum
# error before it ever reaches the driver (no interpolation of caller input).
_ISOLATION_LEVELS: frozenset[str] = frozenset(
    {"serializable", "repeatable_read", "read_committed", "read_uncommitted"}
)


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

    def _require_driver(self) -> QueryExecutorProtocol:
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

    @contextlib.asynccontextmanager
    async def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
        deadline: float | None = None,
    ) -> AsyncGenerator[Transaction, None]:
        """Run a unit of work inside a database transaction.

        Yields a :class:`Transaction` that is accepted anywhere a ``Connection`` is
        — pass it to QuerySet terminals and they all share one pinned connection::

            async with conn.transaction() as tx:
                user = await User.objects.create(tx, email="a@example.com")
                await AuditLog.objects.create(tx, user_id=user.id, action="created")

        Commits on clean exit; rolls back on any exception or cancellation and
        releases the pinned connection. ``isolation`` (one of ``serializable`` /
        ``repeatable_read`` / ``read_committed`` / ``read_uncommitted``),
        ``readonly``, and ``deferrable`` map to the BEGIN modifiers. ``deadline``
        (seconds) bounds the whole block at the Python await point — never inside
        Rust — and rolls back with :class:`FerrumTimeoutError` if exceeded.

        Args:
            isolation: Transaction isolation level, or ``None`` for the server default.
            readonly: Open the transaction in READ ONLY mode.
            deferrable: DEFERRABLE mode (only meaningful for SERIALIZABLE READ ONLY).
            deadline: Optional wall-clock budget in seconds for the entire block.

        Raises:
            FerrumConfigError: if ``isolation`` is not an allowed level, or the
                active driver does not support transactions.
            FerrumTimeoutError: if ``deadline`` is exceeded (after rollback).
        """
        if isolation is not None and isolation not in _ISOLATION_LEVELS:
            raise FerrumConfigError(
                f"Unknown transaction isolation level {isolation!r}. Expected one of: "
                f"{', '.join(sorted(_ISOLATION_LEVELS))}. [FERR-C001]"
            )
        driver = self._require_driver()
        tx_factory = getattr(driver, "transaction", None)
        if tx_factory is None or not callable(tx_factory):
            raise FerrumConfigError(
                f"The active {self.dialect!r} driver does not support transactions. "
                "Transactions require the PostgreSQL (asyncpg) driver in v0.1. [FERR-C001]"
            )
        tx_cm = tx_factory(isolation=isolation, readonly=readonly, deferrable=deferrable)
        dialect = self.dialect
        try:
            if deadline is not None:
                async with asyncio.timeout(deadline), tx_cm as bound:
                    yield Transaction(bound, dialect)
            else:
                async with tx_cm as bound:
                    yield Transaction(bound, dialect)
        except TimeoutError:
            raise FerrumTimeoutError(
                f"Transaction exceeded its deadline of {deadline}s and was rolled back. [FERR-E102]"
            ) from None

    async def __aenter__(self) -> Connection:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class Transaction:
    """A transaction-scoped handle, usable anywhere a :class:`Connection` is.

    Holds a single connection pinned by ``Connection.transaction`` for the life of
    the transaction and exposes the same minimal surface QuerySet terminals rely on
    — ``dialect`` and ``_require_driver()`` — so terminals execute against the
    pinned connection instead of acquiring a fresh pooled one. Obtain it via
    ``async with conn.transaction() as tx:``; do not construct it directly.
    """

    def __init__(self, bound: Any, dialect: str) -> None:  # noqa: ANN401
        self._bound = bound
        self._dialect = dialect

    @property
    def dialect(self) -> str:
        """Dialect for Rust SQL compilation, inherited from the parent connection."""
        return self._dialect

    def _require_driver(self) -> QueryExecutorProtocol:
        """Return the pinned execution surface (matches ``Connection._require_driver``)."""
        return self._bound

    @contextlib.asynccontextmanager
    async def savepoint(self) -> AsyncGenerator[Transaction, None]:
        """Nest a SAVEPOINT inside this transaction.

        The yielded :class:`Transaction` runs on the same pinned connection. An
        exception inside the block rolls back only to the savepoint, leaving the
        enclosing transaction intact; clean exit releases the savepoint::

            async with conn.transaction() as tx:
                await A.objects.create(tx, ...)
                try:
                    async with tx.savepoint() as sp:
                        await B.objects.create(sp, ...)   # rolled back on error
                except FerrumError:
                    ...                                    # A's insert survives
        """
        sp = getattr(self._bound, "savepoint", None)
        if sp is None or not callable(sp):
            raise FerrumConfigError("The active driver does not support savepoints. [FERR-C001]")
        async with sp() as sp_bound:
            yield Transaction(sp_bound, self._dialect)


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
