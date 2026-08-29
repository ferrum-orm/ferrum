"""Unit tests for PostgreSQL array/enum types and richer JSONB operators.

W1-A also includes the table-driven cast matrix that verifies the Rust
``postgres_value_cast`` output matches the migration DDL type for every
``FieldType``, so ``BulkUpdate`` VALUES casts never fail live PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from uuid import UUID

import pytest

import ferrum
from ferrum.migrations.orchestrator import compute_plan
from ferrum.models import FieldMeta, ModelMetadata
from ferrum.queryset import QuerySet, _decode_bound_param, _encode_bind_value


class ArrayModel(ferrum.Model):
    id: int = 0
    tags: list[str] = ferrum.Field(default_factory=list)
    scores: list[int] = ferrum.Field(default_factory=list)
    item_ids: list[UUID] = ferrum.Field(default_factory=list)
    weights: list[float] = ferrum.Field(default_factory=list)
    meta: dict = ferrum.Field(default_factory=dict)


class EnumModel(ferrum.Model):
    id: int = 0
    status: Literal["active", "inactive", "pending"] = "active"


# ---------------------------------------------------------------------------
# Array field metadata
# ---------------------------------------------------------------------------


class TestArrayFieldMetadata:
    def test_list_str_maps_to_array_text(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        tags_field = next(f for f in meta.fields if f.name == "tags")
        assert tags_field.field_type == "array_text"

    def test_list_int_maps_to_array_int(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        scores_field = next(f for f in meta.fields if f.name == "scores")
        assert scores_field.field_type == "array_int"

    def test_list_uuid_maps_to_array_uuid(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        ids_field = next(f for f in meta.fields if f.name == "item_ids")
        assert ids_field.field_type == "array_uuid"

    def test_list_float_maps_to_array_float(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        weights_field = next(f for f in meta.fields if f.name == "weights")
        assert weights_field.field_type == "array_float"


# ---------------------------------------------------------------------------
# DDL type emission
# ---------------------------------------------------------------------------


class TestArrayDdlTypes:
    def _get_column(self, model_class: type, col_name: str) -> dict:
        plan = compute_plan([model_class], {})
        create_op = plan["ops"][0]
        return next(c for c in create_op["columns"] if c["name"] == col_name)

    def test_array_text_ddl(self) -> None:
        col = self._get_column(ArrayModel, "tags")
        assert col["sql_type"] == "TEXT[]"

    def test_array_int_ddl(self) -> None:
        col = self._get_column(ArrayModel, "scores")
        assert col["sql_type"] == "INTEGER[]"

    def test_array_uuid_ddl(self) -> None:
        col = self._get_column(ArrayModel, "item_ids")
        assert col["sql_type"] == "UUID[]"

    def test_array_float_ddl(self) -> None:
        col = self._get_column(ArrayModel, "weights")
        assert col["sql_type"] == "FLOAT8[]"


# ---------------------------------------------------------------------------
# Enum field metadata and DDL
# ---------------------------------------------------------------------------


class TestEnumField:
    def test_literal_maps_to_enum_type(self) -> None:
        meta: ModelMetadata = EnumModel.__ferrum_metadata__
        status_field = next(f for f in meta.fields if f.name == "status")
        assert status_field.field_type == "enum"

    def test_enum_ddl_is_text(self) -> None:
        plan = compute_plan([EnumModel], {})
        create_op = plan["ops"][0]
        status_col = next(c for c in create_op["columns"] if c["name"] == "status")
        assert status_col["sql_type"] == "TEXT"


# ---------------------------------------------------------------------------
# Bind-value encoding for array types
# ---------------------------------------------------------------------------


class TestArrayBindEncoding:
    def test_str_list_encodes_as_text_array(self) -> None:
        encoded = _encode_bind_value(["a", "b", "c"])
        assert encoded == {"type": "text_array", "value": ["a", "b", "c"]}

    def test_int_list_encodes_as_int_array(self) -> None:
        encoded = _encode_bind_value([1, 2, 3])
        assert encoded == {"type": "int_array", "value": [1, 2, 3]}

    def test_uuid_list_encodes_as_text_array(self) -> None:
        u = UUID("12345678-1234-5678-1234-567812345678")
        encoded = _encode_bind_value([u])
        assert encoded["type"] == "text_array"
        assert encoded["value"] == [str(u)]

    def test_empty_list_encodes_as_text_array(self) -> None:
        encoded = _encode_bind_value([])
        assert encoded == {"type": "text_array", "value": []}


class TestArrayBindDecoding:
    def test_decode_text_array(self) -> None:
        encoded = json.dumps({"type": "text_array", "value": ["a", "b"]})
        assert _decode_bound_param(encoded) == ["a", "b"]

    def test_decode_int_array(self) -> None:
        encoded = json.dumps({"type": "int_array", "value": [1, 2, 3]})
        assert _decode_bound_param(encoded) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Array operator allowlists (Python-layer field metadata)
# ---------------------------------------------------------------------------


class TestArrayOperatorAllowlist:
    def test_array_text_allows_contains(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        tags_field = next(f for f in meta.fields if f.name == "tags")
        assert "contains" in tags_field.allowed_operators

    def test_array_text_allows_overlap(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        tags_field = next(f for f in meta.fields if f.name == "tags")
        assert "overlap" in tags_field.allowed_operators

    def test_array_text_allows_contained_by(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        tags_field = next(f for f in meta.fields if f.name == "tags")
        assert "contained_by" in tags_field.allowed_operators

    def test_icontains_not_in_array_allowlist(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        tags_field = next(f for f in meta.fields if f.name == "tags")
        assert "icontains" not in tags_field.allowed_operators


# ---------------------------------------------------------------------------
# JSONB operator allowlists
# ---------------------------------------------------------------------------


class TestJsonbOperatorAllowlist:
    def test_json_field_allows_has_key(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        meta_field = next(f for f in meta.fields if f.name == "meta")
        assert "has_key" in meta_field.allowed_operators

    def test_json_field_allows_has_any_keys(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        meta_field = next(f for f in meta.fields if f.name == "meta")
        assert "has_any_keys" in meta_field.allowed_operators

    def test_json_field_allows_contains(self) -> None:
        meta: ModelMetadata = ArrayModel.__ferrum_metadata__
        meta_field = next(f for f in meta.fields if f.name == "meta")
        assert "contains" in meta_field.allowed_operators


# ---------------------------------------------------------------------------
# W1-A: Cast matrix — Rust postgres_value_cast must match migration DDL
# ---------------------------------------------------------------------------


class _CastMatrixModel(ferrum.Model):
    """Model exercising every FieldType that has a DDL type and a Rust cast.

    ``id`` is an ``int`` PK → auto-upgraded to ``big_int`` (DDL ``BIGSERIAL``).
    ``count`` is a non-PK ``int`` (DDL ``INTEGER``) — the critical cast that
    previously emitted ``bigint`` instead of ``integer``.
    """

    id: int = 0
    count: int = 0
    score: float = 0.0
    amount: Decimal = Decimal("0")
    label: str = ""
    active: bool = True
    created_at: datetime = datetime(2000, 1, 1)
    day: date = date(2000, 1, 1)
    clock: time = time(0, 0, 0)
    uid: UUID = UUID("00000000-0000-0000-0000-000000000000")
    payload: dict = ferrum.Field(default_factory=dict)
    raw: bytes = b""
    tags: list[str] = ferrum.Field(default_factory=list)
    scores_list: list[int] = ferrum.Field(default_factory=list)
    uids: list[UUID] = ferrum.Field(default_factory=list)
    weights: list[float] = ferrum.Field(default_factory=list)
    status: Literal["active", "inactive"] = "active"
    search: ferrum.TSVector | None = None
    embedding: ferrum.Vector = ferrum.Field(
        default_factory=lambda: [0.0, 0.0, 0.0], vector_dimensions=3
    )


class TestCastMatrixMatchesDdl:
    """The Rust ``postgres_value_cast`` (used by ``BulkUpdate`` VALUES) must
    produce a PostgreSQL type cast that matches the migration DDL type for
    every ``FieldType``. A mismatch causes ``SQLSTATE 42883`` on the
    ``t.pk = v.pk`` join predicate (no implicit cast) or precision loss on
    the SET assignment.
    """

    @staticmethod
    def _ddl_type(field_name: str) -> str:
        meta: ModelMetadata = _CastMatrixModel.__ferrum_metadata__
        field = next(f for f in meta.fields if f.name == field_name)
        return field.sql_type

    @staticmethod
    def _bulk_update_cast(field_name: str) -> str:
        """Compile a BulkUpdate IR and extract the ``$N::<cast>`` for the field."""
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = QuerySet(_CastMatrixModel)
        row = _CastMatrixModel()
        ir = qs._build_bulk_update_ir(
            [(row.id, {field_name: getattr(row, field_name)})], (field_name,)
        )
        compiled = qs._compile_ir(ir, dialect="postgres")
        sql = compiled["sql_text"]
        import re

        # The update field cast is the second placeholder (after the PK placeholder).
        matches = re.findall(r"\$\d+::(\w+(?:\[\])?)", sql)
        assert len(matches) >= 2, f"Expected at least 2 casts in BulkUpdate SQL: {sql}"
        return matches[1]  # matches[0] is the PK cast, matches[1] is the update field cast.

    @staticmethod
    def _normalize_ddl(ddl: str) -> str:
        """Normalize DDL type to the Rust cast token for comparison."""
        upper = ddl.upper()
        mapping = {
            "INTEGER": "integer",
            "BIGINT": "bigint",
            "BIGSERIAL": "bigint",
            "REAL": "real",
            "NUMERIC": "numeric",
            "TEXT": "text",
            "VARCHAR": "text",
            "BOOLEAN": "boolean",
            "TIMESTAMPTZ": "timestamptz",
            "DATE": "date",
            "TIME": "time",
            "UUID": "uuid",
            "JSONB": "jsonb",
            "BYTEA": "bytea",
            "TSVECTOR": "tsvector",
            "TEXT[]": "text[]",
            "INTEGER[]": "integer[]",
            "UUID[]": "uuid[]",
            "FLOAT8[]": "float8[]",
            "VECTOR": "vector",
        }
        base = upper.split("(")[0]
        return mapping.get(base, upper.lower())

    def test_int_pk_cast_matches_ddl(self) -> None:
        """The PK is ``big_int`` (auto-upgraded from ``int``) → cast must be ``bigint``."""
        ddl = self._normalize_ddl(self._ddl_type("id"))
        qs = QuerySet(_CastMatrixModel)
        row = _CastMatrixModel()
        ir = qs._build_bulk_update_ir([(row.id, {"count": 0})], ("count",))
        compiled = qs._compile_ir(ir, dialect="postgres")
        import re

        pk_cast = re.findall(r"\$\d+::(\w+)", compiled["sql_text"])[0]
        assert ddl == pk_cast, f"PK (big_int): DDL={ddl}, cast={pk_cast}"

    def test_int_cast_matches_ddl(self) -> None:
        """Non-PK ``int`` → DDL ``INTEGER`` → cast must be ``integer`` (was ``bigint``)."""
        ddl = self._normalize_ddl(self._ddl_type("count"))
        cast = self._bulk_update_cast("count")
        assert ddl == cast, f"int: DDL={ddl}, cast={cast}"

    def test_float_cast_matches_ddl(self) -> None:
        """``float`` → DDL ``REAL`` → cast must be ``real`` (was ``double precision``)."""
        ddl = self._normalize_ddl(self._ddl_type("score"))
        cast = self._bulk_update_cast("score")
        assert ddl == cast, f"float: DDL={ddl}, cast={cast}"

    def test_decimal_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("amount"))
        cast = self._bulk_update_cast("amount")
        assert ddl == cast, f"decimal: DDL={ddl}, cast={cast}"

    def test_text_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("label"))
        cast = self._bulk_update_cast("label")
        assert ddl == cast, f"text: DDL={ddl}, cast={cast}"

    def test_bool_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("active"))
        cast = self._bulk_update_cast("active")
        assert ddl == cast, f"bool: DDL={ddl}, cast={cast}"

    def test_datetime_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("created_at"))
        cast = self._bulk_update_cast("created_at")
        assert ddl == cast, f"datetime: DDL={ddl}, cast={cast}"

    def test_date_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("day"))
        cast = self._bulk_update_cast("day")
        assert ddl == cast, f"date: DDL={ddl}, cast={cast}"

    def test_time_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("clock"))
        cast = self._bulk_update_cast("clock")
        assert ddl == cast, f"time: DDL={ddl}, cast={cast}"

    def test_uuid_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("uid"))
        cast = self._bulk_update_cast("uid")
        assert ddl == cast, f"uuid: DDL={ddl}, cast={cast}"

    def test_json_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("payload"))
        cast = self._bulk_update_cast("payload")
        assert ddl == cast, f"json: DDL={ddl}, cast={cast}"

    def test_bytes_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("raw"))
        cast = self._bulk_update_cast("raw")
        assert ddl == cast, f"bytes: DDL={ddl}, cast={cast}"

    def test_array_text_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("tags"))
        cast = self._bulk_update_cast("tags")
        assert ddl == cast, f"array_text: DDL={ddl}, cast={cast}"

    def test_array_int_cast_matches_ddl(self) -> None:
        """``array_int`` → DDL ``INTEGER[]`` → cast must be ``integer[]`` (was ``bigint[]``)."""
        ddl = self._normalize_ddl(self._ddl_type("scores_list"))
        cast = self._bulk_update_cast("scores_list")
        assert ddl == cast, f"array_int: DDL={ddl}, cast={cast}"

    def test_array_uuid_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("uids"))
        cast = self._bulk_update_cast("uids")
        assert ddl == cast, f"array_uuid: DDL={ddl}, cast={cast}"

    def test_array_float_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("weights"))
        cast = self._bulk_update_cast("weights")
        assert ddl == cast, f"array_float: DDL={ddl}, cast={cast}"

    def test_enum_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("status"))
        cast = self._bulk_update_cast("status")
        assert ddl == cast, f"enum: DDL={ddl}, cast={cast}"

    def test_tsvector_cast_matches_ddl(self) -> None:
        ddl = self._normalize_ddl(self._ddl_type("search"))
        cast = self._bulk_update_cast("search")
        assert ddl == cast, f"tsvector: DDL={ddl}, cast={cast}"

    def test_vector_cast_matches_ddl(self) -> None:
        """VECTOR(n) DDL normalizes to ``vector`` — the cast is ``$N::vector``."""
        ddl = self._normalize_ddl(self._ddl_type("embedding"))
        cast = self._bulk_update_cast("embedding")
        assert ddl == cast, f"vector: DDL={ddl}, cast={cast}"

    def test_inet_has_no_field_type(self) -> None:
        """INET is in the DDL type allowlist but has no FieldType — known gap.

        Models needing an inet column currently fall back to ``text``. Adding
        ``FieldType::Inet`` would be an IR change requiring ChiefArchitect
        escalation; it is out of scope for W1-A.
        """
        from ferrum.migrations.orchestrator import _SQL_TYPE_ALLOWLIST

        assert "INET" in _SQL_TYPE_ALLOWLIST
        # Verify no field_type string in _field_type_to_sql maps to INET.
        all_field_types = [
            "text",
            "int",
            "big_int",
            "float",
            "decimal",
            "bool",
            "datetime",
            "date",
            "time",
            "uuid",
            "bytes",
            "json",
            "tsvector",
            "array_text",
            "array_int",
            "array_uuid",
            "array_float",
            "enum",
        ]
        from ferrum.models import _field_type_to_sql

        for ft in all_field_types:
            stub = FieldMeta(
                name="t",
                column_name="t",
                python_type_name="str",
                field_type=ft,
                allowed_operators=(),
                nullable=False,
                pk=False,
            )
            assert _field_type_to_sql(stub) != "INET", (
                f"field_type {ft!r} unexpectedly maps to INET"
            )
