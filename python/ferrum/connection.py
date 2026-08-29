"""Ferrum connection pool and DSN configuration.

Wraps dialect-specific async drivers with:
- Redacted diagnostics: connection errors report host/port/database/username
  and an error category, never the password or full DSN (CRED-1).
- Async context-manager interface for connection lifecycle.
- ``FERRUM_DATABASE_URL`` environment variable auto-detection, with
  ``DATABASE_URL`` fallback and optional ``[ferrum].database_url_env`` override
  via ``ferrum.toml`` (DX blocker B-5).
- Event-based shutdown: ``close()`` drains in-flight work via an
  ``asyncio.Event`` (not busy polling) and reports a forced drain timeout
  without leaking connections.
- Typed ``PoolStats`` snapshot combining pool internals with Ferrum lifecycle.

This module owns the async I/O path; no SQL building or Rust calls happen here.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator, AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ferrum.config import database_url_env_hint, resolve_database_url_for_cwd
from ferrum.drivers import get_driver_for_dsn
from ferrum.drivers.postgres import PoolStats
from ferrum.drivers.protocol import CompiledQuery, DriverProtocol, QueryExecutorProtocol
from ferrum.errors import (
    FerrumConfigError,
    FerrumConnectionError,
    FerrumError,
    FerrumTimeoutError,
    map_db_error,
)
from ferrum.runtime import (
    ManagedChunkStream,
    RetryPolicy,
    RuntimeConfig,
    TimedQueryExecutor,
    _LifecycleGuard,
)

if TYPE_CHECKING:
    from ssl import SSLContext

# PostgreSQL transaction isolation levels accepted by ``Connection.transaction``.
# Validated as a fixed allowlist so an unknown value fails with a clear Ferrum
# error before it ever reaches the driver (no interpolation of caller input).
_ISOLATION_LEVELS: frozenset[str] = frozenset(
    {"serializable", "repeatable_read", "read_committed", "read_uncommitted"}
)

# Identifier validation pattern for call_function: letters/digits/underscores,
# must start with a letter or underscore, max 63 chars (PostgreSQL limit).
# Prevents SQL injection via function_name or schema — never accept user input.
_IDENT_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


def _validate_pg_identifier(value: str, label: str) -> None:
    """Raise FerrumCompileError if value is not a safe PostgreSQL identifier.

    Args:
        value: The identifier to validate (e.g. function name or schema).
        label: Human-readable label used in the error message.

    Raises:
        FerrumCompileError: If ``value`` does not match the safe identifier pattern.
    """
    from ferrum.errors import FerrumCompileError

    if not _IDENT_RE.match(value):
        raise FerrumCompileError(
            f"Invalid PostgreSQL identifier for {label}: {value!r}. "
            "Identifiers must start with a letter or underscore, contain only "
            "letters, digits, and underscores, and be at most 63 characters. "
            "Do not construct function names from user-supplied input. [FERR-C102]",
            category="invalid_identifier",
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


class _EventLifecycleGuard(_LifecycleGuard):
    """Lifecycle guard with event-based drain (no busy polling).

    When in-flight operations complete (``end()`` decrements to 0), the
    ``_drained`` event is set so ``close()`` can ``await`` it instead of
    polling ``inflight`` in a sleep loop. This replaces the busy-poll
    ``drain_inflight()`` from ``runtime.py`` with a deterministic event wait.

    Subclasses ``_LifecycleGuard`` (from ``runtime.py``, not owned by W1-E)
    so ``ManagedChunkStream`` and ``TimedQueryExecutor`` — which call
    ``begin()`` / ``end()`` on the lifecycle object — transparently use the
    event-aware implementation.
    """

    def __init__(self) -> None:
        super().__init__()
        self._drained = asyncio.Event()
        self._drained.set()  # Nothing in-flight initially → drained.

    def begin(self) -> None:
        super().begin()
        self._drained.clear()

    def end(self) -> None:
        super().end()
        if self._inflight == 0:
            self._drained.set()

    async def wait_drained(self, *, timeout: float) -> bool:
        """Wait for all in-flight work to complete.

        Returns ``True`` if drained within ``timeout``, ``False`` on timeout.
        This is the event-based replacement for ``drain_inflight()``.
        """
        if self._inflight == 0:
            return True
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False


class Connection:
    """A managed async database connection (pool or single connection).

    Usage::

        async with ferrum.connect("postgresql://user@host/db") as conn:
            results = await MyModel.objects.filter(active=True).all(conn)

    The DSN can also be supplied via environment variables when the ``dsn``
    argument is omitted: by default ``FERRUM_DATABASE_URL``, then
    ``DATABASE_URL``. Configure ``[ferrum].database_url_env`` in
    ``ferrum.toml`` to use a different variable name.

    Pool configuration (all optional):

    - ``min_size`` / ``max_size``: pool sizing bounds.
    - ``acquire_timeout``: seconds to wait for a pooled connection (enforced
      on every acquire path — convenience ``fetch``/``execute``, transactions,
      streams, and explicit ``acquire()``).
    - ``query_timeout``: per-query Python-side deadline (seconds).
    - ``statement_timeout``: server-side ``statement_timeout`` (milliseconds).
    - ``max_lifetime``: legacy alias for ``max_idle_lifetime``.
    - ``max_idle_lifetime``: recycle idle connections after this many seconds.
    - ``max_connection_age``: hard max age for connections (seconds).
    - ``command_timeout``: per-command timeout (seconds, PostgreSQL only).
    - ``statement_cache_size``: asyncpg prepared-statement cache size.
    - ``ssl``: SSL configuration (``True``, mode string, ``SSLContext``, or
      ``None``). PostgreSQL only.
    - ``server_settings``: PostgreSQL GUC overrides (``dict[str, str]``).
    - ``application_name``: PostgreSQL ``application_name`` (folded into
      ``server_settings``).
    - ``retry``: explicit :class:`RetryPolicy` (default: no retries).
    - ``drain_timeout``: seconds to wait for in-flight work on ``close()``.
    - ``echo``: SQLAlchemy-like console logging.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
        acquire_timeout: float | None = None,
        query_timeout: float | None = None,
        statement_timeout: int | None = None,
        max_lifetime: float | None = None,
        max_idle_lifetime: float | None = None,
        max_connection_age: float | None = None,
        command_timeout: float | None = None,
        statement_cache_size: int | None = None,
        ssl: bool | str | SSLContext | None = None,
        server_settings: dict[str, str] | None = None,
        application_name: str | None = None,
        retry: RetryPolicy | None = None,
        drain_timeout: float = 30.0,
        echo: bool | str = False,
    ) -> None:
        if dsn is None:
            dsn, database_url_env = resolve_database_url_for_cwd()
        if dsn is None:
            hint = database_url_env_hint(database_url_env=database_url_env)
            raise FerrumConfigError(
                "No database URL provided. Pass a DSN to ferrum.connect() or set the "
                f"{hint} environment variable. [FERR-C001]"
            )
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._max_idle_lifetime = max_idle_lifetime
        self._max_connection_age = max_connection_age
        self._command_timeout = command_timeout
        self._statement_cache_size = statement_cache_size
        self._ssl = ssl
        self._server_settings = server_settings
        self._application_name = application_name
        self._runtime = RuntimeConfig(
            acquire_timeout=acquire_timeout,
            query_timeout=query_timeout,
            statement_timeout_ms=statement_timeout,
            max_lifetime=max_lifetime,
            retry=retry,
            drain_timeout=drain_timeout,
        )
        self._lifecycle = _EventLifecycleGuard()
        self._driver: DriverProtocol | None = None
        # SQLAlchemy-like console echo: False | True/"sql" | "debug"/"verbose".
        self._echo: bool | str = echo

    @property
    def dialect(self) -> str:
        """Dialect for Rust SQL compilation: ``postgres``, ``mysql``, ``sqlite``, or ``mssql``."""
        if self._driver is not None:
            return self._driver.dialect
        scheme = urlparse(self._dsn).scheme.lower()
        if scheme in ("postgresql", "postgres"):
            return "postgres"
        if scheme in ("mysql", "mysql+asyncmy"):
            return "mysql"
        if scheme in ("sqlite", "sqlite+aiosqlite"):
            return "sqlite"
        if scheme in ("mssql", "sqlserver"):
            return "mssql"
        return "postgres"

    def _require_driver(self) -> QueryExecutorProtocol:
        """Return the open driver or raise FerrumConnectionError if not open."""
        if self._driver is None:
            raise FerrumConnectionError(
                "Connection is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open it first. "
                "[FERR-E101]",
                category="config",
            )
        self._lifecycle.reject_if_closing()
        return TimedQueryExecutor(
            self._driver,
            runtime=self._runtime,
            lifecycle=self._lifecycle,
        )

    async def open(self) -> None:
        """Open the database connection (pool or single connection)."""
        self._lifecycle = _EventLifecycleGuard()

        # Determine which kwargs the driver accepts based on dialect.
        # Non-PostgreSQL drivers have fixed __init__ signatures and do not
        # accept the new pool config knobs (command_timeout, ssl, etc.).
        scheme = urlparse(self._dsn).scheme.lower()
        driver_kwargs: dict[str, Any] = {
            "min_size": self._min_size,
            "max_size": self._max_size,
            "acquire_timeout": self._runtime.acquire_timeout,
            "statement_timeout_ms": self._runtime.statement_timeout_ms,
            "max_lifetime": self._runtime.max_lifetime,
        }
        if scheme in ("postgresql", "postgres"):
            driver_kwargs.update(
                max_idle_lifetime=self._max_idle_lifetime,
                max_connection_age=self._max_connection_age,
                command_timeout=self._command_timeout,
                statement_cache_size=self._statement_cache_size,
                ssl=self._ssl,
                server_settings=self._server_settings,
                application_name=self._application_name,
            )

        self._driver = get_driver_for_dsn(self._dsn, **driver_kwargs)
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
                f"{type(exc).__name__} [FERR-E101]",
                category="connection",
            ) from None

    async def close(self) -> None:
        """Gracefully close the pool: stop accepting, drain in-flight, then close.

        Uses an ``asyncio.Event`` (via ``_EventLifecycleGuard.wait_drained``)
        instead of busy polling. If in-flight work does not complete within
        ``drain_timeout``, the pool is still closed (no connection leak) and a
        ``FerrumTimeoutError`` is raised to report the forced drain timeout.
        """
        if self._driver is None:
            return
        self._lifecycle.stop_accepting()
        await self._lifecycle.close_streams()
        drained = await self._lifecycle.wait_drained(timeout=self._runtime.drain_timeout)
        abandoned = self._lifecycle.inflight if not drained else 0
        try:
            await self._driver.close()
        except FerrumError:
            raise
        except Exception as exc:
            raise map_db_error(exc) from None
        finally:
            self._driver = None
        if not drained:
            raise FerrumTimeoutError(
                f"Connection pool close timed out: {abandoned} in-flight operation(s) "
                f"did not complete within {self._runtime.drain_timeout}s and may have "
                "been abandoned. [FERR-E102]",
                category="timeout",
            )

    async def health_check(self, *, timeout: float | None = 5.0) -> bool:
        """Run a cheap liveness probe (``SELECT 1``).

        Returns ``True`` when the database responds. Raises
        :class:`FerrumConnectionError` when the pool is closed or shutting down,
        and :class:`FerrumTimeoutError` when ``timeout`` elapses.
        """
        driver = self._require_driver()
        try:
            if timeout is not None:
                async with asyncio.timeout(timeout):
                    await driver.fetchval("SELECT 1")
            else:
                await driver.fetchval("SELECT 1")
        except TimeoutError:
            raise FerrumTimeoutError(
                f"Health check exceeded its {timeout}s deadline. [FERR-E102]",
                category="timeout",
            ) from None
        except FerrumError:
            raise
        except Exception as exc:
            raise map_db_error(exc) from None
        return True

    def pool_stats(self) -> PoolStats | None:
        """Return a typed snapshot of pool state, or ``None`` if unavailable.

        Combines driver-level pool internals with Ferrum lifecycle state
        (in-flight operations, accepting/closing). Returns ``None`` when the
        pool is not open or the driver does not expose ``pool_stats()``.
        """
        if self._driver is None:
            return None
        pool_stats_fn = getattr(self._driver, "pool_stats", None)
        if pool_stats_fn is None or not callable(pool_stats_fn):
            return None
        return pool_stats_fn(
            inflight=self._lifecycle.inflight,
            accepting=self._lifecycle.accepting,
        )

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        """Acquire a raw driver connection for a transaction or batch of statements."""
        if self._driver is None:
            raise FerrumConnectionError(
                "Connection is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open it first. "
                "[FERR-E101]",
                category="config",
            )
        self._lifecycle.reject_if_closing()
        self._lifecycle.begin()
        raw_driver = self._driver
        acquire_cm = getattr(raw_driver, "acquire", None)
        try:
            if acquire_cm is not None and callable(acquire_cm):
                async with acquire_cm() as raw_conn:
                    yield raw_conn
                return
            yield raw_driver
        except FerrumError:
            raise
        except Exception as exc:
            raise map_db_error(exc) from None
        finally:
            self._lifecycle.end()

    async def release(self, raw_conn: Any) -> None:  # noqa: ANN401
        """Release a raw connection back to the pool when supported."""
        if self._driver is None:
            raise FerrumConnectionError(
                "Connection is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open it first. "
                "[FERR-E101]",
                category="config",
            )
        driver = self._driver
        release_fn = getattr(driver, "release", None)
        if release_fn is not None and callable(release_fn):
            try:
                await release_fn(raw_conn)
            except FerrumError:
                raise
            except Exception as exc:
                raise map_db_error(exc) from None

    @contextlib.asynccontextmanager
    async def stream_compiled(
        self,
        compiled: CompiledQuery,
        *,
        chunk_size: int = 1000,
    ) -> AsyncGenerator[AsyncIterator[list[Any]], None]:
        """Stream compiler-produced rows in bounded chunks.

        The context manager pins one pooled connection and transaction for the
        server cursor's lifetime. It must be used with ``async with`` so early
        loop exit deterministically closes the cursor and releases the pool
        connection. Application-provided SQL strings are intentionally not
        accepted; ``compiled`` is an opaque value created by Ferrum's compiler.
        """
        stream = _open_compiled_stream(
            self._driver,
            self._lifecycle,
            self._runtime,
            self.dialect,
            compiled,
            chunk_size=chunk_size,
        )
        try:
            yield stream
        finally:
            await stream.aclose()

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
        if self._driver is None:
            raise FerrumConnectionError(
                "Connection is not open. "
                "Use 'async with ferrum.connect(...) as conn:' to open it first. "
                "[FERR-E101]",
                category="config",
            )
        self._lifecycle.reject_if_closing()
        driver = self._driver
        tx_factory = getattr(driver, "transaction", None)
        if tx_factory is None or not callable(tx_factory):
            raise FerrumConfigError(
                f"The active {self.dialect!r} driver does not support transactions. "
                "Transactions require the PostgreSQL (asyncpg) driver; the MySQL, "
                "SQLite, and MSSQL backends are thin-parity and omit them. [FERR-C001]"
            )
        tx_cm = tx_factory(isolation=isolation, readonly=readonly, deferrable=deferrable)
        dialect = self.dialect
        try:
            if deadline is not None:
                async with asyncio.timeout(deadline), tx_cm as bound:
                    yield Transaction(
                        bound,
                        dialect,
                        runtime=self._runtime,
                        lifecycle=self._lifecycle,
                        echo=self._echo,
                    )
            else:
                async with tx_cm as bound:
                    yield Transaction(
                        bound,
                        dialect,
                        runtime=self._runtime,
                        lifecycle=self._lifecycle,
                        echo=self._echo,
                    )
        except TimeoutError:
            raise FerrumTimeoutError(
                "Transaction exceeded its deadline of "
                f"{deadline}s and was rolled back. [FERR-E102]",
                category="timeout",
            ) from None

    async def call_function(
        self,
        function_name: str,
        *args: object,
        schema: str = "public",
    ) -> list[dict[str, Any]]:
        """Call a PostgreSQL function with bound arguments.

        ``function_name`` and ``schema`` are validated against an identifier
        allowlist (letters, digits, underscores only; max 63 chars) — never
        interpolated from user input. Arguments are always bound parameters.

        Emits ``SELECT * FROM "schema"."function_name"($1, $2, ...)`` with
        double-quoted identifiers and bound values.

        Returns a list of row dicts (empty list for void functions).
        PostgreSQL only.

        SecurityEngineer note: this is an allowlisted call surface. Applications
        must not construct ``function_name`` or ``schema`` from user-supplied input.

        Args:
            function_name: Name of the PostgreSQL function to call. Must match
                ``^[a-zA-Z_][a-zA-Z0-9_]{0,62}$``.
            *args: Positional arguments passed as bound parameters ($1, $2, …).
            schema: Schema name (default: ``"public"``). Same identifier constraints
                as ``function_name``.

        Returns:
            A list of row dicts. Empty for void or zero-row functions.

        Raises:
            FerrumCompileError: If ``function_name`` or ``schema`` fails identifier
                validation.
            FerrumConnectionError: If the connection is not open.
        """
        _validate_pg_identifier(function_name, "function_name")
        _validate_pg_identifier(schema, "schema")
        driver = self._require_driver()
        placeholders = ", ".join(f"${i + 1}" for i in range(len(args)))
        # S608 suppressed: identifiers are allowlist-validated above — not user input.
        sql = f'SELECT * FROM "{schema}"."{function_name}"({placeholders})'  # noqa: S608
        try:
            rows = await driver.fetch(sql, *args)
        except FerrumError:
            raise
        except Exception as exc:
            raise map_db_error(exc) from None
        return [dict(row) for row in rows]

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

    def __init__(
        self,
        bound: Any,  # noqa: ANN401
        dialect: str,
        *,
        runtime: RuntimeConfig | None = None,
        lifecycle: _LifecycleGuard | None = None,
        echo: bool | str = False,
    ) -> None:
        self._bound = bound
        self._dialect = dialect
        self._runtime = runtime or RuntimeConfig()
        self._lifecycle = lifecycle or _EventLifecycleGuard()
        self._echo: bool | str = echo

    @property
    def dialect(self) -> str:
        """Dialect for Rust SQL compilation, inherited from the parent connection."""
        return self._dialect

    def _require_driver(self) -> QueryExecutorProtocol:
        """Return the pinned execution surface (matches ``Connection._require_driver``)."""
        self._lifecycle.reject_if_closing()
        return TimedQueryExecutor(
            self._bound,
            runtime=self._runtime,
            lifecycle=self._lifecycle,
        )

    @contextlib.asynccontextmanager
    async def stream_compiled(
        self,
        compiled: CompiledQuery,
        *,
        chunk_size: int = 1000,
    ) -> AsyncGenerator[AsyncIterator[list[Any]], None]:
        """Stream compiler-produced rows on this transaction's pinned connection."""
        stream = _open_compiled_stream(
            self._bound,
            self._lifecycle,
            self._runtime,
            self._dialect,
            compiled,
            chunk_size=chunk_size,
        )
        try:
            yield stream
        finally:
            await stream.aclose()

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
            yield Transaction(
                sp_bound,
                self._dialect,
                runtime=self._runtime,
                lifecycle=self._lifecycle,
                echo=self._echo,
            )

    async def call_function(
        self,
        function_name: str,
        *args: object,
        schema: str = "public",
    ) -> list[dict[str, Any]]:
        """Call a PostgreSQL function within this transaction with bound arguments.

        Identical contract to :meth:`Connection.call_function` but executes on the
        pinned transaction connection so the call participates in the current
        transaction.

        ``function_name`` and ``schema`` are validated against an identifier
        allowlist — never interpolated from user input.

        SecurityEngineer note: this is an allowlisted call surface. Applications
        must not construct ``function_name`` or ``schema`` from user-supplied input.

        Args:
            function_name: Name of the PostgreSQL function to call. Must match
                ``^[a-zA-Z_][a-zA-Z0-9_]{0,62}$``.
            *args: Positional arguments passed as bound parameters ($1, $2, …).
            schema: Schema name (default: ``"public"``). Same identifier constraints
                as ``function_name``.

        Returns:
            A list of row dicts. Empty for void or zero-row functions.

        Raises:
            FerrumCompileError: If ``function_name`` or ``schema`` fails identifier
                validation.
        """
        _validate_pg_identifier(function_name, "function_name")
        _validate_pg_identifier(schema, "schema")
        driver = self._require_driver()
        placeholders = ", ".join(f"${i + 1}" for i in range(len(args)))
        # S608 suppressed: identifiers are allowlist-validated above — not user input.
        sql = f'SELECT * FROM "{schema}"."{function_name}"({placeholders})'  # noqa: S608
        rows = await driver.fetch(sql, *args)
        return [dict(row) for row in rows]


# Shared by QuerySet terminals and relation prefetch helpers.
ConnectionLike = Connection | Transaction


def _open_compiled_stream(
    driver: object | None,
    lifecycle: _LifecycleGuard,
    runtime: RuntimeConfig,
    dialect: str,
    compiled: CompiledQuery,
    *,
    chunk_size: int,
) -> ManagedChunkStream:
    """Open the shared Connection/Transaction streaming execution seam."""
    lifecycle.reject_if_closing()
    if not isinstance(compiled, CompiledQuery):
        raise TypeError("stream_compiled() requires Ferrum compiler output.")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1.")
    if dialect != "postgres":
        raise FerrumConfigError(
            "Bounded cursor streaming currently requires the PostgreSQL (asyncpg) "
            "driver. [FERR-C001]"
        )
    if driver is None:
        raise FerrumConnectionError(
            "Connection is not open. "
            "Use 'async with ferrum.connect(...) as conn:' to open it first. "
            "[FERR-E101]",
            category="config",
        )
    open_stream = getattr(driver, "open_stream", None)
    if open_stream is None or not callable(open_stream):
        raise FerrumConfigError(
            "The active PostgreSQL driver does not support bounded streaming. [FERR-C001]"
        )
    inner = open_stream(
        compiled,
        chunk_size=chunk_size,
        query_timeout=runtime.query_timeout,
    )
    return ManagedChunkStream(inner, lifecycle)


@contextlib.asynccontextmanager
async def connect(
    dsn: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
    acquire_timeout: float | None = None,
    query_timeout: float | None = None,
    statement_timeout: int | None = None,
    max_lifetime: float | None = None,
    max_idle_lifetime: float | None = None,
    max_connection_age: float | None = None,
    command_timeout: float | None = None,
    statement_cache_size: int | None = None,
    ssl: bool | str | SSLContext | None = None,
    server_settings: dict[str, str] | None = None,
    application_name: str | None = None,
    retry: RetryPolicy | None = None,
    drain_timeout: float = 30.0,
    echo: bool | str = False,
) -> AsyncGenerator[Connection, None]:
    """Async context manager that yields an open Ferrum connection.

    If ``dsn`` is omitted, environment variables are consulted: by default
    ``FERRUM_DATABASE_URL``, then ``DATABASE_URL``. ``[ferrum].database_url_env``
    in ``ferrum.toml`` overrides which variable is read. Raises
    ``FerrumConfigError`` if neither is provided (DX blocker B-5).

    Production runtime options (all optional):

    - ``acquire_timeout``: seconds to wait for a pooled connection (enforced
      on every acquire path).
    - ``query_timeout``: per-query Python-side deadline (seconds).
    - ``statement_timeout``: server-side ``statement_timeout`` (milliseconds).
    - ``max_lifetime``: legacy alias for ``max_idle_lifetime``.
    - ``max_idle_lifetime``: recycle idle connections after this many seconds.
    - ``max_connection_age``: hard max age for connections (seconds).
    - ``command_timeout``: per-command timeout (seconds, PostgreSQL only).
    - ``statement_cache_size``: asyncpg prepared-statement cache size.
    - ``ssl``: SSL configuration (``True``, mode string, ``SSLContext``, or
      ``None``). PostgreSQL only.
    - ``server_settings``: PostgreSQL GUC overrides (``dict[str, str]``).
    - ``application_name``: PostgreSQL ``application_name``.
    - ``retry``: explicit :class:`RetryPolicy` (default: no retries).
    - ``drain_timeout``: seconds to wait for in-flight work on ``close()``.
    - ``echo``: SQLAlchemy-like console logging — ``True``/``"sql"`` prints
      SQL + param type summary; ``"debug"``/``"verbose"`` also prints bound
      values (local-dev only). Also controllable via ``ferrum.enable_echo()``
      or ``FERRUM_ECHO=1|debug``.
    """
    conn = Connection(
        dsn,
        min_size=min_size,
        max_size=max_size,
        acquire_timeout=acquire_timeout,
        query_timeout=query_timeout,
        statement_timeout=statement_timeout,
        max_lifetime=max_lifetime,
        max_idle_lifetime=max_idle_lifetime,
        max_connection_age=max_connection_age,
        command_timeout=command_timeout,
        statement_cache_size=statement_cache_size,
        ssl=ssl,
        server_settings=server_settings,
        application_name=application_name,
        retry=retry,
        drain_timeout=drain_timeout,
        echo=echo,
    )
    try:
        await conn.open()
        yield conn
    finally:
        await conn.close()
