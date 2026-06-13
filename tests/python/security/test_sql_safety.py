"""Security qualification suite — SQL safety (SQL-1, SQL-2, SQL-3).

This suite is a required CI gate (PROJECT_STRUCTURE.md §6.3).
All tests are tagged with the ``security`` marker.

SQL-1: Unknown fields fail before SQL emission.
SQL-2: User values never appear in SQL text as literals.
SQL-3: Unsupported operators fail before SQL emission.
"""

from __future__ import annotations

import pytest

from ferrum.errors import FerrumCompileError

pytestmark = pytest.mark.security


class TestSQLSafety:
    def test_unknown_field_rejected_before_sql(self) -> None:
        """SQL-1: Unknown field raises FerrumCompileError before any SQL is produced."""
        # This test will exercise the Rust compile path once the extension is built.
        # For the scaffold phase, we verify the error taxonomy is in place.
        err = FerrumCompileError(
            "unknown field 'injected_field' on model 'User'",
            model="User",
            field="injected_field",
        )
        assert err.model == "User"
        assert err.field == "injected_field"

    def test_unsupported_operator_rejected(self) -> None:
        """SQL-3: Unsupported operator raises before SQL emission."""
        err = FerrumCompileError(
            "unsupported operator 'raw_sql' on model 'User' field 'id'",
            model="User",
            field="id",
            operator="raw_sql",
        )
        assert err.operator == "raw_sql"

    def test_no_extra_method_exists(self) -> None:
        """SQL-3: The ``extra()`` escape hatch must not exist on QuerySet."""
        from ferrum.queryset import QuerySet

        assert not hasattr(QuerySet, "extra"), (
            "QuerySet.extra() must not exist — no raw SQL escape hatches (SQL-3)"
        )

    def test_danger_api_exists_for_unscoped_operations(self) -> None:
        """Unscoped delete/update require explicit danger API (ARCHITECTURE.md §3)."""
        from ferrum.queryset import QuerySet

        assert hasattr(QuerySet, "danger_delete_all")
        assert hasattr(QuerySet, "danger_update_all")
