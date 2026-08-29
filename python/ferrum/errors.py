"""Ferrum exception taxonomy and centralized error boundary (ADR-006).

All exceptions raised to application code are subclasses of ``FerrumError``.
Internal exceptions from asyncpg, PyO3, and PostgreSQL are mapped here via
``map_db_error()`` — raw ``DETAIL``/``HINT`` containing row data is never
propagated by default (ERR-1).

PyO3 panics from the Rust core surface as ``FerrumInternalError`` (ERR-2).
No exception message ever contains bound parameter values, DSNs, or passwords.

Stable error codes (``FERR-XXXX``) are class-level attributes and appear in
rendered messages for documentation links and tooling (DX blocker B-6).

``map_native_error()`` is the single centralized seam for routing exceptions
from the ``ferrum._native`` PyO3 extension into the Ferrum taxonomy (ADR-006).

Sanctioned safe-error-field set (ratified §5a — "Safe error fields"):
- ``sqlstate`` — structured attribute on every mapped exception (not just
  migration message text).
- ``category`` — a stable string from a closed enum (``ERROR_CATEGORIES``).
- ``constraint`` — DB-reported constraint name only.
- ``model`` / ``operation`` — Ferrum-side metadata naming the table/model and
  the ORM call.

PostgreSQL ``DETAIL``, ``HINT``, bound parameter values, row data, and full
DSNs NEVER appear in any field, hook payload, log line, or exception message
at any tier.
"""

from __future__ import annotations

import asyncio
from typing import Any

try:
    import asyncpg.exceptions as _asyncpg_exc  # type: ignore[import-untyped]

    _HAS_ASYNCPG: bool = True
except ImportError:
    _asyncpg_exc = None  # type: ignore
    _HAS_ASYNCPG = False

try:
    import asyncmy.errors as _asyncmy_exc  # type: ignore[import-untyped]

    _HAS_ASYNCMY: bool = True
except ImportError:
    _asyncmy_exc = None
    _HAS_ASYNCMY = False

try:
    import aiosqlite  # type: ignore[import-untyped]

    _HAS_AIOSQLITE: bool = True
except ImportError:
    aiosqlite = None
    _HAS_AIOSQLITE = False

try:
    import pyodbc as _pyodbc  # type: ignore[import-untyped]

    _HAS_AIOODBC: bool = True
except ImportError:
    _pyodbc = None
    _HAS_AIOODBC = False


# ---------------------------------------------------------------------------
# Closed category enum (ratified §5a — "Safe error fields")
# ---------------------------------------------------------------------------

ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        # Database-level categories (derived from PostgreSQL SQLSTATE classes)
        "integrity",  # SQLSTATE class 23 — constraint violations (general)
        "integrity_error",  # backward-compatible alias for non-unique integrity
        "unique_violation",  # 23505
        "foreign_key_violation",  # 23503
        "not_null_violation",  # 23502
        "check_violation",  # 23514
        "schema",  # SQLSTATE class 42 — undefined table/column (general)
        "undefined_function",  # 42883
        "undefined_column",  # 42703
        "undefined_table",  # 42P01
        "serialization",  # 40001 — serialization failure
        "deadlock",  # 40P01 — deadlock detected
        "lock_timeout",  # 55P03 — lock not available / lock timeout
        "query_cancellation",  # 57014 — statement timeout or explicit cancel
        "invalid_transaction_state",  # SQLSTATE class 25
        "failover",  # 57P01/57P02/57P03 — admin/crash shutdown
        "connection",  # SQLSTATE class 08 — connection-level errors
        "timeout",  # asyncio.TimeoutError — pool-acquire or statement timeout
        "pool_exhaustion",  # 53300 — too many connections / pool exhausted
        # Ferrum-level categories (not from SQLSTATE)
        "compile_error",  # FerrumCompileError
        "hydration",  # FerrumHydrationError
        "internal",  # FerrumInternalError
        "config",  # FerrumConfigError
        "not_found",  # FerrumNotFoundError
        "multiple_objects",  # FerrumMultipleObjectsError
        "deferred_field",  # FerrumDeferredFieldError
        "relation_not_loaded",  # FerrumRelationNotLoadedError
        "danger_api",  # FerrumDangerApiError
        "migration",  # FerrumMigrationError
        "unknown",  # fallback for unmapped SQLSTATEs
    }
)


class FerrumError(Exception):
    """Base class for all Ferrum exceptions.

    Carries the ratified §5a sanctioned safe-error-field set:
    ``sqlstate``, ``category``, ``constraint``, ``model``, ``operation``.
    All default to ``None``; subclasses and mapping functions populate them.

    PostgreSQL ``DETAIL``/``HINT``, bound parameter values, row data, and
    full DSNs NEVER appear in any attribute or message (ERR-1).
    """

    code: str = "FERR-0000"
    sqlstate: str | None = None
    category: str | None = None
    constraint: str | None = None
    model: str | None = None
    operation: str | None = None

    def __init__(
        self,
        *args: str,
        sqlstate: str | None = None,
        category: str | None = None,
        constraint: str | None = None,
        model: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(*args)
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if category is not None:
            self.category = category
        if constraint is not None:
            self.constraint = constraint
        if model is not None:
            self.model = model
        if operation is not None:
            self.operation = operation


class FerrumConfigError(FerrumError):
    """Misconfiguration error: missing DSN, extension not built, or invalid setup.

    Raised when required configuration is absent (e.g. ``FERRUM_DATABASE_URL``
    not set) or when the native Rust extension has not been compiled yet.
    """

    code = "FERR-C001"
    category = "config"


class FerrumCompileError(FerrumError):
    """IR compilation failed: unknown field, unsupported operator, or IR version mismatch.

    Attributes:
        model: The model class name (never user input).
        field: The field name that triggered the error (metadata-sourced).
        operator: The operator that was rejected, if applicable.
        category: Machine-readable error category for structured logging.
        sqlstate: Always None for compile errors (not a database error).
    """

    code = "FERR-C102"

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        field: str | None = None,
        operator: str | None = None,
        category: str = "compile_error",
        sqlstate: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(
            message,
            sqlstate=sqlstate,
            category=category,
            model=model,
            operation=operation,
        )
        self.field = field
        self.operator = operator


class FerrumDeferredFieldError(FerrumError):
    """Accessing a field that was excluded via ``only()`` / ``defer()``."""

    code = "FERR-Q406"
    category = "deferred_field"


class FerrumRelationNotLoadedError(FerrumError):
    """Accessing a relationship that was not loaded via select/prefetch."""

    code = "FERR-Q407"
    category = "relation_not_loaded"


class FerrumNotFoundError(FerrumError):
    """A ``get()`` or ``get_or_raise()`` call found no matching row."""

    code = "FERR-Q404"
    category = "not_found"


class FerrumMultipleObjectsError(FerrumError):
    """A ``get()`` call matched more than one row."""

    code = "FERR-Q405"
    category = "multiple_objects"


class FerrumIntegrityError(FerrumError):
    """A database constraint violation (unique, FK, not-null, check).

    Attributes:
        constraint: The constraint name from the DB (safe to surface).
        category: Machine-readable error category for structured logging.
        sqlstate: PostgreSQL SQLSTATE code (e.g. "23505" for unique).
    """

    code = "FERR-D201"

    def __init__(
        self,
        message: str,
        *,
        constraint: str | None = None,
        category: str = "integrity_error",
        sqlstate: str | None = None,
        model: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(
            message,
            sqlstate=sqlstate,
            category=category,
            constraint=constraint,
            model=model,
            operation=operation,
        )


class FerrumConnectionError(FerrumError):
    """Connection or pool error.

    Diagnostics are limited to host/port/database/username/error category.
    Passwords and full DSNs are never included (CRED-1).
    """

    code = "FERR-E101"


class FerrumTimeoutError(FerrumError):
    """A query or connection operation timed out."""

    code = "FERR-E102"


class FerrumInternalError(FerrumError):
    """A Rust panic crossed the PyO3 boundary.

    The message contains only a sanitized category — no memory addresses,
    no local paths, no stack trace blobs (ERR-2).
    """

    code = "FERR-E500"
    category = "internal"


class FerrumHydrationError(FerrumError):
    """Row hydration failed: a non-nullable column was NULL or a required column was missing.

    Raised after ``_native.hydrate_rows()`` validates DB rows before ``model_construct``.
    The message contains only model/column names — no row values (ERR-1).
    """

    code = "FERR-H001"
    category = "hydration"


class FerrumMigrationError(FerrumError):
    """A migration operation failed or was rejected by a safety gate."""

    code = "FERR-M001"
    category = "migration"


def describe_migration_op(op: dict[str, Any]) -> str:
    """Return a short human-readable label for a migration operation dict."""
    kind = op.get("kind", "unknown")
    table = op.get("table")
    if kind == "create_table":
        return f"create_table on {table!r}"
    if kind == "drop_table":
        return f"drop_table {table!r}"
    if kind == "add_column":
        return f"add_column {op.get('name')!r} on {table!r}"
    if kind == "drop_column":
        return f"drop_column {op.get('column')!r} on {table!r}"
    if kind == "rename_column":
        return f"rename_column {op.get('from')!r} -> {op.get('to')!r} on {table!r}"
    if kind == "add_index":
        return f"add_index {op.get('name')!r} on {table!r}"
    if kind == "drop_index":
        return f"drop_index {op.get('name')!r}"
    if kind == "add_fk":
        return (
            f"add_fk {op.get('name')!r} "
            f"({op.get('column')!r} -> {op.get('ref_table')!r}.{op.get('ref_column')!r})"
        )
    if kind == "drop_fk":
        return f"drop_fk {op.get('name')!r} on {table!r}"
    if kind == "raw_sql":
        return "raw_sql"
    return kind


def _extract_sqlstate(exc: Exception) -> str | None:
    """Extract the PostgreSQL SQLSTATE code from a driver exception, if available.

    Returns ``None`` for non-PostgreSQL exceptions (TimeoutError, ConnectionError, etc.).
    """
    if _HAS_ASYNCPG and _asyncpg_exc is not None:
        _pg_base = getattr(_asyncpg_exc, "PostgresError", None)
        if _pg_base is not None and isinstance(exc, _pg_base):
            return getattr(exc, "sqlstate", None)
    return None


def _sqlstate_to_category(sqlstate: str | None) -> str:
    """Map a PostgreSQL SQLSTATE code to a Ferrum error category.

    Used when only the SQLSTATE is available (e.g. migration DDL failures where
    the specific asyncpg exception type is not checked). For query-path mapping,
    ``map_db_error`` sets the category directly from the exception type.
    """
    if sqlstate is None:
        return "unknown"
    # Class 23 — integrity constraint violations
    if sqlstate.startswith("23"):
        if sqlstate == "23505":
            return "unique_violation"
        return "integrity_error"
    # Class 42 — schema errors
    if sqlstate.startswith("42"):
        if sqlstate == "42883":
            return "undefined_function"
        if sqlstate == "42703":
            return "undefined_column"
        if sqlstate == "42P01":
            return "undefined_table"
        return "schema"
    # Class 40 — transaction rollback
    if sqlstate == "40001":
        return "serialization"
    if sqlstate == "40P01":
        return "deadlock"
    # Class 55 — object not in prerequisite state (lock not available)
    if sqlstate == "55P03":
        return "lock_timeout"
    # Class 57 — operator intervention
    if sqlstate == "57014":
        return "query_cancellation"
    if sqlstate in ("57P01", "57P02", "57P03"):
        return "failover"
    # Class 25 — invalid transaction state
    if sqlstate.startswith("25"):
        return "invalid_transaction_state"
    # Class 08 — connection errors
    if sqlstate.startswith("08"):
        return "connection"
    # Class 53 — insufficient resources
    if sqlstate == "53300":
        return "pool_exhaustion"
    return "unknown"


def _postgres_ddl_error_detail(exc: Exception) -> str:
    """Sanitized driver detail for migration DDL failures.

    Includes the exception class, SQLSTATE, and the top-level PostgreSQL message.
    ``DETAIL``/``HINT`` attributes are never included (ERR-1). Migration DDL
    failures are schema-level and safe to surface for developer actionability.
    """
    if _HAS_ASYNCPG and _asyncpg_exc is not None:
        _pg_base = getattr(_asyncpg_exc, "PostgresError", None)
        if _pg_base is not None and isinstance(exc, _pg_base):
            label = type(exc).__name__
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate:
                label = f"{label} (SQLSTATE {sqlstate})"
            msg = str(exc).strip()
            if msg and msg != type(exc).__name__:
                return f"{label}: {msg}"
            return label
    return type(exc).__name__


def migration_op_failure(
    *,
    action: str,
    migration_name: str,
    op_index: int,
    op: dict[str, Any],
    exc: Exception,
    code: str = "FERR-M001",
) -> FerrumMigrationError:
    """Build an actionable ``FerrumMigrationError`` for a failed migration operation."""
    verb = "apply" if action == "apply" else "revert"
    op_label = describe_migration_op(op)
    detail = _postgres_ddl_error_detail(exc)
    sqlstate = _extract_sqlstate(exc)
    category = _sqlstate_to_category(sqlstate) if sqlstate else "migration"
    return FerrumMigrationError(
        f"Failed to {verb} migration {migration_name!r} "
        f"at operation {op_index + 1} ({op_label}): {detail} [{code}]",
        sqlstate=sqlstate,
        category=category,
    )


class FerrumDangerApiError(FerrumError):
    """An unscoped destructive operation was attempted without the danger API."""

    code = "FERR-U301"
    category = "danger_api"


class FerrumSchemaError(FerrumError):
    """A referenced table or column does not exist in the database schema.

    Raised when PostgreSQL reports an undefined column (SQLSTATE 42703) or
    undefined table (SQLSTATE 42P01). Safe to surface: only the error class is
    included — no row data or DETAIL text (ERR-1).
    """

    code = "FERR-S001"


class FerrumDatabaseError(FerrumError):
    """A general database error with no more specific Ferrum mapping.

    Wraps any ``asyncpg.PostgresError`` not covered by a more specific
    subclass. Raw PostgreSQL ``DETAIL``/``HINT`` is never included in the
    message (ERR-1).
    """

    code = "FERR-D001"


def _extract_context(context: dict | None) -> dict[str, Any]:
    """Extract sanctioned safe fields (model, operation) from the context dict.

    The context dict MUST NOT contain bound parameter values or row data.
    Only ``model`` and ``operation`` are threaded onto the mapped exception.
    """
    if context is None:
        return {}
    return {
        "model": context.get("model"),
        "operation": context.get("operation"),
    }


def map_db_error(exc: Exception, *, context: dict | None = None) -> FerrumError:
    """Map a driver or internal exception to the Ferrum error taxonomy (ERR-1, ADR-006).

    Raw PostgreSQL ``DETAIL``/``HINT`` is never included in the returned error
    message. Constraint names are safe to surface; bound values and row data
    are not.

    Every mapped exception carries the ratified §5a sanctioned safe-error-field
    set: ``sqlstate`` (structured attribute), ``category`` (closed enum string),
    ``constraint`` (DB-reported name only), ``model``/``operation`` (Ferrum-side
    metadata threaded from the ``context`` dict).

    Args:
        exc: The original exception from asyncpg, PyO3, or another source.
        context: Structured context dict (e.g. ``{"model": "User", "operation":
            "select"}``). Must NOT contain bound parameter values or row data.

    Returns:
        A ``FerrumError`` subclass appropriate to the exception. If ``exc`` is
        already a ``FerrumError`` it is returned unchanged.
    """
    if isinstance(exc, FerrumError):
        return exc

    ctx = _extract_context(context)
    model = ctx.get("model")
    operation = ctx.get("operation")

    # asyncio.TimeoutError covers both pool-acquire timeouts and statement timeouts.
    # In Python 3.11+ TimeoutError is asyncio.TimeoutError, so both are caught here.
    if isinstance(exc, asyncio.TimeoutError):
        return FerrumTimeoutError(
            "Query or connection timed out. [FERR-E102]",
            category="timeout",
            model=model,
            operation=operation,
        )

    if _HAS_ASYNCPG and _asyncpg_exc is not None:
        sqlstate = getattr(exc, "sqlstate", None)

        if isinstance(exc, _asyncpg_exc.UniqueViolationError):
            constraint = getattr(exc, "constraint_name", None)
            return FerrumIntegrityError(
                f"Unique constraint violation"
                f"{f' ({constraint})' if constraint else ''}. [FERR-D201]",
                constraint=constraint,
                category="unique_violation",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # Broad integrity check: FK, NotNull, Check, Exclusion, etc.
        # UniqueViolationError is caught above with its specific category.
        _integrity_base = getattr(_asyncpg_exc, "IntegrityConstraintViolationError", None)
        if _integrity_base is not None and isinstance(exc, _integrity_base):
            constraint = getattr(exc, "constraint_name", None)
            return FerrumIntegrityError(
                f"Integrity constraint violation ({type(exc).__name__})"
                f"{f' ({constraint})' if constraint else ''}. [FERR-D201]",
                constraint=constraint,
                category="integrity_error",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # UndefinedColumnError / UndefinedTableError — guard with getattr for
        # asyncpg version portability.
        _undef_col = getattr(_asyncpg_exc, "UndefinedColumnError", None)
        _undef_tbl = getattr(_asyncpg_exc, "UndefinedTableError", None)
        _undef_types = tuple(t for t in (_undef_col, _undef_tbl) if t is not None)
        if _undef_types and isinstance(exc, _undef_types):
            return FerrumSchemaError(
                f"Schema object not found ({type(exc).__name__}). [FERR-S001]",
                category="schema",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # UndefinedFunctionError (SQLSTATE 42883) — e.g. operator does not exist.
        _undef_fn = getattr(_asyncpg_exc, "UndefinedFunctionError", None)
        if _undef_fn is not None and isinstance(exc, _undef_fn):
            return FerrumSchemaError(
                f"Schema object not found ({type(exc).__name__}). [FERR-S001]",
                category="undefined_function",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # DeadlockDetectedError (SQLSTATE 40P01).
        _deadlock = getattr(_asyncpg_exc, "DeadlockDetectedError", None)
        if _deadlock is not None and isinstance(exc, _deadlock):
            return FerrumDatabaseError(
                f"Deadlock detected ({type(exc).__name__}). [FERR-D001]",
                category="deadlock",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # SerializationError (SQLSTATE 40001).
        _serialization = getattr(_asyncpg_exc, "SerializationError", None)
        if _serialization is not None and isinstance(exc, _serialization):
            return FerrumDatabaseError(
                f"Serialization failure ({type(exc).__name__}). [FERR-D001]",
                category="serialization",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # LockNotAvailableError (SQLSTATE 55P03) — lock timeout.
        _lock_not_avail = getattr(_asyncpg_exc, "LockNotAvailableError", None)
        if _lock_not_avail is not None and isinstance(exc, _lock_not_avail):
            return FerrumTimeoutError(
                f"Lock timeout ({type(exc).__name__}). [FERR-E102]",
                category="lock_timeout",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # QueryCanceledError (SQLSTATE 57014) — statement timeout or explicit cancellation.
        _query_canceled = getattr(_asyncpg_exc, "QueryCanceledError", None)
        if _query_canceled is not None and isinstance(exc, _query_canceled):
            return FerrumTimeoutError(
                "Query was cancelled. [FERR-E102]",
                category="query_cancellation",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # AdminShutdownError (57P01) / CrashShutdownError (57P02) / CannotConnectNowError (57P03)
        # — failover / admin shutdown.
        _admin_shutdown = getattr(_asyncpg_exc, "AdminShutdownError", None)
        _crash_shutdown = getattr(_asyncpg_exc, "CrashShutdownError", None)
        _cannot_connect = getattr(_asyncpg_exc, "CannotConnectNowError", None)
        _failover_types = tuple(
            t for t in (_admin_shutdown, _crash_shutdown, _cannot_connect) if t is not None
        )
        if _failover_types and isinstance(exc, _failover_types):
            return FerrumConnectionError(
                f"Server shutdown/failover ({type(exc).__name__}). [FERR-E101]",
                category="failover",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # InvalidTransactionStateError (SQLSTATE class 25) and related subclasses.
        _invalid_tx_state = getattr(_asyncpg_exc, "InvalidTransactionStateError", None)
        if _invalid_tx_state is not None and isinstance(exc, _invalid_tx_state):
            return FerrumDatabaseError(
                f"Invalid transaction state ({type(exc).__name__}). [FERR-D001]",
                category="invalid_transaction_state",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        # TooManyConnectionsError (SQLSTATE 53300) — pool exhaustion at server level.
        _too_many_conn = getattr(_asyncpg_exc, "TooManyConnectionsError", None)
        if _too_many_conn is not None and isinstance(exc, _too_many_conn):
            return FerrumConnectionError(
                f"Too many connections ({type(exc).__name__}). [FERR-E101]",
                category="pool_exhaustion",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        _pg_conn = getattr(_asyncpg_exc, "PostgresConnectionError", None)
        if _pg_conn is not None and isinstance(exc, _pg_conn):
            return FerrumConnectionError(
                f"PostgreSQL connection error: {type(exc).__name__}. [FERR-E101]",
                category="connection",
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )
        _pg_base = getattr(_asyncpg_exc, "PostgresError", None)
        if _pg_base is not None and isinstance(exc, _pg_base):
            # Sanitized: only the exception class name; never DETAIL/HINT (ERR-1).
            return FerrumDatabaseError(
                f"Database error: {type(exc).__name__}. [FERR-D001]",
                category=_sqlstate_to_category(sqlstate),
                sqlstate=sqlstate,
                model=model,
                operation=operation,
            )

    if _HAS_ASYNCMY and _asyncmy_exc is not None:
        integrity_cls = getattr(_asyncmy_exc, "IntegrityError", None)
        if integrity_cls is not None and isinstance(exc, integrity_cls):
            return FerrumIntegrityError(
                f"Integrity constraint violation ({type(exc).__name__}). [FERR-D201]",
                category="integrity_error",
                model=model,
                operation=operation,
            )
        op_err = getattr(_asyncmy_exc, "OperationalError", None)
        if op_err is not None and isinstance(exc, op_err):
            return FerrumConnectionError(
                f"MySQL connection error: {type(exc).__name__}. [FERR-E101]",
                category="connection",
                model=model,
                operation=operation,
            )
        prog_err = getattr(_asyncmy_exc, "ProgrammingError", None)
        if prog_err is not None and isinstance(exc, prog_err):
            return FerrumSchemaError(
                f"Schema object not found ({type(exc).__name__}). [FERR-S001]",
                category="schema",
                model=model,
                operation=operation,
            )
        data_err = getattr(_asyncmy_exc, "DataError", None)
        if data_err is not None and isinstance(exc, data_err):
            return FerrumDatabaseError(
                f"Database error: {type(exc).__name__}. [FERR-D001]",
                model=model,
                operation=operation,
            )

    if _HAS_AIOSQLITE and aiosqlite is not None:
        if isinstance(exc, aiosqlite.IntegrityError):
            return FerrumIntegrityError(
                f"Integrity constraint violation ({type(exc).__name__}). [FERR-D201]",
                category="integrity_error",
                model=model,
                operation=operation,
            )
        if isinstance(exc, aiosqlite.OperationalError):
            msg = str(exc).lower()
            if "no such table" in msg or "no such column" in msg:
                return FerrumSchemaError(
                    f"Schema object not found ({type(exc).__name__}). [FERR-S001]",
                    category="schema",
                    model=model,
                    operation=operation,
                )
            return FerrumConnectionError(
                f"SQLite connection error: {type(exc).__name__}. [FERR-E101]",
                category="connection",
                model=model,
                operation=operation,
            )
        if isinstance(exc, aiosqlite.DatabaseError):
            return FerrumDatabaseError(
                f"Database error: {type(exc).__name__}. [FERR-D001]",
                model=model,
                operation=operation,
            )

    if _HAS_AIOODBC and _pyodbc is not None:
        # pyodbc hierarchy: Error → DatabaseError → {IntegrityError,
        # OperationalError, ProgrammingError, DataError, …}. Order matters:
        # check leaf classes before DatabaseError. Only the exception class name
        # is surfaced — never the SQL Server message text or row data (ERR-1).
        integrity_cls = getattr(_pyodbc, "IntegrityError", None)
        if integrity_cls is not None and isinstance(exc, integrity_cls):
            return FerrumIntegrityError(
                f"Integrity constraint violation ({type(exc).__name__}). [FERR-D201]",
                category="integrity_error",
                model=model,
                operation=operation,
            )
        prog_err = getattr(_pyodbc, "ProgrammingError", None)
        if prog_err is not None and isinstance(exc, prog_err):
            return FerrumSchemaError(
                f"Schema object not found ({type(exc).__name__}). [FERR-S001]",
                category="schema",
                model=model,
                operation=operation,
            )
        op_err = getattr(_pyodbc, "OperationalError", None)
        if op_err is not None and isinstance(exc, op_err):
            return FerrumConnectionError(
                f"SQL Server connection error: {type(exc).__name__}. [FERR-E101]",
                category="connection",
                model=model,
                operation=operation,
            )
        db_err = getattr(_pyodbc, "DatabaseError", None)
        if db_err is not None and isinstance(exc, db_err):
            return FerrumDatabaseError(
                f"Database error: {type(exc).__name__}. [FERR-D001]",
                model=model,
                operation=operation,
            )

    return FerrumInternalError(
        f"Unexpected error in database operation: {type(exc).__name__}. [FERR-E500]",
        category="internal",
        model=model,
        operation=operation,
    )


def map_native_error(exc: Exception, *, _native_mod: object = None) -> FerrumError:
    """Map a ``ferrum._native`` PyO3 exception to the Ferrum error taxonomy (ADR-006).

    This is the single non-bypassable seam for all native extension exceptions.
    Callers should pass the already-imported ``_native_mod`` (``ferrum._native``)
    to avoid re-importing the extension on each call.

    Routing:
    - ``_native.FerrumCompileError``   → ``FerrumCompileError``   (FERR-C102)
    - ``_native.FerrumHydrationError`` → ``FerrumHydrationError`` (FERR-H001)
    - ``_native.FerrumInternalError``  → ``FerrumInternalError``  (FERR-E500)
    - ``RuntimeError`` (bare)          → ``FerrumInternalError``  (FERR-E500)
    - Anything else                    → delegated to ``map_db_error()``

    Mapped exceptions carry ``category`` from the closed ``ERROR_CATEGORIES``
    enum. ``sqlstate`` is always ``None`` for native (non-database) exceptions.

    Note on message safety: Rust exception messages must contain only model/column
    names — never row values or bound parameters. This is enforced at the Rust layer;
    ``map_native_error`` passes the native message through without additional
    sanitization (the redaction guarantee is structural, not string-based).

    Args:
        exc: The exception raised by the ``ferrum._native`` extension.
        _native_mod: The imported ``ferrum._native`` module, or ``None`` if the
            extension is not available (fallback to ``FerrumInternalError``).

    Returns:
        A ``FerrumError`` subclass appropriate to the exception.
    """
    if isinstance(exc, FerrumError):
        return exc

    if _native_mod is not None:
        _native_compile_err = getattr(_native_mod, "FerrumCompileError", None)
        _native_hydration_err = getattr(_native_mod, "FerrumHydrationError", None)
        _native_internal_err = getattr(_native_mod, "FerrumInternalError", None)

        if _native_compile_err is not None and isinstance(exc, _native_compile_err):
            return FerrumCompileError(str(exc), category="compile_error")
        if _native_hydration_err is not None and isinstance(exc, _native_hydration_err):
            # Message from Rust contains model/column names only — safe to surface.
            return FerrumHydrationError(
                f"Row hydration failed: {exc}. [FERR-H001]",
                category="hydration",
            )
        if _native_internal_err is not None and isinstance(exc, _native_internal_err):
            return FerrumInternalError(
                "Internal Ferrum error (native). [FERR-E500]",
                category="internal",
            )

    if isinstance(exc, RuntimeError):
        return FerrumInternalError(
            f"Internal Ferrum error (native): {type(exc).__name__}. [FERR-E500]",
            category="internal",
        )

    return map_db_error(exc)
