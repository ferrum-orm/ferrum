"""Unit tests for PostgreSQL full-text search compilation."""

from __future__ import annotations

from typing import Annotated

import pytest

import ferrum
from ferrum.models import Field


class PgDoc(ferrum.Model):
    id: int = 0
    search_vector: Annotated[ferrum.TSVector | None, Field(fts_config="english")] = None


class TestPostgresFtsCompile:
    @pytest.mark.parametrize(
        ("lookup", "needle"),
        [
            ("match", "plainto_tsquery"),
            ("match_phrase", "phraseto_tsquery"),
            ("match_websearch", "websearch_to_tsquery"),
            ("match_boolean", "to_tsquery"),
        ],
    )
    def test_match_modes_use_bound_tsquery(self, lookup: str, needle: str) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = PgDoc.objects.filter(**{f"search_vector__{lookup}": "rust orm"})
        compiled = qs._compile(dialect="postgres")
        sql = compiled["sql_text"]
        assert needle in sql
        assert "'english'" in sql or "english" in sql
        assert "rust orm" not in sql

    def test_rank_by_uses_ts_rank(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = PgDoc.objects.rank_by("search_vector", "python", mode="plain").limit(5)
        compiled = qs._compile(dialect="postgres")
        assert "ts_rank" in compiled["sql_text"]
        assert "ORDER BY" in compiled["sql_text"]

    def test_search_sets_filter_and_rank(self) -> None:
        ir = PgDoc.objects.search("hello", field="search_vector", mode="websearch").to_ir_json()
        import json

        parsed = json.loads(ir)
        assert parsed["version"] == 3
        assert "text_rank_by" in parsed
        assert parsed["text_rank_by"]["mode"] == "websearch"
