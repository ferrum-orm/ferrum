"""Ferrum exception taxonomy and centralized error boundary.

All exceptions raised to application code are subclasses of ``FerrumError``.
Internal exceptions from asyncpg, PyO3, and PostgreSQL are mapped here — raw
``DETAIL``/``HINT`` containing row data is never propagated by default (ERR-1).

PyO3 panics from the Rust core surface as ``FerrumInternalError`` (ERR-2).
No exception message ever contains bound parameter values, DSNs, or passwords.
"""

from __future__ import annotations


class FerrumError(Exception):
    """Base class for all Ferrum exceptions."""


class FerrumCompileError(FerrumError):
    """IR compilation failed: unknown field, unsupported operator, or IR version mismatch.

    Attributes:
        model: The model class name (never user input).
        field: The field name that triggered the error (metadata-sourced).
        operator: The operator that was rejected, if applicable.
        category: Machine-readable error category for structured logging.
    """

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


class FerrumMultipleObjectsError(FerrumError):
    """A ``get()`` call matched more than one row."""


class FerrumIntegrityError(FerrumError):
    """A database constraint violation (unique, FK, not-null, check).

    Attributes:
        constraint: The constraint name from the DB (safe to surface).
        category: Machine-readable error category for structured logging.
    """

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


class FerrumTimeoutError(FerrumError):
    """A query or connection operation timed out."""


class FerrumInternalError(FerrumError):
    """A Rust panic crossed the PyO3 boundary.

    The message contains only a sanitized category — no memory addresses,
    no local paths, no stack trace blobs (ERR-2).
    """


class FerrumMigrationError(FerrumError):
    """A migration operation failed or was rejected by a safety gate."""


class FerrumDangerApiError(FerrumError):
    """An unscoped destructive operation was attempted without the danger API."""
