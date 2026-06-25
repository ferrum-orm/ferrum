"""Unit tests for PostgreSQL array/enum types and richer JSONB operators."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

import ferrum
from ferrum.migrations.orchestrator import compute_plan
from ferrum.models import ModelMetadata
from ferrum.queryset import _decode_bound_param, _encode_bind_value


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
