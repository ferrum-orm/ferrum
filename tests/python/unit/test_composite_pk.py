"""Unit tests for composite primary key support."""

from __future__ import annotations

import json
from typing import Annotated

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.migrations.orchestrator import compute_plan
from ferrum.models import ModelMetadata
from ferrum.queryset import QuerySet


class SinglePkModel(ferrum.Model):
    id: int = 0
    name: str = ""


class CompositePkModel(ferrum.Model):
    id: Annotated[int, ferrum.Field(primary_key=True)] = 0
    first_seen_at: Annotated[str, ferrum.Field(primary_key=True)] = ""
    value: str = ""


# ---------------------------------------------------------------------------
# ModelMetadata — pk_fields
# ---------------------------------------------------------------------------


class TestModelMetadataCompositePk:
    def test_single_pk_has_pk_fields_tuple_of_one(self) -> None:
        meta: ModelMetadata = SinglePkModel.__ferrum_metadata__
        assert meta.pk_fields == (0,)

    def test_single_pk_index_property_returns_first(self) -> None:
        meta: ModelMetadata = SinglePkModel.__ferrum_metadata__
        assert meta.pk_index == 0

    def test_composite_pk_fields_contains_both_indices(self) -> None:
        meta: ModelMetadata = CompositePkModel.__ferrum_metadata__
        assert len(meta.pk_fields) == 2
        pk_names = [meta.fields[i].name for i in meta.pk_fields]
        assert "id" in pk_names
        assert "first_seen_at" in pk_names

    def test_composite_pk_index_property_returns_first(self) -> None:
        meta: ModelMetadata = CompositePkModel.__ferrum_metadata__
        first_pk_name = meta.fields[meta.pk_index].name
        pk_names = [meta.fields[i].name for i in meta.pk_fields]
        assert first_pk_name == pk_names[0]

    def test_to_metadata_json_includes_pk_fields(self) -> None:
        meta: ModelMetadata = CompositePkModel.__ferrum_metadata__
        data = json.loads(meta.to_metadata_json())
        assert "pk_fields" in data
        assert len(data["pk_fields"]) == 2


# ---------------------------------------------------------------------------
# BulkUpdate IR — composite PK
# ---------------------------------------------------------------------------


class TestBulkUpdateCompositePk:
    def test_bulk_update_ir_uses_pk_fields_list(self) -> None:
        qs: QuerySet[CompositePkModel] = QuerySet(CompositePkModel)
        ir = qs._build_bulk_update_ir(
            [((1, "2024-01-01"), {"value": "x"}), ((2, "2024-01-02"), {"value": "y"})],
            ["value"],
        )
        assert ir["operation"]["kind"] == "bulk_update"
        assert len(ir["operation"]["pk_fields"]) == 2
        pk_names = [pf["name"] for pf in ir["operation"]["pk_fields"]]
        assert "id" in pk_names
        assert "first_seen_at" in pk_names

    def test_bulk_update_ir_row_has_pk_values_list(self) -> None:
        qs: QuerySet[CompositePkModel] = QuerySet(CompositePkModel)
        ir = qs._build_bulk_update_ir(
            [((1, "2024-01-01"), {"value": "x"})],
            ["value"],
        )
        row = ir["operation"]["rows"][0]
        assert "pk_values" in row
        assert len(row["pk_values"]) == 2

    def test_bulk_update_composite_pk_wrong_arity_raises(self) -> None:
        qs: QuerySet[CompositePkModel] = QuerySet(CompositePkModel)
        with pytest.raises(FerrumCompileError, match="composite PK requires"):
            qs._build_bulk_update_ir(
                [(1, {"value": "x"})],  # scalar instead of tuple
                ["value"],
            )


# ---------------------------------------------------------------------------
# BulkDelete IR — composite PK
# ---------------------------------------------------------------------------


class TestBulkDeleteCompositePk:
    def test_bulk_delete_ir_uses_pk_fields_list(self) -> None:
        qs: QuerySet[CompositePkModel] = QuerySet(CompositePkModel)
        ir = qs._build_bulk_delete_ir([(1, "2024-01-01"), (2, "2024-01-02")])
        assert ir["operation"]["kind"] == "bulk_delete"
        assert len(ir["operation"]["pk_fields"]) == 2
        assert len(ir["operation"]["ids"]) == 2

    def test_bulk_delete_ids_are_nested_lists(self) -> None:
        qs: QuerySet[CompositePkModel] = QuerySet(CompositePkModel)
        ir = qs._build_bulk_delete_ir([(1, "2024-01-01")])
        id_row = ir["operation"]["ids"][0]
        assert isinstance(id_row, list)
        assert len(id_row) == 2

    def test_bulk_delete_composite_pk_wrong_arity_raises(self) -> None:
        qs: QuerySet[CompositePkModel] = QuerySet(CompositePkModel)
        with pytest.raises(FerrumCompileError, match="composite PK requires"):
            qs._build_bulk_delete_ir([1, 2])  # scalar instead of tuples


# ---------------------------------------------------------------------------
# Migration DDL — composite PK
# ---------------------------------------------------------------------------


class TestMigrationDdlCompositePk:
    def test_compute_plan_emits_table_level_primary_key(self) -> None:
        plan = compute_plan([CompositePkModel], {})
        ops = plan["ops"]
        assert ops, "Expected at least one migration op"
        create_op = ops[0]
        assert create_op["kind"] == "create_table"
        assert "composite_pk_columns" in create_op

    def test_composite_pk_no_inline_primary_key_on_columns(self) -> None:
        plan = compute_plan([CompositePkModel], {})
        create_op = plan["ops"][0]
        for col in create_op["columns"]:
            if col["name"] in ("id", "first_seen_at"):
                assert not col.get("primary_key"), (
                    f"Column {col['name']} should not have inline PRIMARY KEY "
                    "for composite PK models"
                )

    def test_single_pk_model_emits_inline_primary_key(self) -> None:
        plan = compute_plan([SinglePkModel], {})
        create_op = plan["ops"][0]
        pk_cols = [c for c in create_op["columns"] if c.get("primary_key")]
        assert len(pk_cols) == 1, "Single-PK model should have inline PRIMARY KEY"
