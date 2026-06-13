"""Ferrum exception taxonomy and centralized error boundary (ADR-006).

All exceptions raised to application code are subclasses of ``FerrumError``.
Internal exceptions from asyncpg, PyO3, and PostgreSQL are mapped here via
``map_db_error()`` — raw ``DETAIL``/``HINT`` containing row data is never
propagated by default (ERR-1).

PyO3 panics from the Rust core surface as ``FerrumInternalError`` (ERR-2).
No exception message ever contains bound parameter values, DSNs, or passwords.

Stable error codes (``FERR-XXXX``) are class-level attributes and appear in
rendered messages for documentation links and tooling (DX blocker B-6).
"""

from __future__ import annotations

try:
    import asyncpg.exceptions as _asyncpg_exc  # type: ignore[import-untyped]

    _HAS_ASYNCPG: bool = True
except ImportError:
    _asyncpg_exc = None  # type: ignore
    _HAS_ASYNCPG = False


class FerrumError(Exception):
    """Base class for all Ferrum exceptions."""

    code: str = "FERR-0000"


class FerrumConfigError(FerrumError):
    """Misconfiguration error: missing DSN, extension not built, or invalid setup.

    Raised when required configuration is absent (e.g. ``FERRUM_DATABASE_URL``
    not set) or when the native Rust extension has not been compiled yet.
    """

    code = "FERR-C001"


class FerrumCompileError(FerrumError):
    """IR compilation failed: unknown field, unsupported operator, or IR version mismatch.

    Attributes:
        model: The model class name (never user input).
        field: The field name that triggered the error (metadata-sourced).
        operator: The operator that was rejected, if applicable.
        category: Machine-readable error category for structured logging.
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
    ) -> None:
        super().__init__(message)
        self.model = model
        self.field = field
        self.operator = operator
        self.category = category


class FerrumNotFoundError(FerrumError):
    """A ``get()`` or ``get_or_raise()`` call found no matching row."""

    code = "FERR-Q404"


class FerrumMultipleObjectsError(FerrumError):
    """A ``get()`` call matched more than one row."""

    code = "FERR-Q405"


class FerrumIntegrityError(FerrumError):
    """A database constraint violation (unique, FK, not-null, check).

    Attributes:
        constraint: The constraint name from the DB (safe to surface).
        category: Machine-readable error category for structured logging.
    """

    code = "FERR-D201"

    def __init__(
        self,
        message: str,
        *,
        constraint: str | None = None,
        category: str = "integrity_error",
    ) -> None:
        super().__init__(message)
        self.constraint = constraint
        self.category = category


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


class FerrumMigrationError(FerrumError):
    """A migration operation failed or was rejected by a safety gate."""

    code = "FERR-M001"


class FerrumDangerApiError(FerrumError):
    """An unscoped destructive operation was attempted without the danger API."""

    code = "FERR-U301"


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


def map_db_error(exc: Exception, *, context: dict | None = None) -> FerrumError:
    """Map a driver or internal exception to the Ferrum error taxonomy (ERR-1, ADR-006).

    Raw PostgreSQL ``DETAIL``/``HINT`` is never included in the returned error
    message. Constraint names are safe to surface; bound values and row data
    are not.

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

    if _HAS_ASYNCPG and _asyncpg_exc is not None:
        if isinstance(exc, _asyncpg_exc.UniqueViolationError):
            constraint = getattr(exc, "constraint_name", None)
            return FerrumIntegrityError(
                f"Unique constraint violation"
                f"{f' ({constraint})' if constraint else ''}. [FERR-D201]",
                constraint=constraint,
                category="unique_violation",
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
            )
        # UndefinedColumnError / UndefinedTableError — guard with getattr for
        # asyncpg version portability.
        _undef_col = getattr(_asyncpg_exc, "UndefinedColumnError", None)
        _undef_tbl = getattr(_asyncpg_exc, "UndefinedTableError", None)
        _undef_types = tuple(t for t in (_undef_col, _undef_tbl) if t is not None)
        if _undef_types and isinstance(exc, _undef_types):
            return FerrumSchemaError(f"Schema object not found ({type(exc).__name__}). [FERR-S001]")
        _pg_conn = getattr(_asyncpg_exc, "PostgresConnectionError", None)
        if _pg_conn is not None and isinstance(exc, _pg_conn):
            return FerrumConnectionError(
                f"PostgreSQL connection error: {type(exc).__name__}. [FERR-E101]"
            )
        _pg_base = getattr(_asyncpg_exc, "PostgresError", None)
        if _pg_base is not None and isinstance(exc, _pg_base):
            # Sanitized: only the exception class name; never DETAIL/HINT (ERR-1).
            return FerrumDatabaseError(f"Database error: {type(exc).__name__}. [FERR-D001]")

    return FerrumInternalError(
        f"Unexpected error in database operation: {type(exc).__name__}. [FERR-E500]"
    )
