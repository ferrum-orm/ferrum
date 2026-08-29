"""Integration tests for select_related and prefetch_related."""

from __future__ import annotations

from typing import ClassVar

import pytest

import ferrum

from .backends import Backend
from .schema import Column, transient_table


def _author_model(table_name: str) -> type[ferrum.Model]:
    class Author(ferrum.Model):
        id: int = 0
        email: str = ""

        class Meta:
            table = table_name

    return Author


def _post_model(table_name: str, author_model_name: str) -> type[ferrum.Model]:
    class Post(ferrum.Model):
        id: int = 0
        author_id: int = 0
        title: str = ""
        author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to=author_model_name,
            related_name="posts",
            on_delete="CASCADE",
        )

        class Meta:
            table = table_name

    return Post


@pytest.mark.integration
async def test_select_related_populates_author(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    author_table = f"ferrum_int_rel_author_{unique_suffix}"
    post_table = f"ferrum_int_rel_post_{unique_suffix}"
    Author = _author_model(author_table)
    Post = _post_model(post_table, Author.__name__)
    q = backend.quote

    async with (
        transient_table(
            db_conn,
            author_table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("email", "text", null=False),
            ],
        ),
        transient_table(
            db_conn,
            post_table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column(
                    "author_id",
                    "int",
                    null=False,
                    extra=f"REFERENCES {q(author_table)}({q('id')})",
                ),
                Column("title", "text", null=False),
            ],
        ),
    ):
        author = await Author.objects.create(db_conn, email="a@example.com")
        post = await Post.objects.create(db_conn, author_id=author.id, title="hello")
        loaded = await Post.objects.filter(id=post.id).select_related("author").all(db_conn)
        assert len(loaded) == 1
        assert loaded[0].author.email == "a@example.com"


@pytest.mark.integration
async def test_prefetch_related_populates_reverse_posts(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    author_table = f"ferrum_int_rel_rev_author_{unique_suffix}"
    post_table = f"ferrum_int_rel_rev_post_{unique_suffix}"
    Author = _author_model(author_table)
    Post = _post_model(post_table, Author.__name__)
    q = backend.quote

    async with (
        transient_table(
            db_conn,
            author_table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("email", "text", null=False),
            ],
        ),
        transient_table(
            db_conn,
            post_table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column(
                    "author_id",
                    "int",
                    null=False,
                    extra=f"REFERENCES {q(author_table)}({q('id')})",
                ),
                Column("title", "text", null=False),
            ],
        ),
    ):
        author = await Author.objects.create(db_conn, email="u@example.com")
        await Post.objects.create(db_conn, author_id=author.id, title="one")
        await Post.objects.create(db_conn, author_id=author.id, title="two")
        users = await Author.objects.filter(id=author.id).prefetch_related("posts").all(db_conn)
        assert len(users) == 1
        assert len(users[0].posts) == 2
        titles = {p.title for p in users[0].posts}
        assert titles == {"one", "two"}
