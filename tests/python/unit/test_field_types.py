"""Unit tests for FieldMeta.sql_type, Field() factory, _col_def allowlist, and compute_plan.

Invariants covered:
- _field_type_to_sql produces correct PostgreSQL DDL types for all supported field types.
- Field() factory correctly stores max_length, db_column, unique, db_index, db_default extras.
- _col_def allowlist rejects unknown SQL type bases before interpolation into DDL.
- compute_plan emits add_index ops for fields with db_index=True.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import pytest

import ferrum
from ferrum.errors import FerrumMigrationError
from ferrum.migrations.orchestrator import _col_def, compute_plan
from ferrum.models import FieldMeta, _field_type_to_sql

# ---------------------------------------------------------------------------
# Helper: build a minimal FieldMeta for sql_type tests
# ---------------------------------------------------------------------------


def _make_field(
    field_type: str,
    *,
    pk: bool = False,
    nullable: bool = False,
    max_length: int | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
) -> FieldMeta:
    return FieldMeta(
        name="x",
        column_name="x",
        python_type_name="str",
        field_type=field_type,
        allowed_operators=("eq",),
        nullable=nullable,
        pk=pk,
        max_length=max_length,
        max_digits=max_digits,
        decimal_places=decimal_places,
    )


# ---------------------------------------------------------------------------
# FieldMeta.sql_type / _field_type_to_sql
# ---------------------------------------------------------------------------


class TestFieldTypeToSql:
    def test_str_no_max_length_returns_text(self) -> None:
        assert _make_field("text").sql_type == "TEXT"

    def test_str_with_max_length_100_returns_varchar_100(self) -> None:
        assert _make_field("text", max_length=100).sql_type == "VARCHAR(100)"

    def test_str_with_max_length_50_returns_varchar_50(self) -> None:
        assert _make_field("text", max_length=50).sql_type == "VARCHAR(50)"

    def test_float_returns_real(self) -> None:
        assert _make_field("float").sql_type == "REAL"

    def test_decimal_no_extras_returns_numeric(self) -> None:
        assert _make_field("decimal").sql_type == "NUMERIC"

    def test_decimal_with_precision_returns_numeric_parameterised(self) -> None:
        assert _make_field("decimal", max_digits=10, decimal_places=2).sql_type == "NUMERIC(10,2)"

    def test_int_non_pk_returns_integer(self) -> None:
        assert _make_field("int", pk=False).sql_type == "INTEGER"

    def test_int_pk_big_int_returns_bigserial(self) -> None:
        """int PK columns are promoted to big_int; pk=True variant emits BIGSERIAL."""
        assert _make_field("big_int", pk=True).sql_type == "BIGSERIAL"

    def test_int_non_pk_big_int_returns_bigint(self) -> None:
        """Non-PK big_int (e.g. explicit primary_key=False on an int field) emits BIGINT."""
        assert _make_field("big_int", pk=False).sql_type == "BIGINT"

    def test_bool_returns_boolean(self) -> None:
        assert _make_field("bool").sql_type == "BOOLEAN"

    def test_datetime_returns_timestamptz(self) -> None:
        assert _make_field("datetime").sql_type == "TIMESTAMPTZ"

    def test_uuid_returns_uuid(self) -> None:
        assert _make_field("uuid").sql_type == "UUID"

    def test_bytes_returns_bytea(self) -> None:
        assert _make_field("bytes").sql_type == "BYTEA"

    def test_dict_json_returns_jsonb(self) -> None:
        assert _make_field("json").sql_type == "JSONB"

    def test_sql_type_property_delegates_to_field_type_to_sql(self) -> None:
        """FieldMeta.sql_type is a property that calls _field_type_to_sql — verify parity."""
        field = _make_field("text", max_length=255)
        assert field.sql_type == _field_type_to_sql(field)


# ---------------------------------------------------------------------------
# Field() factory + _build_metadata() integration
# ---------------------------------------------------------------------------


class TestFieldFactoryIntegration:
    def test_annotated_str_max_length_sets_sql_type_and_meta(self) -> None:
        class FtArticle(ferrum.Model):
            id: int
            name: Annotated[str, ferrum.Field(max_length=100)]

        field_meta = next(f for f in FtArticle.get_metadata().fields if f.name == "name")
        assert field_meta.max_length == 100
        assert field_meta.sql_type == "VARCHAR(100)"

    def test_annotated_str_max_length_50(self) -> None:
        class FtShortName(ferrum.Model):
            id: int
            code: Annotated[str, ferrum.Field(max_length=50)]

        field_meta = next(f for f in FtShortName.get_metadata().fields if f.name == "code")
        assert field_meta.max_length == 50
        assert field_meta.sql_type == "VARCHAR(50)"

    def test_db_column_override(self) -> None:
        class FtColOverride(ferrum.Model):
            id: int
            email: Annotated[str, ferrum.Field(db_column="email_address")]

        field_meta = next(f for f in FtColOverride.get_metadata().fields if f.name == "email")
        assert field_meta.column_name == "email_address"

    def test_unique_flag(self) -> None:
        class FtUnique(ferrum.Model):
            id: int
            code: Annotated[str, ferrum.Field(unique=True)]

        field_meta = next(f for f in FtUnique.get_metadata().fields if f.name == "code")
        assert field_meta.unique is True

    def test_db_index_flag(self) -> None:
        class FtIndexed(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

        field_meta = next(f for f in FtIndexed.get_metadata().fields if f.name == "slug")
        assert field_meta.db_index is True

    def test_db_default_string_stored_in_field_meta(self) -> None:
        """Field(default='NOW()') stores db_default; Python-side default is None so
        the model can be constructed without supplying the field.
        """

        class FtDbDefault(ferrum.Model):
            id: int
            created_at: str | None = ferrum.Field(default="NOW()")

        field_meta = next(f for f in FtDbDefault.get_metadata().fields if f.name == "created_at")
        assert field_meta.db_default == "NOW()"
        # Python-side default is None — construction without the field must not raise.
        instance = FtDbDefault(id=1)
        assert instance.created_at is None

    def test_annotated_decimal_with_precision(self) -> None:
        class FtDecimal(ferrum.Model):
            id: int
            price: Annotated[Decimal, ferrum.Field(max_digits=10, decimal_places=2)]

        field_meta = next(f for f in FtDecimal.get_metadata().fields if f.name == "price")
        assert field_meta.max_digits == 10
        assert field_meta.decimal_places == 2
        assert field_meta.sql_type == "NUMERIC(10,2)"

    def test_field_without_extras_has_none_constraints(self) -> None:
        """A plain str field has no max_length, unique, db_index, or db_default."""

        class FtPlain(ferrum.Model):
            id: int
            title: str = ""

        field_meta = next(f for f in FtPlain.get_metadata().fields if f.name == "title")
        assert field_meta.max_length is None
        assert field_meta.unique is False
        assert field_meta.db_index is False
        assert field_meta.db_default is None


# ---------------------------------------------------------------------------
# _col_def allowlist — parameterised types and injection prevention
# ---------------------------------------------------------------------------


class TestColDefAllowlist:
    def test_varchar_not_null_produces_correct_fragment(self) -> None:
        result = _col_def({"name": "x", "sql_type": "VARCHAR(100)", "not_null": True})
        assert "VARCHAR(100)" in result
        assert "NOT NULL" in result

    def test_numeric_parameterised_in_output(self) -> None:
        result = _col_def({"name": "x", "sql_type": "NUMERIC(10,2)"})
        assert "NUMERIC(10,2)" in result

    def test_unique_flag_in_output(self) -> None:
        result = _col_def({"name": "x", "sql_type": "TEXT", "unique": True})
        assert "UNIQUE" in result

    def test_bad_base_type_raises_migration_error(self) -> None:
        """A sql_type whose base token is not in the allowlist raises FerrumMigrationError.

        This is the injection-prevention gate: _col_def strips the '(...)' suffix and
        checks only the base token, so 'INJECTED; DROP TABLE' (no parens) is caught here.
        """
        with pytest.raises(FerrumMigrationError, match="FERR-M001"):
            _col_def({"name": "x", "sql_type": "INJECTED; DROP TABLE"})

    def test_completely_unknown_type_raises(self) -> None:
        with pytest.raises(FerrumMigrationError, match="Unsupported SQL type"):
            _col_def({"name": "x", "sql_type": "EVIL_TYPE"})

    def test_varchar_base_is_in_allowlist(self) -> None:
        """VARCHAR is in the allowlist — a well-formed VARCHAR(n) must not raise."""
        result = _col_def({"name": "y", "sql_type": "VARCHAR(50)"})
        assert '"y" VARCHAR(50)' in result

    def test_numeric_base_is_in_allowlist(self) -> None:
        """NUMERIC is in the allowlist — a parameterised NUMERIC(p,s) must not raise."""
        result = _col_def({"name": "z", "sql_type": "NUMERIC(5,2)"})
        assert "NUMERIC(5,2)" in result

    def test_col_def_identifier_is_double_quoted(self) -> None:
        result = _col_def({"name": "my_col", "sql_type": "TEXT"})
        assert '"my_col"' in result


# ---------------------------------------------------------------------------
# compute_plan: AddIndex emission for db_index=True fields
# ---------------------------------------------------------------------------


class TestComputePlanAddIndex:
    def test_db_index_field_emits_add_index_op_on_new_table(self) -> None:
        class CpIndexed(ferrum.Model):
            id: int
            email: Annotated[str, ferrum.Field(db_index=True)]

        plan = compute_plan([CpIndexed], existing_tables={})
        ops = plan["ops"]
        index_ops = [op for op in ops if op["kind"] == "add_index"]
        assert len(index_ops) == 1
        op = index_ops[0]
        assert op["table"] == "cp_indexed"
        assert op["name"] == "idx_cp_indexed_email"
        assert op["columns"] == ["email"]

    def test_add_index_op_is_not_unique(self) -> None:
        """db_index=True emits a non-unique index.

        Unique indexes use the unique= flag separately.
        """

        class CpNonUniq(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

        plan = compute_plan([CpNonUniq], existing_tables={})
        index_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert len(index_ops) == 1
        assert index_ops[0]["unique"] is False

    def test_create_table_precedes_add_index(self) -> None:
        """AddIndex ops are emitted immediately after the CreateTable op, not before it."""

        class CpOrdering(ferrum.Model):
            id: int
            code: Annotated[str, ferrum.Field(db_index=True)]

        plan = compute_plan([CpOrdering], existing_tables={})
        kinds = [op["kind"] for op in plan["ops"]]
        assert kinds[0] == "create_table"
        assert "add_index" in kinds
        assert kinds.index("create_table") < kinds.index("add_index")

    def test_no_db_index_field_no_add_index_op(self) -> None:
        """Fields without db_index=True must not produce any add_index ops."""

        class CpNoIndex(ferrum.Model):
            id: int
            name: str

        plan = compute_plan([CpNoIndex], existing_tables={})
        index_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert index_ops == []

    def test_add_index_emitted_for_new_column_on_existing_table(self) -> None:
        """When a db_index field is absent from an existing table, both add_column and
        add_index ops are emitted — ensuring index creation follows column creation.
        """

        class CpExisting(ferrum.Model):
            id: int
            email: Annotated[str, ferrum.Field(db_index=True)]

        plan = compute_plan([CpExisting], existing_tables={"cp_existing": ["id"]})
        ops = plan["ops"]
        add_col_ops = [op for op in ops if op["kind"] == "add_column"]
        index_ops = [op for op in ops if op["kind"] == "add_index"]
        assert len(add_col_ops) == 1
        assert add_col_ops[0]["name"] == "email"
        assert len(index_ops) == 1
        assert index_ops[0]["name"] == "idx_cp_existing_email"

    def test_multiple_indexed_fields_emit_one_index_each(self) -> None:
        class CpMultiIdx(ferrum.Model):
            id: int
            email: Annotated[str, ferrum.Field(db_index=True)]
            slug: Annotated[str, ferrum.Field(db_index=True)]

        plan = compute_plan([CpMultiIdx], existing_tables={})
        index_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert len(index_ops) == 2
        names = {op["name"] for op in index_ops}
        assert "idx_cp_multi_idx_email" in names
        assert "idx_cp_multi_idx_slug" in names
