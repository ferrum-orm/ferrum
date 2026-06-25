"""Unit tests for Phase 1 query expressiveness (Q, exclude, exists, values, distinct)."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import FerrumDeferredFieldError
from ferrum.expressions import Q
from ferrum.queryset import QuerySet


class Note(ferrum.Model):
    id: int = 0
    title: str = ""
    body: str = ""
    published: bool = False


class TestQIrLowering:
    def test_q_or_compiles_to_predicate_or(self) -> None:
        ir = QuerySet(Note).filter(Q(published=True) | Q(title="x"))._build_ir()
        assert ir["predicate"]["kind"] == "or"

    def test_q_not_compiles_to_predicate_not(self) -> None:
        ir = QuerySet(Note).filter(~Q(published=True))._build_ir()
        assert ir["predicate"]["kind"] == "not"

    def test_exclude_is_negated_filter(self) -> None:
        ir = QuerySet(Note).exclude(published=True)._build_ir()
        assert ir["predicate"]["kind"] == "not"
        assert ir["predicate"]["child"]["kind"] == "filter"

    def test_distinct_flag_on_ir(self) -> None:
        ir = QuerySet(Note).distinct()._build_ir()
        assert ir["distinct"] is True

    def test_only_limits_select_fields(self) -> None:
        ir = QuerySet(Note).only("id", "title")._build_ir()
        names = [f["name"] for f in ir["operation"]["fields"]]
        assert names == ["id", "title"]

    def test_defer_excludes_fields(self) -> None:
        ir = QuerySet(Note).defer("body")._build_ir()
        names = [f["name"] for f in ir["operation"]["fields"]]
        assert "body" not in names
        assert "title" in names

    def test_exists_ir_flag(self) -> None:
        ir = QuerySet(Note).filter(published=True)._build_exists_ir()
        assert ir["exists"] is True


class TestCompileWithNative:
    @pytest.fixture
    def require_native(self) -> None:
        pytest.importorskip("ferrum._native")

    def test_q_and_compiles(self, require_native: None) -> None:
        compiled = QuerySet(Note).filter(Q(published=True) & Q(title="a"))._compile()
        assert "WHERE" in compiled["sql_text"]
        assert "AND" in compiled["sql_text"]

    def test_distinct_compiles(self, require_native: None) -> None:
        compiled = QuerySet(Note).distinct()._compile()
        assert "SELECT DISTINCT" in compiled["sql_text"]

    def test_exists_compiles(self, require_native: None) -> None:
        compiled = QuerySet(Note).filter(published=True)._compile_ir(
            QuerySet(Note).filter(published=True)._build_exists_ir()
        )
        assert compiled["sql_text"].startswith("SELECT EXISTS(")


class TestDeferredFieldAccess:
    def test_deferred_field_raises_on_access(self) -> None:
        inst = Note.model_construct(id=1, title="t")
        object.__setattr__(inst, "__ferrum_deferred__", frozenset({"body"}))
        with pytest.raises(FerrumDeferredFieldError):
            _ = inst.body
