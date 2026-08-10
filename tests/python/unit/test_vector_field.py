"""Unit tests for Vector and TSVector field types."""

from __future__ import annotations

from typing import Annotated

import pytest

import ferrum
from ferrum.migrations.orchestrator import _col_def, compute_plan


class TestVectorFieldMetadata:
    def test_vector_sql_type_includes_dimensions(self) -> None:
        class Doc(ferrum.Model):
            id: int
            embedding: Annotated[ferrum.Vector, ferrum.Field(vector_dimensions=1536)]

        field = next(f for f in Doc.get_metadata().fields if f.name == "embedding")
        assert field.field_type == "vector"
        assert field.vector_dimensions == 1536
        assert field.sql_type == "VECTOR(1536)"

    def test_tsvector_sql_type(self) -> None:
        class Doc(ferrum.Model):
            id: int
            search_vector: ferrum.TSVector

        field = next(f for f in Doc.get_metadata().fields if f.name == "search_vector")
        assert field.field_type == "tsvector"
        assert field.sql_type == "TSVECTOR"

    def test_vector_without_dimensions_raises(self) -> None:
        with pytest.raises(ValueError, match="vector_dimensions"):

            class BadVector(ferrum.Model):
                id: int
                embedding: ferrum.Vector

    def test_metadata_json_includes_vector_dimensions(self) -> None:
        class Doc(ferrum.Model):
            id: int
            embedding: Annotated[ferrum.Vector, ferrum.Field(vector_dimensions=3)]

        import json

        payload = json.loads(Doc.get_metadata().to_metadata_json())
        emb = next(f for f in payload["fields"] if f["name"] == "embedding")
        assert emb["field_type"] == "vector"
        assert emb["vector_dimensions"] == 3


class TestVectorBindEncoding:
    """Vector writes bind a pgvector text literal, never a raw ``list[float]``.

    asyncpg has no codec for the pgvector ``vector`` type and falls back to
    text, so a bound list raises ``DataError`` ("expected str, got list").
    """

    @staticmethod
    def _model() -> type[ferrum.Model]:
        class Doc(ferrum.Model):
            id: Annotated[int, ferrum.Field(primary_key=True)] = 0
            embedding: Annotated[
                ferrum.Vector | None,
                ferrum.Field(vector_dimensions=3),
            ] = None

        return Doc

    def test_insert_ir_binds_vector_as_text_literal(self) -> None:
        doc = self._model()
        ir = doc.objects._build_insert_ir({"id": 1, "embedding": [0.1, 0.2, 0.3]})
        bind = next(v[1] for v in ir["operation"]["values"] if v[0]["name"] == "embedding")
        assert bind == {"type": "text", "value": "[0.1,0.2,0.3]"}

    def test_update_ir_binds_vector_as_text_literal(self) -> None:
        doc = self._model()
        ir = doc.objects.filter(id=1)._build_update_ir({"embedding": [0.1, 0.2, 0.3]})
        bind = ir["operation"]["assignments"][0][1]
        assert bind == {"type": "text", "value": "[0.1,0.2,0.3]"}

    def test_bulk_update_ir_binds_vector_as_text_literal(self) -> None:
        doc = self._model()
        ir = doc.objects._build_bulk_update_ir(
            [(1, {"embedding": [0.1, 0.2, 0.3]})],
            ["embedding"],
        )
        assert ir["operation"]["rows"][0]["values"] == [{"type": "text", "value": "[0.1,0.2,0.3]"}]

    def test_upsert_binds_vector_as_text_literal(self) -> None:
        doc = self._model()
        _sql, bound = doc.objects._build_upsert_sql(
            doc.get_metadata(),
            {"id": 1, "embedding": [0.1, 0.2, 0.3]},
            conflict_fields=["id"],
            update_fields=["embedding"],
            returning=False,
        )
        assert bound == [1, "[0.1,0.2,0.3]"]

    def test_int_elements_encode_as_floats(self) -> None:
        doc = self._model()
        ir = doc.objects._build_insert_ir({"id": 1, "embedding": (1, 0, 0)})
        bind = next(v[1] for v in ir["operation"]["values"] if v[0]["name"] == "embedding")
        assert bind == {"type": "text", "value": "[1.0,0.0,0.0]"}

    def test_none_stays_null(self) -> None:
        doc = self._model()
        ir = doc.objects._build_insert_ir({"id": 1, "embedding": None})
        bind = next(v[1] for v in ir["operation"]["values"] if v[0]["name"] == "embedding")
        assert bind == {"type": "null"}

    def test_pre_encoded_literal_is_not_re_encoded(self) -> None:
        doc = self._model()
        ir = doc.objects._build_insert_ir({"id": 1, "embedding": "[0.1,0.2,0.3]"})
        bind = next(v[1] for v in ir["operation"]["values"] if v[0]["name"] == "embedding")
        assert bind == {"type": "text", "value": "[0.1,0.2,0.3]"}


class TestVectorMigrationDdl:
    def test_compute_plan_emits_vector_column(self) -> None:
        class Doc(ferrum.Model):
            id: int
            embedding: Annotated[ferrum.Vector, ferrum.Field(vector_dimensions=8)]

        plan = compute_plan([Doc], existing_tables={})
        create_op = next(op for op in plan["ops"] if op["kind"] == "create_table")
        col = next(c for c in create_op["columns"] if c["name"] == "embedding")
        assert col["sql_type"] == "VECTOR(8)"

    def test_col_def_accepts_vector_type(self) -> None:
        result = _col_def({"name": "embedding", "sql_type": "VECTOR(1536)"})
        assert "VECTOR(1536)" in result

    def test_col_def_accepts_tsvector_type(self) -> None:
        result = _col_def({"name": "search_vector", "sql_type": "TSVECTOR"})
        assert "TSVECTOR" in result
