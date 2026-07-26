"""Unit tests for ``QuerySet.project()``."""

from __future__ import annotations

from typing import Annotated

import pytest

import ferrum
from ferrum.errors import FerrumCompileError


class Ticket(ferrum.Model):
    model_config = ferrum.ModelConfig(table="tickets")

    id: int = 0
    title: str = ""
    summary_embedding: Annotated[ferrum.Vector, ferrum.Field(vector_dimensions=3)] | None = None


class TicketRead(ferrum.Model):
    model_config = ferrum.ModelConfig(table="tickets")

    id: int = 0
    title: str = ""


class OtherTable(ferrum.Model):
    model_config = ferrum.ModelConfig(table="other")

    id: int = 0


class TestProject:
    def test_project_restricts_select_to_shared_fields(self) -> None:
        qs = Ticket.objects.nearest_to("summary_embedding", [0.1, 0.2, 0.3]).project(TicketRead)
        ir = qs._build_ir()
        names = [f["name"] for f in ir["operation"]["fields"]]
        assert "id" in names
        assert "title" in names
        assert "summary_embedding" not in names
        assert qs._hydrate_model is TicketRead
        # Source model retained for IR (nearest_to field indices).
        assert qs._model is Ticket
        assert "vector_order_by" in ir

    def test_project_rejects_different_table(self) -> None:
        with pytest.raises(FerrumCompileError, match="same table"):
            Ticket.objects.project(OtherTable)

    def test_project_chain_preserves_filters_and_limit(self) -> None:
        qs = (
            Ticket.objects.filter(title__icontains="bug")
            .nearest_to("summary_embedding", [1.0, 0.0, 0.0], metric="cosine")
            .order_by("-id")
            .limit(10)
            .project(TicketRead)
        )
        ir = qs._build_ir()
        assert ir["limit"] == 10
        assert ir["vector_order_by"]["metric"] == "cosine"
        assert ir["order_by"][0]["field"]["name"] == "id"
        assert ir["order_by"][0]["direction"] == "desc"
