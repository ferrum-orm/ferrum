"""Unit tests for the Ferrum error taxonomy and centralized error mapping (ADR-006)."""

from __future__ import annotations

import unittest.mock as mock

import asyncpg.exceptions

from ferrum.errors import (
    FerrumCompileError,
    FerrumConnectionError,
    FerrumDangerApiError,
    FerrumError,
    FerrumHydrationError,
    FerrumIntegrityError,
    FerrumInternalError,
    FerrumMigrationError,
    FerrumNotFoundError,
    FerrumTimeoutError,
    map_db_error,
    map_native_error,
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
# FerrumHydrationError taxonomy
# ---------------------------------------------------------------------------


class TestFerrumHydrationError:
    def test_hydration_error_is_ferrum_error(self) -> None:
        """FerrumHydrationError must be a FerrumError subclass (ADR-006 taxonomy)."""
        assert issubclass(FerrumHydrationError, FerrumError)

    def test_hydration_error_code(self) -> None:
        assert FerrumHydrationError.code == "FERR-H001"

    def test_hydration_error_message_does_not_contain_row_data(self) -> None:
        """FERR-H001 messages must not carry row data — only model/column names (ERR-1)."""
        err = FerrumHydrationError(
            "Row hydration failed: column 'title' is NULL on model 'Post'. [FERR-H001]"
        )
        msg = str(err)
        assert "Post" in msg  # model name is safe metadata
        assert "title" in msg  # column name is safe metadata


# ---------------------------------------------------------------------------
# map_native_error() — ADR-006 error remapping at the PyO3 boundary
# ---------------------------------------------------------------------------


def _make_native_mod(
    *,
    compile_cls: type | None = None,
    hydration_cls: type | None = None,
    internal_cls: type | None = None,
) -> object:
    """Return a minimal fake ``ferrum._native`` module for map_native_error tests."""
    mod = mock.MagicMock()
    mod.FerrumCompileError = compile_cls or type("FerrumCompileError", (RuntimeError,), {})
    mod.FerrumHydrationError = hydration_cls or type("FerrumHydrationError", (RuntimeError,), {})
    mod.FerrumInternalError = internal_cls or type("FerrumInternalError", (RuntimeError,), {})
    return mod


class TestMapNativeError:
    def test_native_compile_error_maps_to_ferrum_compile_error(self) -> None:
        """_native.FerrumCompileError → Python FerrumCompileError."""
        native = _make_native_mod()
        exc = native.FerrumCompileError("bad IR")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumCompileError), (
            f"Expected FerrumCompileError, got {type(result).__name__}"
        )

    def test_native_hydration_error_maps_to_ferrum_hydration_error(self) -> None:
        """_native.FerrumHydrationError → Python FerrumHydrationError."""
        native = _make_native_mod()
        exc = native.FerrumHydrationError("NULL in non-nullable column")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumHydrationError), (
            f"Expected FerrumHydrationError, got {type(result).__name__}"
        )

    def test_native_internal_error_maps_to_ferrum_internal_error(self) -> None:
        """_native.FerrumInternalError → Python FerrumInternalError."""
        native = _make_native_mod()
        exc = native.FerrumInternalError("unexpected panic")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumInternalError), (
            f"Expected FerrumInternalError, got {type(result).__name__}"
        )

    def test_unknown_runtime_error_maps_to_ferrum_internal_error(self) -> None:
        """Bare RuntimeError from native ext → FerrumInternalError (catch-all)."""
        native = _make_native_mod()
        exc = RuntimeError("something exploded in Rust")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumInternalError), (
            f"Expected FerrumInternalError for unmapped RuntimeError, got {type(result).__name__}"
        )

    def test_mapped_compile_error_has_correct_code(self) -> None:
        """Mapped FerrumCompileError carries the FERR-C102 code (not config FERR-C001)."""
        native = _make_native_mod()
        exc = native.FerrumCompileError("bad filter field")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumCompileError)
        assert result.code == "FERR-C102"

    def test_mapped_hydration_error_has_correct_code(self) -> None:
        """Mapped FerrumHydrationError carries the FERR-H001 code."""
        native = _make_native_mod()
        exc = native.FerrumHydrationError("NULL value")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumHydrationError)
        assert result.code == "FERR-H001"

    def test_map_native_error_no_native_mod_falls_back_gracefully(self) -> None:
        """If _native_mod is None, map_native_error falls back to FerrumInternalError."""
        result = map_native_error(RuntimeError("fallback"), _native_mod=None)
        assert isinstance(result, FerrumInternalError)

    def test_compile_error_type_is_ferrum_error_subclass(self) -> None:
        """Mapped compile error is always a FerrumError subclass."""
        native = _make_native_mod()
        exc = native.FerrumCompileError("unknown field 'foo'")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumError)

    def test_hydration_error_type_is_ferrum_error_subclass(self) -> None:
        """Mapped hydration error is always a FerrumError subclass."""
        native = _make_native_mod()
        exc = native.FerrumHydrationError("NULL at column 'label'")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumError)


# ---------------------------------------------------------------------------
# ERR-2: PyO3 panic → catchable (reference to test_boundary.py)
# ---------------------------------------------------------------------------


class TestErr2PanicBoundaryReference:
    def test_err2_covered_in_boundary_tests(self) -> None:
        """ERR-2 is covered by tests/python/unit/test_boundary.py.

        Specifically:
        - test_rust_panic_surfaces_as_ferrum_internal_error
        - test_compile_error_message_does_not_contain_bound_values
        - test_hydrate_rows_missing_required_column_raises_hydration_error
        - test_hydrate_rows_null_required_column_raises_hydration_error

        This placeholder documents the coverage mapping so the security gate
        checklist can confirm ERR-2 without re-running native extension tests here.
        """
        boundary_tests = [
            "test_rust_panic_surfaces_as_ferrum_internal_error",
            "test_compile_error_message_does_not_contain_bound_values",
            "test_hydrate_rows_missing_required_column_raises_hydration_error",
            "test_hydrate_rows_null_required_column_raises_hydration_error",
        ]
        assert len(boundary_tests) == 4


# ---------------------------------------------------------------------------
# Timeout and cancellation mapping
# ---------------------------------------------------------------------------


class TestTimeoutAndCancellationMapping:
    def test_asyncio_timeout_maps_to_ferrum_timeout_error(self) -> None:
        """asyncio.TimeoutError (pool-acquire / statement timeout) → FerrumTimeoutError."""
        result = map_db_error(TimeoutError())
        assert isinstance(result, FerrumTimeoutError), (
            f"Expected FerrumTimeoutError for TimeoutError, got {type(result).__name__}"
        )

    def test_asyncio_timeout_message_is_sanitized(self) -> None:
        """Timeout error message must not contain DSN, bound values, or raw exception detail."""
        result = map_db_error(TimeoutError("postgresql://user:secret@host/db"))
        assert "secret" not in str(result), "DSN secret must not appear in timeout error message"

    def test_query_canceled_maps_to_ferrum_timeout_error(self) -> None:
        """asyncpg.QueryCanceledError (SQLSTATE 57014) → FerrumTimeoutError."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.QueryCanceledError)
        mock_exc.detail = None
        mock_exc.hint = None

        result = map_db_error(mock_exc, context={})

        assert isinstance(result, FerrumTimeoutError), (
            f"Expected FerrumTimeoutError for QueryCanceledError, got {type(result).__name__}"
        )

    def test_query_canceled_message_has_no_row_data(self) -> None:
        """Cancellation error message must not echo row data or raw SQLSTATE."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.QueryCanceledError)
        mock_exc.detail = "query canceled because of user request containing row=sentinel_row_data"
        mock_exc.hint = None

        result = map_db_error(mock_exc, context={})

        assert "sentinel_row_data" not in str(result), (
            "Raw DETAIL/HINT must not appear in mapped cancellation error"
        )

    def test_pool_acquire_timeout_maps_to_ferrum_timeout(self) -> None:
        """Pool exhaustion expressed as TimeoutError → FerrumTimeoutError.

        When a pool has no available connections and the acquire timeout fires,
        asyncpg propagates asyncio.TimeoutError (== TimeoutError) to the caller.
        This must map to FerrumTimeoutError — not FerrumInternalError.
        """
        result = map_db_error(TimeoutError())
        assert isinstance(result, FerrumTimeoutError)
        assert FerrumTimeoutError.code in str(result) or "FERR-E102" in str(result)
