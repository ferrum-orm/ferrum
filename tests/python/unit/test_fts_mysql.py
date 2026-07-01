"""Unit tests for MySQL full-text search compilation."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.models import FullTextIndex
from ferrum.queryset import QuerySet


class MyDoc(ferrum.Model):
    id: int = 0
    title: str = ""
    body: str = ""

    class Meta:
        full_text_indexes = (FullTextIndex(fields=("title", "body")),)


class TestMysqlFtsCompile:
    def test_match_uses_natural_language_mode(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs: QuerySet[MyDoc] = MyDoc.objects.filter(title__match="hello world")
        compiled = qs._compile(dialect="mysql")
        sql = compiled["sql_text"]
        assert "MATCH" in sql
        assert "NATURAL LANGUAGE MODE" in sql
        assert "hello world" not in sql

    def test_boolean_mode(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = MyDoc.objects.filter(body__match_boolean="+rust -python")
        compiled = qs._compile(dialect="mysql")
        assert "BOOLEAN MODE" in compiled["sql_text"]

    def test_rank_by_orders_by_match(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = MyDoc.objects.rank_by("title", "ferrum", mode="plain")
        compiled = qs._compile(dialect="mysql")
        assert "MATCH" in compiled["sql_text"]
        assert "ORDER BY" in compiled["sql_text"]
