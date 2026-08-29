"""Unit tests for QuerySet._build_ir() and BindValue encoding.

Covers QE-1 (unknown field fails before SQL), QE-5 (unsupported operator for
type → structured error), and the ADR-002 v1 IR shape contract.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumConfigError
from ferrum.queryset import QuerySet, _append_for_update_clause, _encode_bind_value

# ---------------------------------------------------------------------------
# Fixture models
# ---------------------------------------------------------------------------


class Article(ferrum.Model):
    id: int = 0
    title: str = ""
    published: bool = False
    score: float = 0.0


class JsonArticle(ferrum.Model):
    id: int = 0
    tags: list[str] = ferrum.Field(default_factory=list, jsonb_list=True)
    payload: dict = ferrum.Field(default_factory=dict)


# ---------------------------------------------------------------------------
# BindValue encoding — adjacent-tag format {"type": ..., "value": ...}
# ---------------------------------------------------------------------------


class TestEncodeBindValue:
    def test_none_encodes_to_null(self) -> None:
        assert _encode_bind_value(None) == {"type": "null"}

    def test_null_has_no_value_key(self) -> None:
        result = _encode_bind_value(None)
        assert "value" not in result

    def test_bool_true(self) -> None:
        assert _encode_bind_value(True) == {"type": "bool", "value": True}

    def test_bool_false(self) -> None:
        assert _encode_bind_value(False) == {"type": "bool", "value": False}

    def test_bool_before_int(self) -> None:
        # bool is a subclass of int; must produce type "bool" not "int"
        result = _encode_bind_value(True)
        assert result["type"] == "bool"

    def test_int(self) -> None:
        assert _encode_bind_value(42) == {"type": "int", "value": 42}

    def test_negative_int(self) -> None:
        assert _encode_bind_value(-5) == {"type": "int", "value": -5}

    def test_float(self) -> None:
        assert _encode_bind_value(3.14) == {"type": "float", "value": 3.14}

    def test_string(self) -> None:
        assert _encode_bind_value("hello") == {"type": "text", "value": "hello"}

    def test_bytes(self) -> None:
        result = _encode_bind_value(b"\x01\x02")
        assert result == {"type": "bytes", "value": [1, 2]}

    def test_datetime(self) -> None:
        dt = datetime(2024, 6, 1, 12, 0, 0)
        result = _encode_bind_value(dt)
        assert result["type"] == "datetime"
        assert "2024-06-01" in result["value"]  # type: ignore[operator]

    def test_datetime_before_date(self) -> None:
        # datetime subclasses date; must produce type "datetime", not fall to text
        dt = datetime(2024, 6, 1, 12, 0, 0)
        result = _encode_bind_value(dt)
        assert result["type"] == "datetime"

    def test_date_falls_through_to_text(self) -> None:
        # date has no v1 Rust BindValue variant; falls back to text via str()
        d = date(2024, 6, 1)
        result = _encode_bind_value(d)
        assert result == {"type": "text", "value": "2024-06-01"}

    def test_time_falls_through_to_text(self) -> None:
        # time has no v1 Rust BindValue variant; falls back to text via str()
        t = time(12, 30, 0)
        result = _encode_bind_value(t)
        assert result["type"] == "text"
        assert "12:30:00" in result["value"]  # type: ignore[operator]

    def test_unknown_type_fallback_to_text(self) -> None:
        class Custom:
            def __str__(self) -> str:
                return "custom_value"

        result = _encode_bind_value(Custom())
        assert result == {"type": "text", "value": "custom_value"}


# ---------------------------------------------------------------------------
# _build_ir() structure
# ---------------------------------------------------------------------------


class TestBuildIrStructure:
    def test_insert_encodes_json_fields_as_json_text(self) -> None:
        ir = QuerySet(JsonArticle)._build_insert_ir({"tags": ["orm"], "payload": {"nested": True}})
        values = {field["name"]: value for field, value in ir["operation"]["values"]}
        assert values["tags"] == {"type": "text", "value": '["orm"]'}
        assert values["payload"] == {"type": "text", "value": '{"nested":true}'}

    def test_ir_version(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        assert ir["version"] == 4

    def test_model_name(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        assert ir["model_name"] == "Article"

    def test_operation_kind_is_select(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        assert ir["operation"]["kind"] == "select"

    def test_operation_fields_include_all_model_fields(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        field_names = [f["name"] for f in ir["operation"]["fields"]]
        assert "id" in field_names
        assert "title" in field_names
        assert "published" in field_names
        assert "score" in field_names

    def test_operation_fields_have_index(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        for field in ir["operation"]["fields"]:
            assert isinstance(field["index"], int)
            assert isinstance(field["name"], str)

    def test_field_indices_are_ordered(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        indices = [f["index"] for f in ir["operation"]["fields"]]
        assert indices == sorted(indices)

    def test_empty_filters_list(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        assert ir["filters"] == []

    def test_empty_order_by_list(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        assert ir["order_by"] == []

    def test_limit_none_by_default(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        assert ir["limit"] is None

    def test_offset_none_by_default(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        ir = qs._build_ir()
        assert ir["offset"] is None


class TestBuildIrFilters:
    def test_json_contains_filter_encodes_valid_json_text(self) -> None:
        ir = QuerySet(JsonArticle).filter(payload__contains={"ok": False})._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "contains"
        assert flt["value"] == {"type": "text", "value": '{"ok":false}'}

    def test_json_key_filters_keep_key_bind_types(self) -> None:
        has_key = QuerySet(JsonArticle).filter(payload__has_key="ok")._build_ir()
        has_any = QuerySet(JsonArticle).filter(payload__has_any_keys=["ok", "state"])._build_ir()
        assert has_key["predicate"]["filter"]["value"] == {
            "type": "text",
            "value": "ok",
        }
        assert has_any["predicate"]["filter"]["value"] == {
            "type": "text_array",
            "value": ["ok", "state"],
        }

    def test_postgres_json_operators_compile_with_native_types(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        contains_sql = (
            QuerySet(JsonArticle).filter(payload__contains={"ok": False})._compile()["sql_text"]
        )
        has_key_sql = QuerySet(JsonArticle).filter(payload__has_key="ok")._compile()["sql_text"]
        has_any_sql = (
            QuerySet(JsonArticle)
            .filter(payload__has_any_keys=["ok", "state"])
            ._compile()["sql_text"]
        )

        assert '"payload" @> $1::jsonb' in contains_sql
        assert '"payload" ? $1' in has_key_sql
        assert '"payload" ?| $1' in has_any_sql

    def test_single_eq_filter(self) -> None:
        qs = QuerySet(Article).filter(title="hello")
        ir = qs._build_ir()
        pred = ir["predicate"]
        assert pred["kind"] == "filter"
        flt = pred["filter"]
        assert flt["field"]["name"] == "title"
        assert isinstance(flt["field"]["index"], int)
        assert flt["operator"] == "eq"
        assert flt["value"] == {"type": "text", "value": "hello"}

    def test_int_filter_value_encoding(self) -> None:
        qs = QuerySet(Article).filter(id=42)
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["value"] == {"type": "int", "value": 42}

    def test_bool_filter_value_encoding(self) -> None:
        qs = QuerySet(Article).filter(published=True)
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["value"] == {"type": "bool", "value": True}

    def test_lookup_operator_parsing(self) -> None:
        qs = QuerySet(Article).filter(title__contains="rust")
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["field"]["name"] == "title"
        assert flt["operator"] == "contains"

    def test_multiple_filters(self) -> None:
        qs = QuerySet(Article).filter(published=True).filter(title__contains="rust")
        ir = qs._build_ir()
        assert ir["predicate"]["kind"] == "and"
        assert len(ir["predicate"]["children"]) == 2

    def test_filter_field_ref_index_matches_metadata(self) -> None:
        meta = Article.get_metadata()
        field_names = [f.name for f in meta.fields]
        qs = QuerySet(Article).filter(title="x")
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["field"]["index"] == field_names.index("title")

    def test_gt_operator_on_int_field(self) -> None:
        qs = QuerySet(Article).filter(id__gt=10)
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "gt"
        assert flt["value"] == {"type": "int", "value": 10}


class TestBuildIrOrderBy:
    def test_asc_order_by(self) -> None:
        qs = QuerySet(Article).order_by("title")
        ir = qs._build_ir()
        assert len(ir["order_by"]) == 1
        ob = ir["order_by"][0]
        assert ob["field"]["name"] == "title"
        assert ob["direction"] == "asc"

    def test_desc_order_by(self) -> None:
        qs = QuerySet(Article).order_by("-id")
        ir = qs._build_ir()
        ob = ir["order_by"][0]
        assert ob["field"]["name"] == "id"
        assert ob["direction"] == "desc"

    def test_order_by_field_ref_has_index(self) -> None:
        qs = QuerySet(Article).order_by("id")
        ir = qs._build_ir()
        ob = ir["order_by"][0]
        assert isinstance(ob["field"]["index"], int)


class TestBuildIrLimitOffset:
    def test_limit_included(self) -> None:
        qs = QuerySet(Article).limit(10)
        ir = qs._build_ir()
        assert ir["limit"] == 10

    def test_offset_included(self) -> None:
        qs = QuerySet(Article).offset(5)
        ir = qs._build_ir()
        assert ir["offset"] == 5

    def test_limit_and_offset(self) -> None:
        qs = QuerySet(Article).limit(20).offset(40)
        ir = qs._build_ir()
        assert ir["limit"] == 20
        assert ir["offset"] == 40


# ---------------------------------------------------------------------------
# QE-1: unknown field fails before SQL (no compile)
# ---------------------------------------------------------------------------


class TestBuildIrAllowlistRejection:
    def test_unknown_field_in_filter_raises_at_filter_call(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        with pytest.raises(FerrumCompileError) as exc_info:
            qs.filter(nonexistent=1)
        err = exc_info.value
        assert err.field == "nonexistent"
        assert err.model == "Article"

    def test_unknown_field_in_build_ir_raises(self) -> None:
        # Bypass filter() validation by mutating internal state directly.
        qs: QuerySet[Article] = QuerySet(Article)
        qs._filters.append({"field": "hacked", "operator": "eq", "value": 1})
        with pytest.raises(FerrumCompileError) as exc_info:
            qs._build_ir()
        assert exc_info.value.field == "hacked"

    def test_unknown_operator_raises_at_build_ir(self) -> None:
        # "contains" is valid for text but not for int fields.
        qs: QuerySet[Article] = QuerySet(Article)
        # Bypass filter() field validation, inject bad operator
        qs._filters.append({"field": "id", "operator": "contains", "value": 1})
        qs._is_filtered = True
        with pytest.raises(FerrumCompileError) as exc_info:
            qs._build_ir()
        err = exc_info.value
        assert err.field == "id"
        assert err.operator == "contains"

    def test_compile_error_carries_model_name(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        with pytest.raises(FerrumCompileError) as exc_info:
            qs.filter(badfield=1)
        assert exc_info.value.model == "Article"

    def test_compile_error_message_does_not_echo_submitted_value(self) -> None:
        # Error messages must NOT contain submitted values (LOG-2 / DM-4).
        sentinel = "sentinel_payload_12345"
        qs: QuerySet[Article] = QuerySet(Article)
        with pytest.raises(FerrumCompileError) as exc_info:
            qs.filter(nonexistent=sentinel)
        assert sentinel not in str(exc_info.value)

    def test_unknown_order_by_field_raises_at_build_ir(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        qs._order_by.append({"field": "bad_field", "direction": "asc"})
        with pytest.raises(FerrumCompileError):
            qs._build_ir()

    def test_invalid_sort_direction_raises_at_build_ir(self) -> None:
        qs: QuerySet[Article] = QuerySet(Article)
        qs._order_by.append({"field": "id", "direction": "sideways"})
        with pytest.raises(FerrumCompileError):
            qs._build_ir()


# ---------------------------------------------------------------------------
# W1-A: null-equality compilation (filter(field=None) → IS NULL)
# ---------------------------------------------------------------------------


class TestNullEqualityCompilation:
    """filter(field=None) / exclude(field=None) / filter(field__ne=None).

    These compile to IS NULL / IS NOT NULL. SQL ``= NULL`` never matches;
    ``IS NULL`` is the only correct form. Django-style ``__is_null=True/False``
    continues to work unchanged.
    """

    def test_filter_none_emits_is_null(self) -> None:
        qs = QuerySet(Article).filter(title=None)
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "is_null"
        assert flt["value"] == {"type": "null"}

    def test_filter_none_compiles_to_is_null_sql(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article).filter(title=None)._compile()["sql_text"]
        assert '"title" IS NULL' in sql
        assert "= $" not in sql

    def test_filter_none_has_no_bound_params(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        compiled = QuerySet(Article).filter(title=None)._compile()
        assert compiled["bound_params"] == []

    def test_filter_eq_none_emits_is_null(self) -> None:
        qs = QuerySet(Article).filter(title__eq=None)
        ir = qs._build_ir()
        assert ir["predicate"]["filter"]["operator"] == "is_null"

    def test_filter_ne_none_emits_is_not_null(self) -> None:
        qs = QuerySet(Article).filter(title__ne=None)
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "is_not_null"
        assert flt["value"] == {"type": "null"}

    def test_filter_ne_none_compiles_to_is_not_null_sql(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article).filter(title__ne=None)._compile()["sql_text"]
        assert '"title" IS NOT NULL' in sql

    def test_exclude_none_emits_not_is_null(self) -> None:
        """exclude(field=None) wraps the is_null leaf in NOT (...) via ~Q."""
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article).exclude(title=None)._compile()["sql_text"]
        assert 'NOT ("title" IS NULL)' in sql

    def test_exclude_ne_none_emits_not_is_not_null(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article).exclude(title__ne=None)._compile()["sql_text"]
        assert 'NOT ("title" IS NOT NULL)' in sql

    def test_explicit_is_null_true_still_works(self) -> None:
        qs = QuerySet(Article).filter(title__is_null=True)
        ir = qs._build_ir()
        assert ir["predicate"]["filter"]["operator"] == "is_null"

    def test_explicit_is_null_false_still_works(self) -> None:
        qs = QuerySet(Article).filter(title__is_null=False)
        ir = qs._build_ir()
        assert ir["predicate"]["filter"]["operator"] == "is_not_null"

    def test_explicit_is_not_null_true_still_works(self) -> None:
        qs = QuerySet(Article).filter(title__is_not_null=True)
        ir = qs._build_ir()
        assert ir["predicate"]["filter"]["operator"] == "is_not_null"

    def test_explicit_is_not_null_false_still_works(self) -> None:
        qs = QuerySet(Article).filter(title__is_not_null=False)
        ir = qs._build_ir()
        assert ir["predicate"]["filter"]["operator"] == "is_null"

    def test_non_none_eq_still_uses_eq(self) -> None:
        """A non-None value must NOT be rewritten to is_null."""
        qs = QuerySet(Article).filter(title="hello")
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "eq"
        assert flt["value"] == {"type": "text", "value": "hello"}

    def test_non_none_ne_still_uses_ne(self) -> None:
        qs = QuerySet(Article).filter(title__ne="hello")
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "ne"
        assert flt["value"] == {"type": "text", "value": "hello"}

    def test_gt_none_not_rewritten(self) -> None:
        """Only eq/ne with None are rewritten; gt None stays as gt."""
        qs = QuerySet(Article).filter(id__gt=None)
        ir = qs._build_ir()
        flt = ir["predicate"]["filter"]
        assert flt["operator"] == "gt"
        assert flt["value"] == {"type": "null"}


# ---------------------------------------------------------------------------
# W1-A: danger_delete_all emits valid SQL (no dangling WHERE)
# ---------------------------------------------------------------------------


class TestDangerDeleteAllSql:
    def test_danger_delete_all_emits_delete_from_no_where(self) -> None:
        """danger_delete_all IR compiles to ``DELETE FROM "table"`` with no WHERE."""
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = QuerySet(Article)
        delete_ir = qs._build_delete_ir()
        delete_ir["operation"]["danger"] = True
        compiled = qs._compile_ir(delete_ir, dialect="postgres")
        sql = compiled["sql_text"]
        assert sql.startswith('DELETE FROM "article"')
        assert "WHERE" not in sql
        assert compiled["bound_params"] == []

    def test_danger_delete_all_no_trailing_where_clause(self) -> None:
        """The SQL must not end with a dangling WHERE keyword."""
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = QuerySet(Article)
        delete_ir = qs._build_delete_ir()
        delete_ir["operation"]["danger"] = True
        sql = qs._compile_ir(delete_ir, dialect="postgres")["sql_text"]
        assert not sql.rstrip().endswith("WHERE")
        assert not sql.rstrip().endswith("WHERE ")


# ---------------------------------------------------------------------------
# W1-A: Golden IR-to-SQL contract tests
# ---------------------------------------------------------------------------


class TestGoldenIrToSql:
    """Golden SQL snapshots for create, filter, join, update, upsert, hydration paths."""

    _SELECT_ALL = 'SELECT "id", "title", "published", "score" FROM "article"'

    def test_select_all_golden(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article)._compile()["sql_text"]
        assert sql == self._SELECT_ALL

    def test_select_filter_eq_golden(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article).filter(title="x")._compile()["sql_text"]
        assert sql == f'{self._SELECT_ALL} WHERE "title" = $1'

    def test_select_filter_is_null_golden(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article).filter(title=None)._compile()["sql_text"]
        assert sql == f'{self._SELECT_ALL} WHERE "title" IS NULL'

    def test_select_order_by_limit_golden(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        sql = QuerySet(Article).order_by("-id").limit(10)._compile()["sql_text"]
        assert sql == (f'{self._SELECT_ALL} ORDER BY "id" DESC LIMIT $1')

    def test_delete_filtered_golden(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = QuerySet(Article).filter(id=1)
        sql = qs._compile_ir(qs._build_delete_ir(), dialect="postgres")["sql_text"]
        assert sql == 'DELETE FROM "article" WHERE "id" = $1'

    def test_danger_delete_all_golden(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = QuerySet(Article)
        ir = qs._build_delete_ir()
        ir["operation"]["danger"] = True
        sql = qs._compile_ir(ir, dialect="postgres")["sql_text"]
        assert sql == 'DELETE FROM "article"'

    def test_update_filtered_golden(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = QuerySet(Article).filter(id=1)
        ir = qs._build_update_ir({"title": "new"})
        sql = qs._compile_ir(ir, dialect="postgres")["sql_text"]
        assert sql == (
            'UPDATE "article" SET "title" = $1 WHERE "id" = $2 '
            'RETURNING "id", "title", "published", "score"'
        )


class TestAppendForUpdateClauseQuoteEscaping:
    def test_simple_table_name_quoted(self) -> None:
        sql = _append_for_update_clause("SELECT 1", {"of": ["article"]}, "postgres")
        assert sql == 'SELECT 1 FOR UPDATE OF "article"'

    def test_multiple_table_names_quoted(self) -> None:
        sql = _append_for_update_clause("SELECT 1", {"of": ["article", "comment"]}, "postgres")
        assert sql == 'SELECT 1 FOR UPDATE OF "article", "comment"'

    def test_embedded_double_quote_doubled(self) -> None:
        sql = _append_for_update_clause("SELECT 1", {"of": ['bad"name']}, "postgres")
        assert sql == 'SELECT 1 FOR UPDATE OF "bad""name"'

    def test_non_postgres_dialect_rejected(self) -> None:
        with pytest.raises(FerrumConfigError):
            _append_for_update_clause("SELECT 1", {"of": ["article"]}, "mysql")
