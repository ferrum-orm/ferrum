"""Unit tests for relationship loading (select_related / prefetch_related)."""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumRelationNotLoadedError
from ferrum.relations import (
    build_join_ir,
    prefetch_related_objects,
    resolve_prefetch_name,
)


class RelUser(ferrum.Model):
    id: int = 0
    email: str = ""


class RelPost(ferrum.Model):
    id: int = 0
    author_id: int = 0
    title: str = ""
    published: bool = False
    author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="RelUser", related_name="posts", on_delete="CASCADE"
    )


class RelTag(ferrum.Model):
    id: int = 0
    name: str = ""


class RelArticle(ferrum.Model):
    id: int = 0
    tags: ClassVar[ferrum.ManyToMany] = ferrum.ManyToMany(to="RelTag", through="rel_article_tags")


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect", "quoted_table", "placeholder"),
    [
        ("postgres", '"rel_post"', "$1"),
        ("mysql", "`rel_post`", "%s"),
        ("sqlite", '"rel_post"', "?"),
        ("mssql", "[rel_post]", "?"),
    ],
)
async def test_reverse_prefetch_sql_is_dialect_aware(
    dialect: str, quoted_table: str, placeholder: str
) -> None:
    driver = MagicMock()
    driver.fetch = AsyncMock(return_value=[])
    conn = MagicMock()
    conn.dialect = dialect
    conn._require_driver.return_value = driver

    await prefetch_related_objects(
        [RelUser.model_construct(id=1, email="a@example.com")],
        RelUser,
        ("posts",),
        conn,
    )

    sql = driver.fetch.await_args.args[0]
    assert quoted_table in sql
    assert placeholder in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect", "quoted_through", "placeholder"),
    [
        ("postgres", '"rel_article_tags"', "$1"),
        ("mysql", "`rel_article_tags`", "%s"),
        ("sqlite", '"rel_article_tags"', "?"),
        ("mssql", "[rel_article_tags]", "?"),
    ],
)
async def test_m2m_prefetch_sql_is_dialect_aware(
    dialect: str, quoted_through: str, placeholder: str
) -> None:
    driver = MagicMock()
    driver.fetch = AsyncMock(return_value=[])
    conn = MagicMock()
    conn.dialect = dialect
    conn._require_driver.return_value = driver

    await prefetch_related_objects(
        [RelArticle.model_construct(id=1)],
        RelArticle,
        ("tags",),
        conn,
    )

    sql = driver.fetch.await_args.args[0]
    assert quoted_through in sql
    assert placeholder in sql


@pytest.mark.asyncio
async def test_reverse_prefetch_coerces_sqlite_integer_boolean() -> None:
    driver = MagicMock()
    driver.fetch = AsyncMock(
        return_value=[{"id": 2, "author_id": 1, "title": "post", "published": 1}]
    )
    conn = MagicMock()
    conn.dialect = "sqlite"
    conn._require_driver.return_value = driver
    user = RelUser.model_construct(id=1, email="a@example.com")

    await prefetch_related_objects([user], RelUser, ("posts",), conn)

    assert user.posts[0].published is True
