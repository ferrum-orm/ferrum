"""Unit tests for SQLite FTS5 compilation."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.models import FullTextIndex
from ferrum.queryset import QuerySet


class SqliteDoc(ferrum.Model):
    id: int = 0
    content: str = ""

    class Meta:
        table = "sqlite_docs"
        full_text_indexes = (FullTextIndex(fields=("content",), name="sqlite_docs_content_fts"),)


class TestSqliteFtsCompile:
    def test_match_uses_fts5_subquery(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs: QuerySet[SqliteDoc] = SqliteDoc.objects.filter(content__match="hello")
        compiled = qs._compile(dialect="sqlite")
        sql = compiled["sql_text"]
        assert "MATCH" in sql
        assert "sqlite_docs_content_fts" in sql
        assert "rowid" in sql

    def test_rank_by_uses_bm25(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = SqliteDoc.objects.rank_by("content", "world", mode="plain")
        compiled = qs._compile(dialect="sqlite")
        assert "bm25" in compiled["sql_text"]
