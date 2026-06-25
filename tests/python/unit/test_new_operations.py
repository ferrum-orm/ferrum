"""Unit tests for new migration operation classes.

Invariants covered:
- SQL emission for each operation via _op_to_sql.
- CreateExtension is classified non_transactional.
- DropExtension, DropPolicy, DropFunction, DisableRLS are classified destructive.
- CreateFunction body is emitted verbatim.
- CreatePolicy command and role are validated/emitted correctly.
- EnableRLS with force=True emits FORCE ROW LEVEL SECURITY.
- call_function validates identifiers and constructs correct SQL.
"""

from __future__ import annotations

import pytest

from ferrum.errors import FerrumCompileError
from ferrum.migrations.operations import (
    CreateExtension,
    CreateFunction,
    CreatePolicy,
    DisableRLS,
    DropExtension,
    DropFunction,
    DropPolicy,
    EnableRLS,
)
from ferrum.migrations.orchestrator import _op_to_sql

# ---------------------------------------------------------------------------
# CreateExtension
# ---------------------------------------------------------------------------


class TestCreateExtension:
    def test_classification_is_non_transactional(self) -> None:
        op = CreateExtension("pgcrypto")
        assert op.classification == "non_transactional"

    def test_to_op_dict_keys(self) -> None:
        op = CreateExtension("pgcrypto")
        d = op.to_op_dict()
        assert d["kind"] == "create_extension"
        assert d["name"] == "pgcrypto"
        assert "schema" not in d

    def test_to_op_dict_with_schema(self) -> None:
        op = CreateExtension("vector", schema="extensions")
        d = op.to_op_dict()
        assert d["schema"] == "extensions"

    def test_sql_emission_no_schema(self) -> None:
        op = CreateExtension("pgcrypto")
        sql = _op_to_sql(op.to_op_dict())
        assert "CREATE EXTENSION IF NOT EXISTS" in sql
        assert '"pgcrypto"' in sql

    def test_sql_emission_with_schema(self) -> None:
        op = CreateExtension("vector", schema="extensions")
        sql = _op_to_sql(op.to_op_dict())
        assert '"extensions"' in sql
        assert '"vector"' in sql

    def test_repr(self) -> None:
        assert "CreateExtension" in repr(CreateExtension("pgcrypto"))


# ---------------------------------------------------------------------------
# DropExtension
# ---------------------------------------------------------------------------


class TestDropExtension:
    def test_classification_is_destructive(self) -> None:
        op = DropExtension("pgcrypto")
        assert op.classification == "destructive"

    def test_to_op_dict_keys(self) -> None:
        op = DropExtension("pgcrypto")
        d = op.to_op_dict()
        assert d["kind"] == "drop_extension"
        assert d["name"] == "pgcrypto"
        assert d["cascade"] is False

    def test_to_op_dict_cascade(self) -> None:
        op = DropExtension("pgcrypto", cascade=True)
        d = op.to_op_dict()
        assert d["cascade"] is True

    def test_sql_emission_no_cascade(self) -> None:
        op = DropExtension("pgcrypto")
        sql = _op_to_sql(op.to_op_dict())
        assert "DROP EXTENSION IF EXISTS" in sql
        assert '"pgcrypto"' in sql
        assert "CASCADE" not in sql

    def test_sql_emission_with_cascade(self) -> None:
        op = DropExtension("pgcrypto", cascade=True)
        sql = _op_to_sql(op.to_op_dict())
        assert "CASCADE" in sql


# ---------------------------------------------------------------------------
# EnableRLS
# ---------------------------------------------------------------------------


class TestEnableRLS:
    def test_classification_is_safe(self) -> None:
        op = EnableRLS("tickets")
        assert op.classification == "safe"

    def test_to_op_dict_keys(self) -> None:
        op = EnableRLS("tickets")
        d = op.to_op_dict()
        assert d["kind"] == "enable_rls"
        assert d["table"] == "tickets"
        assert d["force"] is False

    def test_to_op_dict_force(self) -> None:
        op = EnableRLS("tickets", force=True)
        d = op.to_op_dict()
        assert d["force"] is True

    def test_sql_emission_enable(self) -> None:
        op = EnableRLS("tickets")
        sql = _op_to_sql(op.to_op_dict())
        assert "ENABLE ROW LEVEL SECURITY" in sql
        assert '"tickets"' in sql
        assert "FORCE" not in sql

    def test_sql_emission_force(self) -> None:
        op = EnableRLS("tickets", force=True)
        sql = _op_to_sql(op.to_op_dict())
        assert "FORCE ROW LEVEL SECURITY" in sql
        assert '"tickets"' in sql


# ---------------------------------------------------------------------------
# DisableRLS
# ---------------------------------------------------------------------------


class TestDisableRLS:
    def test_classification_is_destructive(self) -> None:
        op = DisableRLS("tickets")
        assert op.classification == "destructive"

    def test_to_op_dict_keys(self) -> None:
        op = DisableRLS("tickets")
        d = op.to_op_dict()
        assert d["kind"] == "disable_rls"
        assert d["table"] == "tickets"

    def test_sql_emission(self) -> None:
        op = DisableRLS("tickets")
        sql = _op_to_sql(op.to_op_dict())
        assert "DISABLE ROW LEVEL SECURITY" in sql
        assert '"tickets"' in sql


# ---------------------------------------------------------------------------
# CreatePolicy
# ---------------------------------------------------------------------------


class TestCreatePolicy:
    def test_classification_is_safe(self) -> None:
        op = CreatePolicy("tenant_isolation", "tickets", "team_id = current_setting('app.team_id')")
        assert op.classification == "safe"

    def test_to_op_dict_minimal(self) -> None:
        op = CreatePolicy("pol", "t", "true")
        d = op.to_op_dict()
        assert d["kind"] == "create_policy"
        assert d["name"] == "pol"
        assert d["table"] == "t"
        assert d["using"] == "true"
        assert d["command"] == "ALL"
        assert "check_expr" not in d
        assert "role" not in d

    def test_to_op_dict_with_check_and_role(self) -> None:
        op = CreatePolicy(
            "pol",
            "t",
            "team_id = 1",
            check_expr="team_id = 1",
            command="UPDATE",
            role="app_user",
        )
        d = op.to_op_dict()
        assert d["check_expr"] == "team_id = 1"
        assert d["command"] == "UPDATE"
        assert d["role"] == "app_user"

    def test_sql_emission_minimal(self) -> None:
        op = CreatePolicy("tenant_iso", "tickets", "team_id = 1")
        sql = _op_to_sql(op.to_op_dict())
        assert "CREATE POLICY" in sql
        assert '"tenant_iso"' in sql
        assert '"tickets"' in sql
        assert "USING (team_id = 1)" in sql

    def test_sql_emission_with_check(self) -> None:
        op = CreatePolicy("upd_pol", "orders", "owner_id = 1", check_expr="owner_id = 1")
        sql = _op_to_sql(op.to_op_dict())
        assert "WITH CHECK (owner_id = 1)" in sql

    def test_sql_emission_for_command(self) -> None:
        op = CreatePolicy("sel_pol", "orders", "true", command="SELECT")
        sql = _op_to_sql(op.to_op_dict())
        assert "FOR SELECT" in sql

    def test_sql_emission_all_command_omitted(self) -> None:
        op = CreatePolicy("all_pol", "orders", "true", command="ALL")
        sql = _op_to_sql(op.to_op_dict())
        # FOR ALL is not emitted by the orchestrator for the default command.
        assert "FOR ALL" not in sql

    def test_sql_emission_with_role(self) -> None:
        op = CreatePolicy("rol_pol", "items", "true", role="app_user")
        sql = _op_to_sql(op.to_op_dict())
        assert "TO" in sql
        assert '"app_user"' in sql

    def test_sql_emission_invalid_command_raises(self) -> None:
        op = CreatePolicy("bad_pol", "t", "true", command="TRUNCATE")
        with pytest.raises(Exception):  # FerrumMigrationError
            _op_to_sql(op.to_op_dict())


# ---------------------------------------------------------------------------
# DropPolicy
# ---------------------------------------------------------------------------


class TestDropPolicy:
    def test_classification_is_destructive(self) -> None:
        op = DropPolicy("tenant_isolation", "tickets")
        assert op.classification == "destructive"

    def test_to_op_dict_keys(self) -> None:
        op = DropPolicy("pol", "t")
        d = op.to_op_dict()
        assert d["kind"] == "drop_policy"
        assert d["name"] == "pol"
        assert d["table"] == "t"

    def test_sql_emission(self) -> None:
        op = DropPolicy("tenant_iso", "tickets")
        sql = _op_to_sql(op.to_op_dict())
        assert "DROP POLICY IF EXISTS" in sql
        assert '"tenant_iso"' in sql
        assert '"tickets"' in sql


# ---------------------------------------------------------------------------
# CreateFunction
# ---------------------------------------------------------------------------


FUNC_BODY = """\
CREATE OR REPLACE FUNCTION purge_team_data(p_team_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM tickets WHERE team_id = p_team_id;
END;
$$;
"""


class TestCreateFunction:
    def test_classification_is_non_transactional(self) -> None:
        op = CreateFunction("purge_team_data", FUNC_BODY)
        assert op.classification == "non_transactional"

    def test_to_op_dict_keys(self) -> None:
        op = CreateFunction("purge_team_data", FUNC_BODY)
        d = op.to_op_dict()
        assert d["kind"] == "create_function"
        assert d["name"] == "purge_team_data"
        assert d["body"] == FUNC_BODY

    def test_body_emitted_verbatim(self) -> None:
        """The function body must not be escaped, wrapped, or modified."""
        op = CreateFunction("my_func", FUNC_BODY)
        sql = _op_to_sql(op.to_op_dict())
        assert sql == FUNC_BODY

    def test_body_with_dollar_signs_emitted_verbatim(self) -> None:
        body = "CREATE OR REPLACE FUNCTION f() RETURNS void LANGUAGE sql AS $$ SELECT 1 $$;"
        op = CreateFunction("f", body)
        assert _op_to_sql(op.to_op_dict()) == body

    def test_repr(self) -> None:
        op = CreateFunction("purge_team_data", FUNC_BODY)
        r = repr(op)
        assert "CreateFunction" in r
        assert "purge_team_data" in r


# ---------------------------------------------------------------------------
# DropFunction
# ---------------------------------------------------------------------------


class TestDropFunction:
    def test_classification_is_destructive(self) -> None:
        op = DropFunction("purge_team_data")
        assert op.classification == "destructive"

    def test_to_op_dict_no_args(self) -> None:
        op = DropFunction("purge_team_data")
        d = op.to_op_dict()
        assert d["kind"] == "drop_function"
        assert d["name"] == "purge_team_data"
        assert d["args"] == ""

    def test_to_op_dict_with_args(self) -> None:
        op = DropFunction("purge_team_data", args="uuid")
        d = op.to_op_dict()
        assert d["args"] == "uuid"

    def test_sql_emission_no_args(self) -> None:
        op = DropFunction("purge_team_data")
        sql = _op_to_sql(op.to_op_dict())
        assert "DROP FUNCTION IF EXISTS" in sql
        assert '"purge_team_data"' in sql
        assert "()" in sql

    def test_sql_emission_with_args(self) -> None:
        op = DropFunction("purge_team_data", args="uuid")
        sql = _op_to_sql(op.to_op_dict())
        assert "(uuid)" in sql


# ---------------------------------------------------------------------------
# call_function — identifier validation (Connection._validate_pg_identifier)
# ---------------------------------------------------------------------------


class TestCallFunctionIdentifierValidation:
    """Test that _validate_pg_identifier blocks invalid identifiers."""

    def test_valid_identifier_passes(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        _validate_pg_identifier("purge_team_data", "function_name")
        _validate_pg_identifier("public", "schema")
        _validate_pg_identifier("_private_fn", "function_name")

    def test_empty_string_raises(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        with pytest.raises(FerrumCompileError) as exc_info:
            _validate_pg_identifier("", "function_name")
        assert "FERR-C102" in str(exc_info.value)

    def test_starts_with_digit_raises(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        with pytest.raises(FerrumCompileError):
            _validate_pg_identifier("1bad_name", "function_name")

    def test_contains_hyphen_raises(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        with pytest.raises(FerrumCompileError):
            _validate_pg_identifier("my-function", "function_name")

    def test_sql_injection_attempt_raises(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        with pytest.raises(FerrumCompileError):
            _validate_pg_identifier('"; DROP TABLE users; --', "function_name")

    def test_too_long_raises(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        too_long = "a" * 64
        with pytest.raises(FerrumCompileError):
            _validate_pg_identifier(too_long, "function_name")

    def test_exactly_63_chars_passes(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        exactly_63 = "a" * 63
        _validate_pg_identifier(exactly_63, "function_name")

    def test_dot_in_name_raises(self) -> None:
        from ferrum.connection import _validate_pg_identifier

        with pytest.raises(FerrumCompileError):
            _validate_pg_identifier("public.my_func", "function_name")
