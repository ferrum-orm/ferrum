"""Unit tests for SQL Server full-text search compilation."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.models import FullTextIndex
from ferrum.queryset import QuerySet


class MsDoc(ferrum.Model):
    id: int = 0
    body: str = ""

    class Meta:
        full_text_indexes = (FullTextIndex(fields=("body",)),)


class TestMssqlFtsCompile:
    def test_match_uses_freetext_for_plain(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs: QuerySet[MsDoc] = MsDoc.objects.filter(body__match="hello")
        compiled = qs._compile(dialect="mssql")
        assert "FREETEXT" in compiled["sql_text"]

    def test_match_boolean_uses_contains(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = MsDoc.objects.filter(body__match_boolean='"rust" AND "orm"')
        compiled = qs._compile(dialect="mssql")
        assert "CONTAINS" in compiled["sql_text"]

    def test_rank_by_uses_containstable(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = MsDoc.objects.rank_by("body", "ferrum", mode="boolean")
        compiled = qs._compile(dialect="mssql")
        assert "CONTAINSTABLE" in compiled["sql_text"]
