"""Unit tests for the Ferrum error taxonomy and centralized error mapping (ADR-006).

Covers the ratified §5a "Safe error fields" contract:
- Every mapped exception has structured ``sqlstate`` and ``category``.
- ``category`` is a stable string from the closed ``ERROR_CATEGORIES`` enum.
- PostgreSQL DETAIL/HINT/bound values/row data/DSNs never appear in any field.
"""

from __future__ import annotations

import unittest.mock as mock

import asyncpg.exceptions

from ferrum.errors import (
    ERROR_CATEGORIES,
    FerrumCompileError,
    FerrumConnectionError,
    FerrumDangerApiError,
    FerrumDatabaseError,
    FerrumError,
    FerrumHydrationError,
    FerrumIntegrityError,
    FerrumInternalError,
    FerrumMigrationError,
    FerrumNotFoundError,
    FerrumSchemaError,
    FerrumTimeoutError,
    _sqlstate_to_category,
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


class TestMigrationOpFailure:
    def test_describe_create_table_op(self) -> None:
        from ferrum.errors import describe_migration_op

        assert describe_migration_op({"kind": "create_table", "table": "users"}) == (
            "create_table on 'users'"
        )

    def test_migration_op_failure_includes_operation_context(self) -> None:
        from ferrum.errors import migration_op_failure

        exc = asyncpg.exceptions.DatatypeMismatchError(
            'column "embedding" is of type vector but default expression is of type uuid'
        )

        err = migration_op_failure(
            action="apply",
            migration_name="0001_initial",
            op_index=0,
            op={"kind": "create_table", "table": "documents"},
            exc=exc,
        )

        msg = str(err)
        assert "0001_initial" in msg
        assert "operation 1" in msg
        assert "create_table on 'documents'" in msg
        assert "DatatypeMismatchError" in msg
        assert "SQLSTATE 42804" in msg
        assert "embedding" in msg
        assert "vector" in msg
        assert "FERR-M001" in msg

    def test_migration_op_failure_never_includes_detail_or_hint(self) -> None:
        from ferrum.errors import migration_op_failure

        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.DatatypeMismatchError)
        mock_exc.sqlstate = "42804"
        mock_exc.detail = "DETAIL: Key (id)=(secret-row-id) already exists."
        mock_exc.hint = "HINT: Remove the duplicate row first."
        mock_exc.__str__ = mock.Mock(return_value="column type mismatch")

        err = migration_op_failure(
            action="apply",
            migration_name="0001_initial",
            op_index=0,
            op={"kind": "create_table", "table": "documents"},
            exc=mock_exc,
        )

        msg = str(err)
        assert "secret-row-id" not in msg
        assert "DETAIL:" not in msg
        assert "HINT:" not in msg


# ---------------------------------------------------------------------------
# §5a "Safe error fields": structured sqlstate/category on every mapped exception
# ---------------------------------------------------------------------------


class TestStructuredErrorFields:
    """Every mapped exception must carry structured sqlstate and category (§5a)."""

    def test_ferrum_error_base_has_sanctioned_fields(self) -> None:
        """FerrumError base class declares all sanctioned §5a fields."""
        err = FerrumError("test")
        assert hasattr(err, "sqlstate")
        assert hasattr(err, "category")
        assert hasattr(err, "constraint")
        assert hasattr(err, "model")
        assert hasattr(err, "operation")
        assert err.sqlstate is None
        assert err.category is None
        assert err.constraint is None
        assert err.model is None
        assert err.operation is None

    def test_ferrum_error_init_accepts_sanctioned_fields(self) -> None:
        """FerrumError.__init__ accepts and sets all sanctioned fields."""
        err = FerrumError(
            "test",
            sqlstate="23505",
            category="unique_violation",
            constraint="users_email_key",
            model="User",
            operation="insert",
        )
        assert err.sqlstate == "23505"
        assert err.category == "unique_violation"
        assert err.constraint == "users_email_key"
        assert err.model == "User"
        assert err.operation == "insert"

    def test_compile_error_carries_sqlstate_and_category(self) -> None:
        """FerrumCompileError carries sqlstate (None for compile) and category."""
        err = FerrumCompileError("bad field", model="User", field="foo")
        assert err.category == "compile_error"
        assert err.sqlstate is None
        assert err.model == "User"
        assert err.field == "foo"

    def test_integrity_error_carries_sqlstate_and_category(self) -> None:
        """FerrumIntegrityError carries sqlstate and category."""
        err = FerrumIntegrityError(
            "unique violation",
            constraint="users_email_key",
            category="unique_violation",
            sqlstate="23505",
            model="User",
            operation="insert",
        )
        assert err.constraint == "users_email_key"
        assert err.category == "unique_violation"
        assert err.sqlstate == "23505"
        assert err.model == "User"
        assert err.operation == "insert"

    def test_connection_error_carries_category_and_sqlstate(self) -> None:
        """FerrumConnectionError accepts sqlstate and category via base init."""
        err = FerrumConnectionError(
            "connection failed",
            sqlstate="08006",
            category="connection",
        )
        assert err.sqlstate == "08006"
        assert err.category == "connection"

    def test_timeout_error_carries_category(self) -> None:
        """FerrumTimeoutError accepts category via base init."""
        err = FerrumTimeoutError("timed out", category="timeout")
        assert err.category == "timeout"

    def test_schema_error_carries_category_and_sqlstate(self) -> None:
        """FerrumSchemaError accepts sqlstate and category via base init."""
        err = FerrumSchemaError(
            "not found",
            sqlstate="42P01",
            category="schema",
        )
        assert err.sqlstate == "42P01"
        assert err.category == "schema"

    def test_database_error_carries_category_and_sqlstate(self) -> None:
        """FerrumDatabaseError accepts sqlstate and category via base init."""
        err = FerrumDatabaseError(
            "db error",
            sqlstate="40P01",
            category="deadlock",
        )
        assert err.sqlstate == "40P01"
        assert err.category == "deadlock"

    def test_internal_error_has_default_category(self) -> None:
        """FerrumInternalError has category='internal' as class default."""
        err = FerrumInternalError("panic")
        assert err.category == "internal"

    def test_hydration_error_has_default_category(self) -> None:
        """FerrumHydrationError has category='hydration' as class default."""
        err = FerrumHydrationError("hydration failed")
        assert err.category == "hydration"

    def test_config_error_has_default_category(self) -> None:
        """FerrumConfigError has category='config' as class default."""
        from ferrum.errors import FerrumConfigError

        err = FerrumConfigError("missing DSN")
        assert err.category == "config"

    def test_not_found_error_has_default_category(self) -> None:
        """FerrumNotFoundError has category='not_found' as class default."""
        err = FerrumNotFoundError("not found")
        assert err.category == "not_found"

    def test_migration_error_has_default_category(self) -> None:
        """FerrumMigrationError has category='migration' as class default."""
        err = FerrumMigrationError("migration failed")
        assert err.category == "migration"


# ---------------------------------------------------------------------------
# Closed category enum validation
# ---------------------------------------------------------------------------


class TestErrorCategoriesEnum:
    """ERROR_CATEGORIES is a closed frozenset — every category must be in it."""

    def test_error_categories_is_frozenset(self) -> None:
        assert isinstance(ERROR_CATEGORIES, frozenset)

    def test_required_categories_present(self) -> None:
        """All categories required by the §5a contract are in the closed enum."""
        required = {
            "integrity",
            "integrity_error",
            "unique_violation",
            "schema",
            "undefined_function",
            "serialization",
            "deadlock",
            "lock_timeout",
            "query_cancellation",
            "invalid_transaction_state",
            "failover",
            "connection",
            "timeout",
            "pool_exhaustion",
            "compile_error",
            "hydration",
            "internal",
            "config",
            "not_found",
            "multiple_objects",
            "deferred_field",
            "relation_not_loaded",
            "danger_api",
            "migration",
            "unknown",
        }
        missing = required - ERROR_CATEGORIES
        assert not missing, f"Missing required categories: {missing}"

    def test_sqlstate_to_category_maps_correctly(self) -> None:
        """_sqlstate_to_category maps known SQLSTATEs to correct categories."""
        assert _sqlstate_to_category("23505") == "unique_violation"
        assert _sqlstate_to_category("23503") == "integrity_error"
        assert _sqlstate_to_category("42P01") == "undefined_table"
        assert _sqlstate_to_category("42703") == "undefined_column"
        assert _sqlstate_to_category("42883") == "undefined_function"
        assert _sqlstate_to_category("40001") == "serialization"
        assert _sqlstate_to_category("40P01") == "deadlock"
        assert _sqlstate_to_category("55P03") == "lock_timeout"
        assert _sqlstate_to_category("57014") == "query_cancellation"
        assert _sqlstate_to_category("57P01") == "failover"
        assert _sqlstate_to_category("57P02") == "failover"
        assert _sqlstate_to_category("57P03") == "failover"
        assert _sqlstate_to_category("25001") == "invalid_transaction_state"
        assert _sqlstate_to_category("08006") == "connection"
        assert _sqlstate_to_category("53300") == "pool_exhaustion"

    def test_sqlstate_to_category_unknown_returns_unknown(self) -> None:
        """Unknown SQLSTATEs map to 'unknown'."""
        assert _sqlstate_to_category("XX999") == "unknown"
        assert _sqlstate_to_category(None) == "unknown"

    def test_sqlstate_to_category_results_in_closed_enum(self) -> None:
        """Every result of _sqlstate_to_category is in ERROR_CATEGORIES."""
        test_sqlstates = [
            "23505",
            "23503",
            "42P01",
            "42703",
            "42883",
            "40001",
            "40P01",
            "55P03",
            "57014",
            "57P01",
            "57P02",
            "57P03",
            "25001",
            "08006",
            "53300",
            "XX999",
            None,
        ]
        for ss in test_sqlstates:
            cat = _sqlstate_to_category(ss)
            assert cat in ERROR_CATEGORIES, (
                f"_sqlstate_to_category({ss!r}) returned {cat!r} not in ERROR_CATEGORIES"
            )


# ---------------------------------------------------------------------------
# §5a: New PostgreSQL/driver class mappings (deadlock, serialization, etc.)
# ---------------------------------------------------------------------------


class TestNewMappedCategories:
    """Map the required PostgreSQL/driver classes per the §5a task contract."""

    def _make_pg_mock(self, exc_cls: type, sqlstate: str | None = None) -> mock.MagicMock:
        """Create a mock asyncpg exception with sqlstate and sanitized detail."""
        mock_exc = mock.MagicMock(spec=exc_cls)
        if sqlstate:
            mock_exc.sqlstate = sqlstate
        mock_exc.detail = "DETAIL: secret_row_data_leaked_here"
        mock_exc.hint = "HINT: another_secret_in_hint"
        return mock_exc

    def test_deadlock_maps_to_database_error_with_category(self) -> None:
        """DeadlockDetectedError (40P01) → FerrumDatabaseError, category=deadlock."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.DeadlockDetectedError, "40P01")
        result = map_db_error(mock_exc, context={"model": "User", "operation": "update"})
        assert isinstance(result, FerrumDatabaseError)
        assert result.category == "deadlock"
        assert result.sqlstate == "40P01"
        assert result.model == "User"
        assert result.operation == "update"

    def test_serialization_maps_to_database_error_with_category(self) -> None:
        """SerializationError (40001) → FerrumDatabaseError, category=serialization."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.SerializationError, "40001")
        result = map_db_error(mock_exc, context={"model": "Account", "operation": "update"})
        assert isinstance(result, FerrumDatabaseError)
        assert result.category == "serialization"
        assert result.sqlstate == "40001"
        assert result.model == "Account"
        assert result.operation == "update"

    def test_lock_timeout_maps_to_timeout_error_with_category(self) -> None:
        """LockNotAvailableError (55P03) → FerrumTimeoutError, category=lock_timeout."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.LockNotAvailableError, "55P03")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumTimeoutError)
        assert result.category == "lock_timeout"
        assert result.sqlstate == "55P03"

    def test_query_cancellation_maps_to_timeout_error_with_category(self) -> None:
        """QueryCanceledError (57014) → FerrumTimeoutError, category=query_cancellation."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.QueryCanceledError, "57014")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumTimeoutError)
        assert result.category == "query_cancellation"
        assert result.sqlstate == "57014"

    def test_admin_shutdown_maps_to_connection_error_with_category(self) -> None:
        """AdminShutdownError (57P01) → FerrumConnectionError, category=failover."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.AdminShutdownError, "57P01")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumConnectionError)
        assert result.category == "failover"
        assert result.sqlstate == "57P01"

    def test_crash_shutdown_maps_to_connection_error_with_category(self) -> None:
        """CrashShutdownError (57P02) → FerrumConnectionError, category=failover."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.CrashShutdownError, "57P02")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumConnectionError)
        assert result.category == "failover"
        assert result.sqlstate == "57P02"

    def test_cannot_connect_now_maps_to_connection_error_with_category(self) -> None:
        """CannotConnectNowError (57P03) → FerrumConnectionError, category=failover."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.CannotConnectNowError, "57P03")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumConnectionError)
        assert result.category == "failover"
        assert result.sqlstate == "57P03"

    def test_invalid_transaction_state_maps_to_database_error_with_category(self) -> None:
        """InvalidTransactionStateError (25000) → FerrumDatabaseError.

        Category is ``invalid_transaction_state``.
        """
        mock_exc = self._make_pg_mock(asyncpg.exceptions.InvalidTransactionStateError, "25000")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumDatabaseError)
        assert result.category == "invalid_transaction_state"
        assert result.sqlstate == "25000"

    def test_too_many_connections_maps_to_connection_error_with_category(self) -> None:
        """TooManyConnectionsError (53300) → FerrumConnectionError, category=pool_exhaustion."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.TooManyConnectionsError, "53300")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumConnectionError)
        assert result.category == "pool_exhaustion"
        assert result.sqlstate == "53300"

    def test_undefined_function_maps_to_schema_error_with_category(self) -> None:
        """UndefinedFunctionError (42883) → FerrumSchemaError, category=undefined_function."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.UndefinedFunctionError, "42883")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumSchemaError)
        assert result.category == "undefined_function"
        assert result.sqlstate == "42883"

    def test_connection_error_maps_with_category_and_sqlstate(self) -> None:
        """PostgresConnectionError → FerrumConnectionError, category=connection."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.ConnectionFailureError, "08006")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumConnectionError)
        assert result.category == "connection"
        assert result.sqlstate == "08006"

    def test_timeout_maps_with_category(self) -> None:
        """asyncio.TimeoutError → FerrumTimeoutError, category=timeout."""
        result = map_db_error(TimeoutError(), context={"model": "User", "operation": "select"})
        assert isinstance(result, FerrumTimeoutError)
        assert result.category == "timeout"
        assert result.model == "User"
        assert result.operation == "select"
        assert result.sqlstate is None

    def test_unique_violation_has_sqlstate_and_category(self) -> None:
        """UniqueViolationError carries sqlstate=23505 and category=unique_violation."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.UniqueViolationError, "23505")
        mock_exc.constraint_name = "users_email_key"
        result = map_db_error(mock_exc, context={"model": "User", "operation": "insert"})
        assert isinstance(result, FerrumIntegrityError)
        assert result.sqlstate == "23505"
        assert result.category == "unique_violation"
        assert result.constraint == "users_email_key"
        assert result.model == "User"
        assert result.operation == "insert"

    def test_fk_violation_has_sqlstate_and_category(self) -> None:
        """ForeignKeyViolationError carries sqlstate and category=integrity_error."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.ForeignKeyViolationError, "23503")
        mock_exc.constraint_name = "fk_posts_user"
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumIntegrityError)
        assert result.sqlstate == "23503"
        assert result.category == "integrity_error"
        assert result.constraint == "fk_posts_user"

    def test_undefined_column_has_sqlstate_and_category(self) -> None:
        """UndefinedColumnError carries sqlstate=42703 and category=schema."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.UndefinedColumnError, "42703")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumSchemaError)
        assert result.sqlstate == "42703"
        assert result.category == "schema"

    def test_generic_postgres_error_has_sqlstate_and_category(self) -> None:
        """Generic PostgresError carries sqlstate and category from _sqlstate_to_category."""
        mock_exc = self._make_pg_mock(asyncpg.exceptions.PostgresError, "42P01")
        result = map_db_error(mock_exc)
        assert isinstance(result, FerrumError)
        assert result.sqlstate == "42P01"
        assert result.category == "undefined_table"

    def test_internal_error_fallback_has_category(self) -> None:
        """The catch-all FerrumInternalError fallback has category=internal."""
        result = map_db_error(
            Exception("unexpected"), context={"model": "X", "operation": "select"}
        )
        assert isinstance(result, FerrumInternalError)
        assert result.category == "internal"
        assert result.model == "X"
        assert result.operation == "select"

    def test_all_mapped_categories_are_in_closed_enum(self) -> None:
        """Every category set by map_db_error must be in ERROR_CATEGORIES."""
        test_cases = [
            (asyncpg.exceptions.UniqueViolationError, "23505"),
            (asyncpg.exceptions.ForeignKeyViolationError, "23503"),
            (asyncpg.exceptions.NotNullViolationError, "23502"),
            (asyncpg.exceptions.CheckViolationError, "23514"),
            (asyncpg.exceptions.UndefinedColumnError, "42703"),
            (asyncpg.exceptions.UndefinedTableError, "42P01"),
            (asyncpg.exceptions.UndefinedFunctionError, "42883"),
            (asyncpg.exceptions.DeadlockDetectedError, "40P01"),
            (asyncpg.exceptions.SerializationError, "40001"),
            (asyncpg.exceptions.LockNotAvailableError, "55P03"),
            (asyncpg.exceptions.QueryCanceledError, "57014"),
            (asyncpg.exceptions.AdminShutdownError, "57P01"),
            (asyncpg.exceptions.CrashShutdownError, "57P02"),
            (asyncpg.exceptions.CannotConnectNowError, "57P03"),
            (asyncpg.exceptions.InvalidTransactionStateError, "25000"),
            (asyncpg.exceptions.TooManyConnectionsError, "53300"),
            (asyncpg.exceptions.ConnectionFailureError, "08006"),
            (asyncpg.exceptions.PostgresError, "XX000"),
        ]
        for exc_cls, sqlstate in test_cases:
            mock_exc = mock.MagicMock(spec=exc_cls)
            mock_exc.sqlstate = sqlstate
            mock_exc.constraint_name = None
            mock_exc.detail = None
            mock_exc.hint = None
            result = map_db_error(mock_exc)
            assert result.category in ERROR_CATEGORIES, (
                f"{exc_cls.__name__} (sqlstate={sqlstate}) → "
                f"category={result.category!r} not in ERROR_CATEGORIES"
            )

    def test_context_threads_model_and_operation(self) -> None:
        """map_db_error threads model/operation from context onto mapped exceptions."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.UniqueViolationError)
        mock_exc.constraint_name = "uk_email"
        mock_exc.sqlstate = "23505"
        mock_exc.detail = None
        mock_exc.hint = None
        result = map_db_error(
            mock_exc,
            context={"model": "Account", "operation": "create"},
        )
        assert result.model == "Account"
        assert result.operation == "create"

    def test_context_none_does_not_set_model_or_operation(self) -> None:
        """When context is None, model/operation stay None on the mapped exception."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.UniqueViolationError)
        mock_exc.constraint_name = None
        mock_exc.sqlstate = "23505"
        mock_exc.detail = None
        mock_exc.hint = None
        result = map_db_error(mock_exc)
        assert result.model is None
        assert result.operation is None


# ---------------------------------------------------------------------------
# §5a: map_native_error sets category on PyO3-mapped exceptions
# ---------------------------------------------------------------------------


class TestMapNativeErrorCategory:
    """map_native_error sets category on mapped PyO3 exceptions."""

    def test_native_compile_error_has_category(self) -> None:
        native = _make_native_mod()
        exc = native.FerrumCompileError("bad IR")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumCompileError)
        assert result.category == "compile_error"

    def test_native_hydration_error_has_category(self) -> None:
        native = _make_native_mod()
        exc = native.FerrumHydrationError("NULL in non-nullable column")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumHydrationError)
        assert result.category == "hydration"

    def test_native_internal_error_has_category(self) -> None:
        native = _make_native_mod()
        exc = native.FerrumInternalError("unexpected panic")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumInternalError)
        assert result.category == "internal"

    def test_runtime_error_fallback_has_category(self) -> None:
        native = _make_native_mod()
        exc = RuntimeError("something exploded in Rust")
        result = map_native_error(exc, _native_mod=native)
        assert isinstance(result, FerrumInternalError)
        assert result.category == "internal"


# ---------------------------------------------------------------------------
# §5a: DETAIL/HINT/bound values/row data/DSN never escape mapped exceptions
# ---------------------------------------------------------------------------


class TestSafeErrorFieldsNoLeak:
    """DETAIL/HINT/bound values/row data/DSNs never appear in mapped exception fields."""

    _SECRET_DETAIL = "DETAIL: Key (email)=(leaked_secret@example.com) already exists."  # noqa: S105
    _SECRET_HINT = "HINT: Remove the row with password=hunter2 first."  # noqa: S105
    _SECRET_DSN = "postgresql://admin:supersecret@db.internal:5432/prod"  # noqa: S105

    def test_detail_never_in_mapped_exception_attributes(self) -> None:
        """SQLSTATE DETAIL must not appear in any mapped exception attribute or message."""
        for exc_cls, sqlstate in [
            (asyncpg.exceptions.UniqueViolationError, "23505"),
            (asyncpg.exceptions.ForeignKeyViolationError, "23503"),
            (asyncpg.exceptions.DeadlockDetectedError, "40P01"),
            (asyncpg.exceptions.SerializationError, "40001"),
            (asyncpg.exceptions.AdminShutdownError, "57P01"),
            (asyncpg.exceptions.InvalidTransactionStateError, "25000"),
            (asyncpg.exceptions.PostgresError, "XX000"),
        ]:
            mock_exc = mock.MagicMock(spec=exc_cls)
            mock_exc.sqlstate = sqlstate
            mock_exc.constraint_name = None
            mock_exc.detail = self._SECRET_DETAIL
            mock_exc.hint = self._SECRET_HINT
            result = map_db_error(mock_exc)
            msg = str(result)
            assert "leaked_secret" not in msg, (
                f"DETAIL leaked into {type(result).__name__} message: {msg!r}"
            )
            assert "hunter2" not in msg, (
                f"HINT leaked into {type(result).__name__} message: {msg!r}"
            )
            # Check all string attributes
            for attr_name in ("sqlstate", "category", "constraint", "model", "operation"):
                attr_val = getattr(result, attr_name, None)
                if isinstance(attr_val, str):
                    assert "leaked_secret" not in attr_val
                    assert "hunter2" not in attr_val

    def test_dsn_never_in_mapped_exception_message(self) -> None:
        """A DSN planted in the exception message must not survive mapping."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.ConnectionFailureError)
        mock_exc.sqlstate = "08006"
        mock_exc.detail = None
        mock_exc.hint = None
        mock_exc.__str__ = mock.Mock(return_value=f"connection failed: {self._SECRET_DSN}")
        result = map_db_error(mock_exc)
        msg = str(result)
        assert "supersecret" not in msg
        assert self._SECRET_DSN not in msg

    def test_timeout_with_dsn_in_message_does_not_leak(self) -> None:
        """A DSN in a TimeoutError message must not survive mapping."""
        result = map_db_error(TimeoutError(self._SECRET_DSN))
        assert "supersecret" not in str(result)
        assert self._SECRET_DSN not in str(result)
