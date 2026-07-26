"""Unit tests for Django-style relation filter JOINs (``team__slug``)."""

from __future__ import annotations

from typing import ClassVar

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.expressions import Q


class RelTeam(ferrum.Model):
    model_config = ferrum.ModelConfig(table="teams")

    id: int = 0
    slug: str = ""
    name: str = ""
    metadata_: dict = ferrum.Field(default_factory=dict, db_column="metadata")


class RelTicket(ferrum.Model):
    model_config = ferrum.ModelConfig(table="tickets")

    id: int = 0
    team_id: int = 0
    title: str = ""
    team: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="RelTeam", related_name="tickets", on_delete="CASCADE"
    )


class RelTicketRead(ferrum.Model):
    """Parent model declaring the FK — mirrors TicketRead / Ticket split."""

    model_config = ferrum.ModelConfig(table="tickets")

    id: int = 0
    team_id: int = 0
    title: str = ""
    team: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="RelTeam", related_name="ticket_reads", on_delete="CASCADE"
    )


class RelTicketConcrete(RelTicketRead):
    """Concrete subclass that only adds columns — FK lives on the parent."""

    embedding: str = ""


class TestRelationFilterIr:
    def test_inherited_fk_visible_on_subclass(self) -> None:
        rels = {r.field_name: r for r in RelTicketConcrete.get_metadata().relations}
        assert "team" in rels
        assert rels["team"].db_column == "team_id"
        qs = RelTicketConcrete.objects.filter(team__slug="dice")
        ir = qs._build_ir()
        assert ir["joins"][0]["alias"] == "team"
        assert ir["predicate"]["filter"]["join_alias"] == "team"

    def test_relation_eq_lookup_adds_inner_join(self) -> None:
        qs = RelTicket.objects.filter(team__slug="dice")
        ir = qs._build_ir()
        assert ir["joins"]
        join = ir["joins"][0]
        assert join["alias"] == "team"
        assert join["join_kind"] == "inner"
        assert join["project_remote"] is False
        assert join["remote_table"] == "teams"
        filt = ir["predicate"]["filter"]
        assert filt["join_alias"] == "team"
        assert filt["field"]["name"] == "slug"
        assert filt["operator"] == "eq"

    def test_relation_operator_lookup(self) -> None:
        qs = RelTicket.objects.filter(team__name__icontains="super")
        filt = qs._build_ir()["predicate"]["filter"]
        assert filt["join_alias"] == "team"
        assert filt["field"]["name"] == "name"
        assert filt["operator"] == "icontains"

    def test_relation_json_lookup_carries_remote_field_type(self) -> None:
        ir = RelTicket.objects.filter(team__metadata___has_key="region")._build_ir()
        remote_field = ir["joins"][0]["remote_fields"][0]
        assert remote_field["name"] == "metadata_"
        assert remote_field["field_type"] == "json"

    def test_q_or_relation_lookups(self) -> None:
        qs = RelTicket.objects.filter(Q(team__slug="dice") | Q(team__id=1))
        ir = qs._build_ir()
        assert ir["predicate"]["kind"] == "or"
        assert len(ir["joins"]) == 1
        assert ir["joins"][0]["join_kind"] == "inner"
        # Both remote fields present for allowlisting.
        remote_names = {f["name"] for f in ir["joins"][0]["remote_fields"]}
        assert "slug" in remote_names
        assert "id" in remote_names

    def test_select_related_plus_filter_reuses_left_join(self) -> None:
        qs = RelTicket.objects.select_related("team").filter(team__slug="dice")
        ir = qs._build_ir()
        assert len(ir["joins"]) == 1
        assert ir["joins"][0]["join_kind"] == "left"
        assert ir["joins"][0]["project_remote"] is True

    def test_unknown_remote_field_raises(self) -> None:
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            RelTicket.objects.filter(team__missing="x")

    def test_nested_relation_rejected(self) -> None:
        # RelTeam has no FK, so this is unknown field — also cover multi-hop message
        # via a synthetic double path when the first hop is valid.
        with pytest.raises(FerrumCompileError):
            RelTicket.objects.filter(team__slug__extra="x")  # type: ignore[arg-type]

    def test_base_lookup_unchanged(self) -> None:
        qs = RelTicket.objects.filter(title__icontains="bug")
        ir = qs._build_ir()
        assert ir["joins"] == []
        assert "join_alias" not in ir["predicate"]["filter"]


class TestRelationFilterCompile:
    def test_compiles_relation_json_operators(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        payload = {"unsafe": "'; DROP TABLE teams; --"}
        contains_sql = RelTicket.objects.filter(team__metadata___contains=payload)._compile()[
            "sql_text"
        ]
        has_key_sql = RelTicket.objects.filter(team__metadata___has_key="region")._compile()[
            "sql_text"
        ]
        has_any_sql = RelTicket.objects.filter(
            team__metadata___has_any_keys=["region", "tier"]
        )._compile()["sql_text"]

        assert '"team"."metadata" @> $1::jsonb' in contains_sql
        assert '"team"."metadata" ? $1' in has_key_sql
        assert '"team"."metadata" ?| $1' in has_any_sql
        assert "DROP TABLE" not in contains_sql

    def test_compiles_inner_join_and_qualified_where(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = RelTicket.objects.filter(team__slug="dice").limit(5)
        sql = qs._compile()["sql_text"]
        assert "INNER JOIN" in sql
        assert '"teams"' in sql
        assert '"team"."slug"' in sql
        assert "team__" not in sql  # filter-only: no SELECT projection

    def test_compiles_q_or_relation_filters(self) -> None:
        pytest.importorskip("ferrum._native", reason="Rust extension not built")
        qs = RelTicket.objects.filter(Q(team__slug="dice") | Q(team__id=42))
        sql = qs._compile()["sql_text"]
        assert "INNER JOIN" in sql
        assert " OR " in sql
        assert '"team"."slug"' in sql
        assert '"team"."id"' in sql

    def test_delete_rejects_relation_lookup(self) -> None:
        qs = RelTicket.objects.filter(team__slug="dice")
        with pytest.raises(FerrumCompileError, match="relation lookups"):
            qs._check_write_scope("delete()")
