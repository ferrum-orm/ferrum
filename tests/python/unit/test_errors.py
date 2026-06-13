"""Unit tests for the Ferrum error taxonomy."""

from __future__ import annotations

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
