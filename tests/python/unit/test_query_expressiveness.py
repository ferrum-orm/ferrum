"""Unit tests for W2-B query expressiveness features.

Tests cover:
- Reusable expression classes (F, Star, Combinable)
- Aggregate factory methods accepting F and Star
- select_related() cycle/depth limits
- Total JOIN count safety guard
- group_by / order_by with F expressions
- Immutability of QuerySet chaining
- SQL safety: no raw SQL, no string fragments, allowlisted identifiers only

These tests do NOT require the native Rust extension or a live database;
they validate IR building, Python-side guards, and expression ergonomics.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.expressions import F, Star, resolve_field_name
from ferrum.queryset import Aggregate, QuerySet

# ---------------------------------------------------------------------------
# Test models (mirror the patterns in test_aggregate_primitives.py)
# ---------------------------------------------------------------------------


class Metric(ferrum.Model):
    id: int = 0
    category: str = ""
    amount: float = 0.0
    active: bool = False
    created_at: datetime = datetime(2024, 1, 1)


class User(ferrum.Model):
    id: int = 0
    email: str = ""
    name: str = ""
    active: bool = True


class GuardAuthor(ferrum.Model):
    id: int = 0
    name: str = ""


class GuardPost(ferrum.Model):
    id: int = 0
    author_id: int = 0
    title: str = ""
    author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="GuardAuthor", related_name="posts", on_delete="CASCADE"
    )


class GuardMultiFK(ferrum.Model):
    id: int = 0
    rel1_id: int = 0
    rel2_id: int = 0
    rel3_id: int = 0
    rel4_id: int = 0
    rel5_id: int = 0
    rel6_id: int = 0
    rel1: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="GuardAuthor", related_name="mc1", on_delete="CASCADE"
    )
    rel2: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="GuardAuthor", related_name="mc2", on_delete="CASCADE"
    )
    rel3: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="GuardAuthor", related_name="mc3", on_delete="CASCADE"
    )
    rel4: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="GuardAuthor", related_name="mc4", on_delete="CASCADE"
    )
    rel5: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="GuardAuthor", related_name="mc5", on_delete="CASCADE"
    )
    rel6: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="GuardAuthor", related_name="mc6", on_delete="CASCADE"
    )


# ---------------------------------------------------------------------------
# F expression tests
# ---------------------------------------------------------------------------


class TestFExpression:
    def test_f_repr(self) -> None:
        assert repr(F("email")) == "F('email')"

    def test_f_requires_non_empty_string(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            F("")
        with pytest.raises(ValueError, match="non-empty"):
            F(123)  # type: ignore[arg-type]

    def test_f_equality_and_hash(self) -> None:
        assert F("email") == F("email")
        assert F("email") != F("name")
        assert hash(F("email")) == hash(F("email"))
        assert F("email") != "email"

    def test_resolve_field_name_from_string(self) -> None:
        assert resolve_field_name("email") == "email"

    def test_resolve_field_name_from_f(self) -> None:
        assert resolve_field_name(F("email")) == "email"

    def test_resolve_field_name_rejects_other_types(self) -> None:
        with pytest.raises(TypeError, match="str or F"):
            resolve_field_name(123)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="str or F"):
            resolve_field_name(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Star expression tests
# ---------------------------------------------------------------------------


class TestStarExpression:
    def test_star_repr(self) -> None:
        assert repr(Star()) == "Star()"

    def test_star_equality(self) -> None:
        assert Star() == Star()
        assert Star() != F("x")

    def test_star_hash(self) -> None:
        assert hash(Star()) == hash(Star())


# ---------------------------------------------------------------------------
# Aggregate factory with F and Star
# ---------------------------------------------------------------------------


class TestAggregateFactories:
    def test_count_with_star(self) -> None:
        agg = Aggregate.count(Star())
        assert agg.function == "count"
        assert agg.field is None

    def test_count_with_f(self) -> None:
        agg = Aggregate.count(F("id"))
        assert agg.function == "count"
        assert agg.field == "id"

    def test_count_without_field(self) -> None:
        agg = Aggregate.count()
        assert agg.function == "count"
        assert agg.field is None

    def test_sum_with_f(self) -> None:
        agg = Aggregate.sum(F("amount"))
        assert agg.function == "sum"
        assert agg.field == "amount"

    def test_sum_with_string(self) -> None:
        agg = Aggregate.sum("amount")
        assert agg.function == "sum"
        assert agg.field == "amount"

    def test_avg_with_f(self) -> None:
        agg = Aggregate.avg(F("amount"))
        assert agg.function == "avg"
        assert agg.field == "amount"

    def test_min_with_f(self) -> None:
        agg = Aggregate.min(F("amount"))
        assert agg.function == "min"
        assert agg.field == "amount"

    def test_max_with_f(self) -> None:
        agg = Aggregate.max(F("amount"))
        assert agg.function == "max"
        assert agg.field == "amount"

    def test_aggregate_with_filter(self) -> None:
        from ferrum.expressions import Q

        agg = Aggregate.sum(F("amount"), filter=Q(active=True))
        assert agg.function == "sum"
        assert agg.field == "amount"
        assert agg.filter is not None


# ---------------------------------------------------------------------------
# group_by / order_by with F expressions
# ---------------------------------------------------------------------------


class TestFInGroupByAndOrderBy:
    def test_group_by_with_f(self) -> None:
        qs = QuerySet(Metric).group_by(F("category"))
        assert len(qs._aggregate_groups) == 1
        assert qs._aggregate_groups[0]["field"] == "category"

    def test_group_by_with_string_and_f(self) -> None:
        qs = QuerySet(Metric).group_by("category", F("active"))
        assert len(qs._aggregate_groups) == 2
        assert qs._aggregate_groups[0]["field"] == "category"
        assert qs._aggregate_groups[1]["field"] == "active"

    def test_order_by_with_f(self) -> None:
        qs = QuerySet(User).order_by(F("email"))
        assert qs._order_by == [{"field": "email", "direction": "asc"}]

    def test_order_by_desc_with_string(self) -> None:
        qs = QuerySet(User).order_by("-email")
        assert qs._order_by == [{"field": "email", "direction": "desc"}]

    def test_order_by_mixed_f_and_string(self) -> None:
        qs = QuerySet(User).order_by(F("email"), "-id")
        assert qs._order_by == [
            {"field": "email", "direction": "asc"},
            {"field": "id", "direction": "desc"},
        ]


# ---------------------------------------------------------------------------
# select_related cycle/depth limits
# ---------------------------------------------------------------------------


class TestSelectRelatedGuards:
    def test_select_related_rejects_non_relation(self) -> None:
        """select_related('email') raises because 'email' is a field, not a relation."""
        with pytest.raises(FerrumCompileError, match="Unknown relation"):
            QuerySet(User).select_related("email")

    def test_select_related_preserves_immutability_on_error(self) -> None:
        """When select_related raises, the original queryset is unchanged."""
        qs = QuerySet(User)
        with pytest.raises(FerrumCompileError):
            qs.select_related("invalid_relation")
        assert qs._select_related == ()

    def test_select_related_rejects_duplicate_relation(self) -> None:
        with pytest.raises(FerrumCompileError, match="Duplicate relation"):
            QuerySet(GuardPost).select_related("author", "author")

    def test_select_related_rejects_chained_duplicate_relation(self) -> None:
        qs = QuerySet(GuardPost).select_related("author")
        with pytest.raises(FerrumCompileError, match="Duplicate relation"):
            qs.select_related("author")

    def test_select_related_rejects_depth_exceeding_limit(self) -> None:
        with pytest.raises(FerrumCompileError, match="exceeds limit"):
            QuerySet(GuardMultiFK).select_related("rel1", "rel2", "rel3", "rel4", "rel5", "rel6")


class TestTotalJoinCap:
    def test_build_ir_rejects_total_join_count_exceeding_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ferrum.queryset as qs_mod

        monkeypatch.setattr(qs_mod, "_MAX_TOTAL_JOINS", 1)
        qs = QuerySet(GuardMultiFK).select_related("rel1", "rel2")
        with pytest.raises(FerrumCompileError, match="Total JOIN count"):
            qs._build_ir()


# ---------------------------------------------------------------------------
# QuerySet immutability
# ---------------------------------------------------------------------------


class TestQuerySetImmutability:
    def test_filter_returns_new_queryset(self) -> None:
        qs = QuerySet(User)
        filtered = qs.filter(id=1)
        assert filtered is not qs
        assert filtered._is_filtered
        assert not qs._is_filtered

    def test_order_by_returns_new_queryset(self) -> None:
        qs = QuerySet(User)
        ordered = qs.order_by("email")
        assert ordered is not qs
        assert ordered._order_by != qs._order_by

    def test_limit_returns_new_queryset(self) -> None:
        qs = QuerySet(User)
        limited = qs.limit(10)
        assert limited is not qs
        assert limited._limit == 10
        assert qs._limit is None

    def test_distinct_returns_new_queryset(self) -> None:
        qs = QuerySet(User)
        distinct = qs.distinct()
        assert distinct is not qs
        assert distinct._distinct
        assert not qs._distinct

    def test_group_by_returns_new_queryset(self) -> None:
        qs = QuerySet(Metric)
        grouped = qs.group_by("category")
        assert grouped is not qs
        assert len(grouped._aggregate_groups) == 1
        assert qs._aggregate_groups == []

    def test_chaining_preserves_immutability(self) -> None:
        qs = QuerySet(User)
        chained = qs.filter(active=True).order_by("-id").limit(10)
        assert qs._is_filtered is False
        assert qs._order_by == []
        assert qs._limit is None
        assert chained._is_filtered
        assert len(chained._order_by) == 1
        assert chained._limit == 10


# ---------------------------------------------------------------------------
# SQL safety: no raw SQL, no string fragments
# ---------------------------------------------------------------------------


class TestSQLSafety:
    def test_filter_rejects_unknown_field(self) -> None:
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            QuerySet(User).filter(unknown_field="value")._build_ir()

    def test_group_by_rejects_unknown_field(self) -> None:
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            QuerySet(Metric).group_by("unknown")

    def test_order_by_rejects_unknown_field_in_build_ir(self) -> None:
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            QuerySet(User).order_by("unknown_field")._build_ir()

    def test_no_raw_method_exists(self) -> None:
        qs = QuerySet(User)
        assert not hasattr(qs, "raw")
        assert not hasattr(qs, "extra")

    def test_ir_does_not_contain_raw_sql(self) -> None:
        ir = QuerySet(User).filter(id=1)._build_ir()
        ir_str = repr(ir)
        assert "SELECT" not in ir_str
        assert "WHERE" not in ir_str
        assert "FROM" not in ir_str


# ---------------------------------------------------------------------------
# Existing expressiveness features (regression tests)
# ---------------------------------------------------------------------------


class TestExistingExpressiveness:
    def test_distinct_ir(self) -> None:
        ir = QuerySet(User).distinct()._build_ir()
        assert ir["distinct"] is True

    def test_exists_ir(self) -> None:
        qs = QuerySet(User).filter(active=True)
        ir = qs._build_exists_ir()
        assert ir["exists"] is True

    def test_q_composition(self) -> None:
        from ferrum.expressions import Q

        q = Q(active=True) & (Q(email="a@b.com") | Q(email="c@d.com"))
        qs = QuerySet(User).filter(q)
        assert qs._predicate_q is not None

    def test_q_negation(self) -> None:
        from ferrum.expressions import Q

        q = ~Q(active=True)
        qs = QuerySet(User).filter(q)
        assert qs._predicate_q is not None
        assert qs._predicate_q.negated

    def test_aggregate_ir_structure(self) -> None:
        ir, _keys = (
            QuerySet(Metric)
            .group_by("category")
            .having(total__gte=10)
            ._build_aggregate_ir(
                {
                    "count": Aggregate.count(),
                    "active_count": Aggregate.count(
                        filter=__import__("ferrum.expressions", fromlist=["Q"]).Q(active=True)
                    ),
                    "total": Aggregate.sum("amount"),
                }
            )
        )
        assert ir["version"] == 4
        assert ir["aggregation"] is not None
        agg = ir["aggregation"]
        assert len(agg["groups"]) == 1
        assert agg["groups"][0]["field"]["name"] == "category"
        assert len(agg["aggregates"]) == 3
        assert agg["aggregates"][0]["function"] == "count"
        assert agg["aggregates"][1]["filter"] is not None
        assert agg["having"] == [
            {
                "aggregate_index": 2,
                "operator": "gte",
                "value": {"type": "int", "value": 10},
            }
        ]

    def test_aggregate_with_f_expression(self) -> None:
        ir, _keys = QuerySet(Metric)._build_aggregate_ir({"total": Aggregate.sum(F("amount"))})
        assert ir["aggregation"]["aggregates"][0]["field"]["name"] == "amount"

    def test_aggregate_with_star(self) -> None:
        ir, _keys = QuerySet(Metric)._build_aggregate_ir({"total": Aggregate.count(Star())})
        assert ir["aggregation"]["aggregates"][0]["function"] == "count"
        assert ir["aggregation"]["aggregates"][0]["field"] is None

    def test_date_trunc_with_f(self) -> None:
        qs = QuerySet(Metric).date_trunc(F("created_at"), "day")
        assert len(qs._aggregate_groups) == 1
        assert qs._aggregate_groups[0]["field"] == "created_at"
        assert qs._aggregate_groups[0]["granularity"] == "day"


# ---------------------------------------------------------------------------
# IR version and structure invariants
# ---------------------------------------------------------------------------


class TestIRInvariants:
    def test_ir_version_is_4(self) -> None:
        from ferrum.queryset import _IR_VERSION

        assert _IR_VERSION == 4

    def test_build_ir_returns_version(self) -> None:
        ir = QuerySet(User)._build_ir()
        assert ir["version"] == 4

    def test_build_ir_has_required_fields(self) -> None:
        ir = QuerySet(User)._build_ir()
        for field in (
            "version",
            "model_name",
            "operation",
            "filters",
            "order_by",
            "limit",
            "offset",
            "distinct",
            "exists",
            "joins",
        ):
            assert field in ir, f"IR missing field: {field}"

    def test_filter_values_are_bound_not_interpolated(self) -> None:
        ir = QuerySet(User).filter(email="test@example.com")._build_ir()
        # The IR carries values as BindValue dicts ({"type": "text", "value": "..."})
        # — not as raw SQL fragments. Verify the value is properly wrapped.
        if ir.get("predicate"):
            pred = ir["predicate"]
            pred_str = repr(pred)
            # The value must appear inside a BindValue structure, not as bare text
            assert "'type': 'text'" in pred_str or '"type": "text"' in pred_str
        # No SQL keywords should appear in the IR dict
        ir_str = repr(ir)
        for keyword in ("SELECT", "WHERE", "FROM", "INSERT", "UPDATE", "DELETE"):
            assert keyword not in ir_str, f"IR should not contain SQL keyword: {keyword}"
