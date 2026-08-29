"""Unit tests for W2-C bulk operations: composite keys, batch sizing,
parameter limits, per-row values, conflict predicates, returning.

These tests validate the bulk operation IR builders and batch-sizing helpers
without requiring a live database. The bulk operation implementations live in
``queryset.py`` (W1-A/W2-B own, complete — import only); these tests verify
the behavior from the test layer, including the parameter-limit-aware
``safe_batch_size`` helper added in W2-C.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.queryset import QuerySet
from ferrum.relations import safe_batch_size

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class Widget(ferrum.Model):
    id: int = 0
    name: str = ""
    active: bool = True


class CompositePK(ferrum.Model):
    tenant_id: Annotated[int, ferrum.Field(primary_key=True)]
    entity_id: Annotated[int, ferrum.Field(primary_key=True)]
    label: str = ""

    class Meta:
        pk_fields = (0, 1)


class UUIDComposite(ferrum.Model):
    id: Annotated[UUID, ferrum.Field(primary_key=True)]
    first_seen: Annotated[str, ferrum.Field(primary_key=True)]
    label: str = ""


# ---------------------------------------------------------------------------
# safe_batch_size — parameter-limit-aware batch sizing
# ---------------------------------------------------------------------------


class TestSafeBatchSize:
    def test_single_field_under_limit(self) -> None:
        assert safe_batch_size(1, requested=1000) == 1000

    def test_three_fields_at_default(self) -> None:
        # Widget has 3 writable fields (id, name, active) — but id may be auto
        # For bulk_insert with 3 fields: 65535 / 3 = 21845
        assert safe_batch_size(3, requested=1000) == 1000

    def test_clamps_to_limit(self) -> None:
        assert safe_batch_size(3, requested=30000) == 21845

    def test_clamps_to_one_for_huge_fields(self) -> None:
        assert safe_batch_size(100000, requested=10) == 1

    def test_zero_fields_returns_requested(self) -> None:
        assert safe_batch_size(0, requested=500) == 500

    def test_custom_max_params(self) -> None:
        assert safe_batch_size(5, max_params=100, requested=100) == 20

    def test_min_one(self) -> None:
        assert safe_batch_size(65535, requested=10) == 1


# ---------------------------------------------------------------------------
# Bulk insert IR — composite keys, per-row values, returning
# ---------------------------------------------------------------------------


class TestBulkInsertIR:
    def test_rejects_unknown_field(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            qs._build_bulk_insert_ir([{"ghost": "x"}], returning=True)

    def test_rejects_inconsistent_columns(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="same field set"):
            qs._build_bulk_insert_ir(
                [{"name": "a"}, {"name": "b", "active": False}],
                returning=True,
            )

    def test_shape_single_pk(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_insert_ir(
            [{"name": "a", "active": True}, {"name": "b", "active": False}],
            returning=True,
        )
        assert ir["operation"]["kind"] == "bulk_insert"
        assert len(ir["operation"]["rows"]) == 2
        assert ir["operation"]["returning"] is True

    def test_returning_false(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_insert_ir([{"name": "a"}], returning=False)
        assert ir["operation"]["returning"] is False

    def test_composite_pk_rows(self) -> None:
        qs: QuerySet[CompositePK] = QuerySet(CompositePK)
        ir = qs._build_bulk_insert_ir(
            [
                {"tenant_id": 1, "entity_id": 10, "label": "a"},
                {"tenant_id": 1, "entity_id": 20, "label": "b"},
            ],
            returning=True,
        )
        assert len(ir["operation"]["rows"]) == 2
        # Each row should have 3 field-value pairs
        assert len(ir["operation"]["rows"][0]) == 3

    def test_empty_rows_rejected(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="at least one row"):
            qs._build_bulk_insert_ir([], returning=True)


# ---------------------------------------------------------------------------
# Bulk update IR — composite keys
# ---------------------------------------------------------------------------


class TestBulkUpdateIR:
    def test_shape_single_pk(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_update_ir(
            [(1, {"name": "x"}), (2, {"name": "y"})],
            ["name"],
        )
        assert ir["operation"]["kind"] == "bulk_update"
        assert ir["operation"]["pk_fields"][0]["name"] == "id"
        assert len(ir["operation"]["rows"]) == 2

    def test_composite_pk(self) -> None:
        qs: QuerySet[CompositePK] = QuerySet(CompositePK)
        ir = qs._build_bulk_update_ir(
            [((1, 10), {"label": "x"}), ((1, 20), {"label": "y"})],
            ["label"],
        )
        assert ir["operation"]["kind"] == "bulk_update"
        assert len(ir["operation"]["pk_fields"]) == 2
        assert ir["operation"]["pk_fields"][0]["name"] == "tenant_id"
        assert ir["operation"]["pk_fields"][1]["name"] == "entity_id"
        assert len(ir["operation"]["rows"]) == 2
        assert len(ir["operation"]["rows"][0]["pk_values"]) == 2

    def test_composite_pk_rejects_wrong_arity(self) -> None:
        qs: QuerySet[CompositePK] = QuerySet(CompositePK)
        with pytest.raises(FerrumCompileError, match="composite PK requires"):
            qs._build_bulk_update_ir([((1,), {"label": "x"})], ["label"])

    def test_empty_fields_rejected(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="at least one field"):
            qs._build_bulk_update_ir([(1, {"name": "x"})], [])

    def test_empty_rows_rejected(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="at least one row"):
            qs._build_bulk_update_ir([], ["name"])


# ---------------------------------------------------------------------------
# Bulk delete IR — composite keys
# ---------------------------------------------------------------------------


class TestBulkDeleteIR:
    def test_shape_single_pk(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_delete_ir([1, 2, 3])
        assert ir["operation"]["kind"] == "bulk_delete"
        assert len(ir["operation"]["ids"]) == 3

    def test_composite_pk(self) -> None:
        qs: QuerySet[CompositePK] = QuerySet(CompositePK)
        ir = qs._build_bulk_delete_ir([(1, 10), (1, 20), (2, 30)])
        assert ir["operation"]["kind"] == "bulk_delete"
        assert len(ir["operation"]["pk_fields"]) == 2
        assert len(ir["operation"]["ids"]) == 3
        assert len(ir["operation"]["ids"][0]) == 2

    def test_composite_pk_rejects_scalar(self) -> None:
        qs: QuerySet[CompositePK] = QuerySet(CompositePK)
        with pytest.raises(FerrumCompileError, match="composite PK requires"):
            qs._build_bulk_delete_ir([1, 2, 3])

    def test_composite_pk_rejects_wrong_arity(self) -> None:
        qs: QuerySet[CompositePK] = QuerySet(CompositePK)
        with pytest.raises(FerrumCompileError, match="composite PK requires"):
            qs._build_bulk_delete_ir([(1, 2, 3)])

    def test_empty_ids_rejected(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="at least one id"):
            qs._build_bulk_delete_ir([])


# ---------------------------------------------------------------------------
# Upsert IR — conflict predicates, update fields
# ---------------------------------------------------------------------------


class TestUpsertValidation:
    def test_conflict_field_validated_against_metadata(self) -> None:
        """Conflict fields are validated against the metadata allowlist."""
        qs: QuerySet[Widget] = QuerySet(Widget)
        metadata = qs._get_metadata()
        assert metadata is not None
        field_names = {f.name for f in metadata.fields}
        assert "name" in field_names
        assert "active" in field_names
        assert "ghost" not in field_names

    def test_upsert_sql_builds_for_valid_conflict(self) -> None:
        """_build_upsert_sql succeeds for allowlisted conflict fields."""
        qs: QuerySet[Widget] = QuerySet(Widget)
        metadata = qs._get_metadata()
        assert metadata is not None
        sql, _bound = qs._build_upsert_sql(
            metadata,
            {"name": "x", "active": True},
            conflict_fields=["name"],
            update_fields=None,
            returning=True,
            dialect="postgres",
        )
        assert "INSERT INTO" in sql
        assert "ON CONFLICT" in sql
        assert "RETURNING" in sql

    def test_upsert_sql_do_nothing(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        metadata = qs._get_metadata()
        assert metadata is not None
        sql, _bound = qs._build_upsert_sql(
            metadata,
            {"name": "x", "active": True},
            conflict_fields=["name"],
            update_fields=[],
            returning=False,
            dialect="postgres",
        )
        assert "DO NOTHING" in sql
        assert "RETURNING" not in sql

    def test_upsert_sql_rejects_non_postgres(self) -> None:
        from ferrum.errors import FerrumConfigError

        qs: QuerySet[Widget] = QuerySet(Widget)
        metadata = qs._get_metadata()
        assert metadata is not None
        with pytest.raises(FerrumConfigError, match="not supported"):
            qs._build_upsert_sql(
                metadata,
                {"name": "x"},
                conflict_fields=["name"],
                update_fields=None,
                returning=True,
                dialect="mysql",
            )


# ---------------------------------------------------------------------------
# Batch sizing with parameter limits — integration of safe_batch_size
# ---------------------------------------------------------------------------


class TestBatchSizingIntegration:
    def test_widget_insert_safe_batch(self) -> None:
        """Widget has 3 writable fields (id, name, active).
        With 65535 max params: safe_batch_size(3) = 21845.
        A default batch_size of 1000 is well within limits.
        """
        bs = safe_batch_size(3, requested=1000)
        assert bs == 1000
        assert 3 * bs <= 65535

    def test_composite_pk_update_safe_batch(self) -> None:
        """CompositePK has 3 fields total; bulk_update with 1 update field
        uses 3 params per row (2 PK + 1 update).
        """
        params_per_row = 3  # 2 PK values + 1 assignment
        bs = safe_batch_size(params_per_row, requested=1000)
        assert bs == 1000
        assert params_per_row * bs <= 65535

    def test_large_field_count_clamped(self) -> None:
        """A model with many fields clamps batch_size to avoid param overflow."""
        params_per_row = 50
        bs = safe_batch_size(params_per_row, requested=2000)
        assert bs == 1310  # 65535 // 50 = 1310
        assert params_per_row * bs <= 65535

    def test_batch_size_at_pg_limit(self) -> None:
        """At the exact PG limit, batch size is 1."""
        bs = safe_batch_size(65535, requested=100)
        assert bs == 1
