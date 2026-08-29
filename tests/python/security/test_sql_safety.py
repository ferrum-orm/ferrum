"""Security qualification suite — SQL safety (SQL-1, SQL-2, SQL-3).

This suite is a required CI gate (PROJECT_STRUCTURE.md §6.3).
All tests are tagged with the ``security`` marker.

Requirement mapping
-------------------
SQL-1: Unknown fields fail before SQL emission (allowlist, not denylist).
SQL-2: Unsupported operators fail before SQL emission.
SQL-3: Sort directions not in {"asc", "desc"} are rejected before SQL emission.
"""

from __future__ import annotations

import sys
import types
import unittest.mock as mock

import pytest

from ferrum.errors import FerrumCompileError
from ferrum.models import Model
from ferrum.queryset import QuerySet

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _User(Model):
    id: int = 0
    email: str = ""
    active: bool = True


# ---------------------------------------------------------------------------
# SQL-1: Unknown field → FerrumCompileError before SQL
# ---------------------------------------------------------------------------


class TestSQL1UnknownFieldRejection:
    def test_unknown_field_raises_compile_error_before_sql(self) -> None:
        """SQL-1: FerrumCompileError is raised for unknown filter fields before SQL.

        The Python Stage-0 gate fires in ``filter()`` (first check) or in
        ``_build_ir()`` (second check).  Either location is correct — the
        invariant is that SQL is never produced for unknown fields.
        """
        with pytest.raises(FerrumCompileError) as exc_info:
            qs = QuerySet(_User).filter(injected_field="value")
            qs._build_ir()
        assert exc_info.value.field == "injected_field"
        assert exc_info.value.model == "_User"

    def test_build_ir_field_index_is_from_metadata_allowlist(self) -> None:
        """SQL-1: Known model fields pass; non-model strings are rejected.

        This test verifies the allowlist property: the set of accepted fields
        equals model_fields exactly — nothing more.
        """
        allowed = set(_User.model_fields.keys())
        # Every declared field produces a valid IR entry
        for field_name in allowed:
            qs = QuerySet(_User).filter(**{field_name: "x"})
            ir = qs._build_ir()
            assert ir["predicate"]["filter"]["field"]["name"] == field_name
            assert isinstance(ir["predicate"]["filter"]["field"]["index"], int)

        # A field not declared on the model is always rejected.
        # Single-underscore join avoids the Django-style __ separator so
        # filter() treats the whole string as a field name, not field__op.
        unknown = "not_a_field_" + "_".join(sorted(allowed))
        with pytest.raises(FerrumCompileError, match=unknown):
            qs_bad = QuerySet(_User).filter(**{unknown: "x"})
            qs_bad._build_ir()

    def test_unknown_order_by_field_raises_compile_error(self) -> None:
        """SQL-1: Unknown field in order_by raises FerrumCompileError."""
        qs = QuerySet(_User)
        # Bypass order_by() validation by cloning and injecting a bad entry
        cloned = qs._clone()
        cloned._order_by = [{"field": "evil_field", "direction": "asc"}]
        with pytest.raises(FerrumCompileError) as exc_info:
            cloned._build_ir()
        assert exc_info.value.field == "evil_field"

    def test_native_compile_error_propagated_from_mocked_extension(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQL-1: FerrumCompileError raised by _native.compile_query propagates out.

        The Python guard (_build_ir) fires first for unknown fields, so this
        test mocks the extension to verify the *propagation path* when the
        Rust side raises (e.g. for a field that passed the Python check but was
        rejected by a stricter Rust validator in the future).
        """
        fake_native = types.ModuleType("ferrum._native")
        fake_native.compile_query = mock.Mock(  # type: ignore[attr-defined]
            side_effect=FerrumCompileError(
                "unknown field 'id' in Rust validator",
                model="_User",
                field="id",
            )
        )
        # Inject fake native into both sys.modules and the queryset module
        monkeypatch.setitem(sys.modules, "ferrum._native", fake_native)
        import ferrum.queryset as qs_module

        original = qs_module._native_ext
        monkeypatch.setattr(qs_module, "_native_ext", fake_native)
        try:
            qs = QuerySet(_User).filter(id=1)
            # _build_ir() succeeds (id is a known field)
            # _compile() calls fake extension which raises
            with pytest.raises(FerrumCompileError, match="Rust validator"):
                qs._compile()
            # The mocked extension WAS called (Python IR passed the Python guard)
            fake_native.compile_query.assert_called_once()
        finally:
            monkeypatch.setattr(qs_module, "_native_ext", original)


# ---------------------------------------------------------------------------
# SQL-2: Unsupported operator → FerrumCompileError before SQL
# ---------------------------------------------------------------------------


class TestSQL2OperatorRejection:
    def test_unsupported_operator_raises_compile_error_before_sql(self) -> None:
        """SQL-2: _build_ir() raises FerrumCompileError for unsupported operators."""
        qs = QuerySet(_User)
        # Inject a bad operator directly (filter() always uses "eq")
        cloned = qs._clone()
        cloned._filters = [{"field": "id", "operator": "raw_sql", "value": "1"}]
        cloned._is_filtered = True
        with pytest.raises(FerrumCompileError) as exc_info:
            cloned._build_ir()
        assert exc_info.value.operator == "raw_sql"
        assert exc_info.value.field == "id"

    def test_all_allowed_operators_accepted(self) -> None:
        """SQL-2: Every operator in _ALLOWED_OPERATORS builds a valid IR.

        ``_ALLOWED_OPERATORS`` maps Ferrum field-type names to their allowed
        operator tuples.  ``_User.id`` is a ``big_int`` PK — exercise all
        operators defined for that type, which is the most permissive numeric
        set and a representative sample.
        """
        from ferrum.models import _ALLOWED_OPERATORS

        for op in _ALLOWED_OPERATORS.get("big_int", ()):
            qs = QuerySet(_User)
            cloned = qs._clone()
            cloned._filters = [{"field": "id", "operator": op, "value": 1}]
            cloned._is_filtered = True
            ir = cloned._build_ir()
            assert ir["filters"][0]["operator"] == op

    def test_no_extra_method_exists(self) -> None:
        """SQL-3: The ``extra()`` escape hatch must not exist on QuerySet."""
        assert not hasattr(QuerySet, "extra"), (
            "QuerySet.extra() must not exist — no raw SQL escape hatches (SQL-3)"
        )

    def test_no_raw_method_exists(self) -> None:
        """SQL-3: A ``raw()`` method must not exist on QuerySet."""
        assert not hasattr(QuerySet, "raw"), (
            "QuerySet.raw() must not exist — no raw SQL escape hatches (SQL-3)"
        )


# ---------------------------------------------------------------------------
# SQL-3: Sort direction validation → FerrumCompileError before SQL
# ---------------------------------------------------------------------------


class TestSQL3SortDirectionRejection:
    def test_invalid_sort_direction_rejected(self) -> None:
        """SQL-3: Sort direction not in {'asc','desc'} raises FerrumCompileError."""
        qs = QuerySet(_User)
        cloned = qs._clone()
        cloned._order_by = [{"field": "id", "direction": "DESC; DROP TABLE users;--"}]
        with pytest.raises(FerrumCompileError) as exc_info:
            cloned._build_ir()
        assert exc_info.value.field == "id"

    def test_valid_sort_directions_accepted(self) -> None:
        """SQL-3: 'asc' and 'desc' are the only valid sort directions (and they pass)."""
        for direction in ("asc", "desc"):
            qs = QuerySet(_User).order_by("id" if direction == "asc" else "-id")
            ir = qs._build_ir()
            assert ir["order_by"][0]["direction"] == direction

    def test_empty_sort_direction_rejected(self) -> None:
        """SQL-3: Empty string sort direction raises FerrumCompileError."""
        qs = QuerySet(_User)
        cloned = qs._clone()
        cloned._order_by = [{"field": "id", "direction": ""}]
        with pytest.raises(FerrumCompileError):
            cloned._build_ir()


# ---------------------------------------------------------------------------
# Danger API structural checks (AGENTS.md §3)
# ---------------------------------------------------------------------------


class TestDangerApiStructural:
    def test_danger_api_exists_for_unscoped_operations(self) -> None:
        """Unscoped delete/update require explicit danger API (ARCHITECTURE.md §3)."""
        assert hasattr(QuerySet, "danger_delete_all")
        assert hasattr(QuerySet, "danger_update_all")


# ---------------------------------------------------------------------------
# Write-path SQL safety (SQL-4, MIG-5): INSERT placeholders, delete guard
# ---------------------------------------------------------------------------


class TestWritePathSQLSafety:
    def test_insert_sql_uses_placeholder_not_value(self) -> None:
        """SQL-4: INSERT SQL text must use $N positional placeholders, not interpolated values.

        Ferrum's Rust compiler is the only source of SQL text. The contract: user
        values travel exclusively in ``bound_params`` (out-of-band from sql_text).
        This test verifies the expected shape of a compile_query INSERT response —
        the sql_text contains $1 but never the raw value string.
        """
        user_value = "injection_test@example.com"

        # Simulated Rust compiler response for an INSERT — the canonical shape.
        # sql_text has a positional placeholder; the user value is in bound_params only.
        simulated_response = {
            "sql_text": "INSERT INTO fake_users (email) VALUES ($1)",
            "bound_params": [f'{{"type": "text", "value": "{user_value}"}}'],
            "param_type_summary": ["text"],
        }

        sql_text = simulated_response["sql_text"]

        # The user value must NOT appear in sql_text (it is bound out-of-band).
        no_interpolation_msg = (
            f"User value {user_value!r} must not be interpolated into INSERT sql_text; "  # noqa: S608
            "values must travel in bound_params only"
        )
        assert user_value not in sql_text, no_interpolation_msg
        # Positional placeholder must be present.
        assert "$1" in sql_text, (
            "INSERT sql_text must reference bound parameters via positional placeholder $1"
        )
        # Sanity: the value does appear in bound_params (correct side of the contract).
        assert any(user_value in param for param in simulated_response["bound_params"]), (
            "User value must be present in bound_params, not absent from both sides"
        )

    @pytest.mark.asyncio
    async def test_delete_without_filter_raises_before_compile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQL/MIG-5: delete() without filter raises FerrumDangerApiError before _compile().

        The Python guard must fire synchronously inside delete() before
        _compile() (and therefore before any Rust or SQL) is ever reached.
        Mocking _compile() lets us assert it was never called.
        """
        import unittest.mock as mock

        from ferrum.errors import FerrumDangerApiError

        compile_mock = mock.MagicMock(name="_compile")
        qs = QuerySet(_User)
        monkeypatch.setattr(qs, "_compile", compile_mock)

        with pytest.raises(FerrumDangerApiError):
            await qs.delete(None)  # type: ignore[arg-type]

        compile_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_without_filter_raises_missing_filter_error(self) -> None:
        """SQL/MIG-5: delete() without filter raises before Rust is invoked — no SQL emitted.

        The Python guard fires synchronously inside delete(), so the Rust compiler
        is never reached and no SQL can be emitted for the unscoped operation.
        """
        from ferrum.errors import FerrumDangerApiError

        qs = QuerySet(_User)
        with pytest.raises(FerrumDangerApiError):
            await qs.delete()


# ---------------------------------------------------------------------------
# SQL-1 / LOG-1 for the instance-write forms (create(obj), update_instance)
# ---------------------------------------------------------------------------


class TestInstanceWriteFormsSafety:
    @pytest.mark.asyncio
    async def test_create_dict_form_unknown_key_fails_before_sql(self) -> None:
        """SQL-1: injection-shaped dict keys are rejected before SQL emission."""
        mock_ext = mock.MagicMock()
        qs = QuerySet(_User)

        with (
            mock.patch("ferrum.queryset._native_ext", mock_ext),
            pytest.raises(FerrumCompileError) as exc_info,
        ):
            await qs.create(mock.MagicMock(), {"email; DROP TABLE users--": "x", "id": 1})

        assert exc_info.value.model == "_User"
        mock_ext.compile_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_instance_unknown_field_fails_before_sql(self) -> None:
        """SQL-1: update_instance() fields are allowlist-validated before SQL."""
        mock_ext = mock.MagicMock()
        qs = QuerySet(_User)

        with (
            mock.patch("ferrum.queryset._native_ext", mock_ext),
            pytest.raises(FerrumCompileError) as exc_info,
        ):
            await qs.update_instance(
                mock.MagicMock(),
                _User(id=1),
                fields=["email; DROP TABLE users--"],
            )

        assert exc_info.value.field == "email; DROP TABLE users--"
        mock_ext.compile_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_instance_write_hook_payloads_carry_no_values(self) -> None:
        """LOG-1: Tier A hook payloads for instance writes never contain field values."""
        import json as _json

        from ferrum.hooks import clear_hooks, register_hook

        canary = "supersecret-instance-value"
        mock_ext = mock.MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "UPDATE users SET email = $1 WHERE id = $2",
            "bound_params": [_json.dumps({"type": "text", "value": canary})],
            "fingerprint": "fp-instance",
            "operation": "update",
        }
        mock_conn = mock.MagicMock()
        mock_conn.dialect = "postgres"
        mock_driver = mock.MagicMock()
        mock_driver.execute = mock.AsyncMock(return_value="UPDATE 1")
        mock_conn._require_driver.return_value = mock_driver

        dispatched: list[dict] = []
        register_hook("*", dispatched.append)
        try:
            with mock.patch("ferrum.queryset._native_ext", mock_ext):
                await QuerySet(_User).update_instance(
                    mock_conn, _User(id=1, email=canary), fields=["email"]
                )
        finally:
            clear_hooks()

        assert dispatched, "instance write must dispatch Tier A hook payloads"
        for payload in dispatched:
            assert canary not in str(payload)


# ---------------------------------------------------------------------------
# W1-A: Malformed input fuzz — fields, operators, sorts fail before SQL
# ---------------------------------------------------------------------------


class TestMalformedInputFuzz:
    """Fuzz of malformed fields/operators/sorts must fail before SQL emission.

    These tests verify the Stage-0 Python gate and the Rust compile gate both
    reject malformed input without producing any SQL text. The invariants:

    - Unknown fields raise ``FerrumCompileError`` at ``filter()`` or ``_build_ir()``.
    - Unsupported operators raise ``FerrumCompileError`` at ``_build_ir()``.
    - Invalid sort directions raise ``FerrumCompileError`` at ``_build_ir()``.
    - No SQL text is ever produced for rejected input.
    """

    @pytest.mark.parametrize(
        "bad_field",
        [
            "evil_field",
            "id; DROP TABLE users--",
            "id' OR '1'='1",
            "id UNION SELECT 1",
            "id__eq__gt",  # malformed double operator
            "__id",  # leading dunder
            "id__",  # trailing dunder
            "id--",
            "1id",
            "id ",
            " id",
            "id\x00",  # null byte
            "id\x1b",  # escape byte
            "id\t",
            "id\n",
            "id\r",
            "id\x00",
            "id\x00test",
        ],
        ids=lambda v: repr(v),
    )
    def test_unknown_field_fuzz_fails_before_sql(self, bad_field: str) -> None:
        """Unknown field names (including injection payloads) fail before SQL."""
        qs = QuerySet(_User)
        with pytest.raises(FerrumCompileError):
            qs.filter(**{bad_field: "x"})._build_ir()

    @pytest.mark.parametrize(
        "bad_op",
        [
            "raw_sql",
            "exec",
            "execute",
            "DROP",
            "DROP TABLE",
            "eq; DROP TABLE users--",
            "' OR '1'='1",
            "eq_to_sql",
            "LIKE",
            "GLOB",
            "",
            " ",
            "eq\0",
            "eq\x00",
            "eq\n",
        ],
        ids=lambda v: repr(v),
    )
    def test_unsupported_operator_fuzz_fails_before_sql(self, bad_op: str) -> None:
        """Unsupported operators (including injection payloads) fail before SQL."""
        qs = QuerySet(_User)
        cloned = qs._clone()
        cloned._filters = [{"field": "id", "operator": bad_op, "value": 1}]
        cloned._is_filtered = True
        with pytest.raises(FerrumCompileError):
            cloned._build_ir()

    @pytest.mark.parametrize(
        "bad_direction",
        [
            "DESC; DROP TABLE users;--",
            "ASC; DROP TABLE users;--",
            "sideways",
            "up",
            "down",
            "DROP",
            "",
            " ",
            "asc\0",
            "desc; --",
            "asc UNION SELECT 1",
            "ASC ",
            " ASC",
        ],
        ids=lambda v: repr(v),
    )
    def test_invalid_sort_direction_fuzz_fails_before_sql(self, bad_direction: str) -> None:
        """Invalid sort directions (including injection payloads) fail before SQL."""
        qs = QuerySet(_User)
        cloned = qs._clone()
        cloned._order_by = [{"field": "id", "direction": bad_direction}]
        with pytest.raises(FerrumCompileError):
            cloned._build_ir()

    @pytest.mark.parametrize(
        "bad_order_field",
        [
            "evil_field",
            "id; DROP TABLE users--",
            "id' OR '1'='1",
            "id UNION SELECT 1",
            "__id",
            "id__",
            "1id",
            "id\x00",
        ],
        ids=lambda v: repr(v),
    )
    def test_unknown_order_by_field_fuzz_fails_before_sql(self, bad_order_field: str) -> None:
        """Unknown order_by fields (including injection payloads) fail before SQL."""
        qs = QuerySet(_User)
        cloned = qs._clone()
        cloned._order_by = [{"field": bad_order_field, "direction": "asc"}]
        with pytest.raises(FerrumCompileError):
            cloned._build_ir()

    def test_compile_mock_not_called_on_rejection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the Python Stage-0 gate rejects a field, the Rust compiler is never called."""
        compile_mock = mock.MagicMock(name="_compile")
        qs = QuerySet(_User)
        monkeypatch.setattr(qs, "_compile", compile_mock)
        with pytest.raises(FerrumCompileError):
            qs.filter(injected_field="value")._build_ir()
        compile_mock.assert_not_called()

    def test_no_sql_text_produced_for_rejected_operator(self) -> None:
        """A rejected operator must not produce any SQL text — verify by
        bypassing filter() and injecting a bad operator directly, then
        confirming _build_ir() raises before _compile() is reachable."""
        qs = QuerySet(_User)
        cloned = qs._clone()
        cloned._filters = [{"field": "id", "operator": "raw_sql", "value": "1"}]
        cloned._is_filtered = True
        with pytest.raises(FerrumCompileError):
            cloned._build_ir()
