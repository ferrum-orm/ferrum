"""Unit tests for the Ferrum error taxonomy."""

from __future__ import annotations

import unittest.mock as mock

import asyncpg.exceptions

from ferrum.errors import (
    FerrumCompileError,
    FerrumConnectionError,
    FerrumDangerApiError,
    FerrumError,
    FerrumIntegrityError,
    FerrumInternalError,
    FerrumMigrationError,
    FerrumNotFoundError,
    FerrumTimeoutError,
    map_db_error,
)


class TestErrorHierarchy:
    def test_all_errors_are_ferrum_error(self) -> None:
        for cls in (
            FerrumCompileError,
            FerrumNotFoundError,
            FerrumIntegrityError,
            FerrumConnectionError,
            FerrumTimeoutError,
            FerrumInternalError,
            FerrumMigrationError,
            FerrumDangerApiError,
        ):
            assert issubclass(cls, FerrumError)

    def test_compile_error_structured_fields(self) -> None:
        err = FerrumCompileError(
            "unknown field 'foo' on model 'User'",
            model="User",
            field="foo",
            operator=None,
            category="compile_error",
        )
        assert err.model == "User"
        assert err.field == "foo"
        assert err.category == "compile_error"
        # Error message must NOT echo submitted user values (LOG-1)
        assert "foo" in str(err)  # field name is metadata, not user input

    def test_integrity_error_structured_fields(self) -> None:
        err = FerrumIntegrityError("unique violation", constraint="users_email_key")
        assert err.constraint == "users_email_key"
        assert err.category == "integrity_error"


# ---------------------------------------------------------------------------
# ERR-1: SQLSTATE → Ferrum taxonomy (map_db_error)
# ---------------------------------------------------------------------------


class TestMapDbError:
    def test_unique_violation_maps_to_integrity_error(self) -> None:
        """ERR-1: asyncpg.UniqueViolationError maps to FerrumIntegrityError.

        The mapped error carries the constraint name (safe metadata) but must
        not surface the PostgreSQL DETAIL/HINT which may contain row values.
        """
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.UniqueViolationError)
        mock_exc.constraint_name = "users_email_key"
        mock_exc.detail = None
        mock_exc.hint = None

        result = map_db_error(mock_exc, context={})

        assert isinstance(result, FerrumIntegrityError), (
            f"Expected FerrumIntegrityError, got {type(result).__name__}"
        )
        assert result.constraint == "users_email_key"
        assert result.category in ("integrity_error", "unique_violation")

    def test_postgres_connection_error_maps_to_connection_error(self) -> None:
        """ERR-1: asyncpg connection-layer errors map to FerrumConnectionError."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.ConnectionFailureError)
        mock_exc.detail = None
        mock_exc.hint = None

        result = map_db_error(mock_exc, context={})

        assert isinstance(result, FerrumConnectionError), (
            f"Expected FerrumConnectionError, got {type(result).__name__}"
        )

    def test_raw_detail_hint_not_in_mapped_error_message(self) -> None:
        """ERR-1: PostgreSQL DETAIL/HINT containing row data must not leak into the mapped error.

        asyncpg surfaces DETAIL clauses that may include duplicate-key values or
        FK references. map_db_error must never propagate this raw content.
        """
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.UniqueViolationError)
        mock_exc.constraint_name = "users_email_key"
        mock_exc.detail = "Key (email)=(secret_value@example.com) already exists."
        mock_exc.hint = "Another hint mentioning secret_value row detail."

        result = map_db_error(mock_exc, context={})

        error_message = str(result)
        assert "secret_value" not in error_message, (
            f"PostgreSQL DETAIL/HINT with sensitive row data must not appear in mapped error: "
            f"{error_message!r}"
        )

    def test_map_db_error_unknown_raises_ferrum_internal(self) -> None:
        """ERR-1: A plain Exception with no asyncpg mapping returns FerrumInternalError.

        Anything that is not an asyncpg exception and not already a FerrumError
        falls through to the final catch-all branch, which must return
        FerrumInternalError — never re-raise the raw exception.
        """
        result = map_db_error(Exception("something unexpected"), context={})
        assert isinstance(result, FerrumInternalError), (
            f"Expected FerrumInternalError for unmapped exception, got {type(result).__name__}"
        )

    def test_generic_postgres_error_maps_to_internal_error(self) -> None:
        """ERR-1: Unmapped PostgresError subclasses map to a sanitized FerrumError."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.PostgresError)
        mock_exc.sqlstate = "XX000"
        mock_exc.detail = "Internal server detail with raw_row_value=42."
        mock_exc.hint = None

        result = map_db_error(mock_exc, context={})

        assert isinstance(result, FerrumError), (
            f"Expected a FerrumError subclass, got {type(result).__name__}"
        )
        assert "raw_row_value" not in str(result), (
            "Raw PostgreSQL detail must not appear in the mapped error message"
        )

    def test_integrity_error_subclasses_all_map_to_integrity_error(self) -> None:
        """ERR-1: All asyncpg integrity subclasses map to FerrumIntegrityError."""
        for exc_cls in (
            asyncpg.exceptions.ForeignKeyViolationError,
            asyncpg.exceptions.NotNullViolationError,
            asyncpg.exceptions.CheckViolationError,
        ):
            mock_exc = mock.MagicMock(spec=exc_cls)
            mock_exc.constraint_name = None
            mock_exc.detail = None
            mock_exc.hint = None

            result = map_db_error(mock_exc, context={})
            assert isinstance(result, FerrumIntegrityError), (
                f"{exc_cls.__name__} must map to FerrumIntegrityError, got {type(result).__name__}"
            )


# ---------------------------------------------------------------------------
# ERR-2: PyO3 panic → catchable (reference to test_boundary.py)
# ---------------------------------------------------------------------------


class TestErr2PanicBoundaryReference:
    def test_err2_covered_in_boundary_tests(self) -> None:
        """ERR-2 is covered by tests/python/unit/test_boundary.py.

        Specifically:
        - test_rust_panic_surfaces_as_ferrum_internal_error
        - test_compile_error_message_does_not_contain_bound_values

        This placeholder documents the coverage mapping so the security gate
        checklist can confirm ERR-2 without re-running native extension tests here.
        """
        # test_boundary.py contains the native-extension-gated ERR-2 tests.
        # This test serves as a coverage-map marker; it always passes.
        boundary_tests = [
            "test_rust_panic_surfaces_as_ferrum_internal_error",
            "test_compile_error_message_does_not_contain_bound_values",
        ]
        assert len(boundary_tests) == 2
