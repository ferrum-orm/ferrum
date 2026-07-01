"""Unit tests for vector KNN ordering and tsvector full-text filtering."""

from __future__ import annotations

import json
from typing import Annotated

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.queryset import QuerySet, _encode_bind_value


class Doc(ferrum.Model):
    id: int = 0
    embedding: Annotated[ferrum.Vector, ferrum.Field(vector_dimensions=3)]
    search_vector: ferrum.TSVector | None = None


class TestVectorBindEncoding:
    def test_float_list_encodes_as_float_array(self) -> None:
        encoded = _encode_bind_value([0.1, 0.2, 0.3])
        assert encoded == {"type": "float_array", "value": [0.1, 0.2, 0.3]}

    def test_int_list_encodes_as_int_array(self) -> None:
        # A list of Python ints maps to int_array (INTEGER[] column filter).
        # Vector nearest-to queries should use float lists, e.g. [1.0, 2.0, 3.0].
        encoded = _encode_bind_value([1, 2, 3])
        assert encoded == {"type": "int_array", "value": [1, 2, 3]}


class TestNearestToIr:
    def test_nearest_to_adds_vector_order_by(self) -> None:
        qs = Doc.objects.nearest_to("embedding", [0.1, 0.2, 0.3], metric="l2")
        ir = json.loads(qs.to_ir_json())
        assert "vector_order_by" in ir
        vob = ir["vector_order_by"]
        assert vob["field"]["name"] == "embedding"
        assert vob["metric"] == "l2"
        assert vob["value"]["type"] == "float_array"

    def test_nearest_to_non_vector_field_raises(self) -> None:
        with pytest.raises(FerrumCompileError, match="vector field"):
            Doc.objects.nearest_to("id", [1.0])

    def test_nearest_to_unknown_field_raises(self) -> None:
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            Doc.objects.nearest_to("missing", [1.0])


class TestTsvectorFilterIr:
    def test_match_operator_in_ir(self) -> None:
        qs: QuerySet[Doc] = Doc.objects.filter(search_vector__match="python orm")
        ir = json.loads(qs.to_ir_json())
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "match"
        assert flt["value"]["type"] == "text"
        assert flt["value"]["value"] == "python orm"

    def test_match_phrase_operator_in_ir(self) -> None:
        qs = Doc.objects.filter(search_vector__match_phrase="exact phrase")
        ir = json.loads(qs.to_ir_json())
        assert ir["predicate"]["filter"]["operator"] == "match_phrase"

    def test_match_websearch_operator_in_ir(self) -> None:
        qs = Doc.objects.filter(search_vector__match_websearch='"rust" -python')
        ir = json.loads(qs.to_ir_json())
        assert ir["predicate"]["filter"]["operator"] == "match_websearch"

    def test_match_boolean_operator_in_ir(self) -> None:
        qs = Doc.objects.filter(search_vector__match_boolean="rust & postgres")
        ir = json.loads(qs.to_ir_json())
        assert ir["predicate"]["filter"]["operator"] == "match_boolean"


class TestTextRankByIr:
    def test_rank_by_adds_text_rank_by(self) -> None:
        qs = Doc.objects.rank_by("search_vector", "orm", mode="phrase")
        ir = json.loads(qs.to_ir_json())
        assert ir["version"] == 3
        trb = ir["text_rank_by"]
        assert trb["field"]["name"] == "search_vector"
        assert trb["mode"] == "phrase"
        assert trb["query"]["value"] == "orm"

    def test_search_sets_filter_and_rank(self) -> None:
        qs = Doc.objects.search("hello world", field="search_vector", mode="websearch")
        ir = json.loads(qs.to_ir_json())
        assert ir["predicate"]["filter"]["operator"] == "match_websearch"
        assert ir["text_rank_by"]["mode"] == "websearch"

    def test_rank_by_non_tsvector_raises(self) -> None:
        with pytest.raises(FerrumCompileError, match="full-text field"):
            Doc.objects.rank_by("id", "x")


class TestVectorFilterCompile:
    def test_nearest_to_compiles_to_order_by_distance(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = Doc.objects.nearest_to("embedding", [0.0, 0.0, 1.0], metric="cosine").limit(5)
        compiled = qs._compile()
        sql = compiled["sql_text"]
        assert "ORDER BY" in sql
        assert "<=>" in sql
        assert "LIMIT $2" in sql or "LIMIT $1" in sql

    def test_match_compiles_to_plainto_tsquery(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = Doc.objects.filter(search_vector__match="rust postgres")
        compiled = qs._compile()
        sql = compiled["sql_text"]
        assert "@@ plainto_tsquery" in sql
        assert "rust postgres" not in sql

    def test_match_phrase_compiles_to_phraseto_tsquery(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = Doc.objects.filter(search_vector__match_phrase="exact phrase")
        compiled = qs._compile()
        assert "@@ phraseto_tsquery" in compiled["sql_text"]

    def test_rank_by_compiles_to_ts_rank(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = Doc.objects.rank_by("search_vector", "orm")
        compiled = qs._compile(dialect="postgres")
        assert "ORDER BY" in compiled["sql_text"]
        assert "ts_rank" in compiled["sql_text"]

    @pytest.mark.parametrize(
        ("dialect", "needles"),
        [
            ("mysql", ("MATCH", "NATURAL LANGUAGE MODE")),
            ("sqlite", ("MATCH", "rowid IN")),
            ("mssql", ("FREETEXT",)),
        ],
    )
    def test_match_non_postgres_dialect_dispatch(
        self, dialect: str, needles: tuple[str, ...]
    ) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = Doc.objects.filter(search_vector__match="hello")
        compiled = qs._compile(dialect=dialect)
        sql = compiled["sql_text"]
        for needle in needles:
            assert needle in sql
