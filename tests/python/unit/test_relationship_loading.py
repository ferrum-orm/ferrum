"""Unit tests for relationship loading (select_related / prefetch_related)."""

from __future__ import annotations

from typing import ClassVar

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumRelationNotLoadedError
from ferrum.relations import build_join_ir, resolve_prefetch_name


class RelUser(ferrum.Model):
    id: int = 0
    email: str = ""


class RelPost(ferrum.Model):
    id: int = 0
    author_id: int = 0
    title: str = ""
    author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="RelUser", related_name="posts", on_delete="CASCADE"
    )


def test_build_join_ir_shape() -> None:
    ir = build_join_ir(
        RelPost.get_metadata(),
        "author",
        {f.name: i for i, f in enumerate(RelPost.get_metadata().fields)},
    )
    assert ir["relation"] == "author"
    assert ir["remote_table"] == RelUser.get_metadata().table_name
    assert ir["local_field"]["name"] == "author_id"


def test_prefetch_rejects_forward_fk() -> None:
    with pytest.raises(FerrumCompileError, match="select_related"):
        resolve_prefetch_name(RelPost.get_metadata(), "author")


def test_prefetch_accepts_reverse_accessor() -> None:
    kind, meta = resolve_prefetch_name(RelUser.get_metadata(), "posts")
    assert kind == "reverse"
    assert meta.related_model_name == "RelPost"  # type: ignore[union-attr]


def test_unloaded_forward_relation_raises() -> None:
    post = RelPost.model_construct(id=1, author_id=2, title="t")
    with pytest.raises(FerrumRelationNotLoadedError):
        _ = post.author


def test_select_related_compiles_join() -> None:
    pytest.importorskip("ferrum._native")
    compiled = RelPost.objects.select_related("author")._compile()
    assert "LEFT JOIN" in compiled["sql_text"]
    assert "author__" in compiled["sql_text"]
