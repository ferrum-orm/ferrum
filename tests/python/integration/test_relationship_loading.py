"""Integration tests for select_related and prefetch_related."""

from __future__ import annotations

from typing import ClassVar

import pytest

import ferrum

from .helpers import transient_table


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


_AUTHOR = """
    CREATE TABLE "{t}" (
        id SERIAL PRIMARY KEY,
        email TEXT NOT NULL
    )
"""

_POST = """
    CREATE TABLE "{t}" (
        id SERIAL PRIMARY KEY,
        author_id INTEGER NOT NULL REFERENCES "{a}"(id),
        title TEXT NOT NULL
    )
"""


@pytest.mark.integration
async def test_select_related_populates_author(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    author_table = f"ferrum_int_rel_author_{unique_suffix}"
    post_table = f"ferrum_int_rel_post_{unique_suffix}"
    Author = _author_model(author_table)
    Post = _post_model(post_table, Author.__name__)

    async with transient_table(
        pg_conn,
        create_sql=_AUTHOR.format(t=author_table),
        drop_sql=f'DROP TABLE "{author_table}"',
    ), transient_table(
        pg_conn,
        create_sql=_POST.format(t=post_table, a=author_table),
        drop_sql=f'DROP TABLE "{post_table}"',
    ):
        author = await Author.objects.create(pg_conn, email="a@example.com")
        post = await Post.objects.create(pg_conn, author_id=author.id, title="hello")
        loaded = await Post.objects.filter(id=post.id).select_related("author").all(pg_conn)
        assert len(loaded) == 1
        assert loaded[0].author.email == "a@example.com"


@pytest.mark.integration
async def test_prefetch_related_populates_reverse_posts(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    author_table = f"ferrum_int_rel_rev_author_{unique_suffix}"
    post_table = f"ferrum_int_rel_rev_post_{unique_suffix}"
    Author = _author_model(author_table)
    Post = _post_model(post_table, Author.__name__)

    async with transient_table(
        pg_conn,
        create_sql=_AUTHOR.format(t=author_table),
        drop_sql=f'DROP TABLE "{author_table}"',
    ), transient_table(
        pg_conn,
        create_sql=_POST.format(t=post_table, a=author_table),
        drop_sql=f'DROP TABLE "{post_table}"',
    ):
        author = await Author.objects.create(pg_conn, email="u@example.com")
        await Post.objects.create(pg_conn, author_id=author.id, title="one")
        await Post.objects.create(pg_conn, author_id=author.id, title="two")
        users = await Author.objects.filter(id=author.id).prefetch_related("posts").all(pg_conn)
        assert len(users) == 1
        assert len(users[0].posts) == 2
        titles = {p.title for p in users[0].posts}
        assert titles == {"one", "two"}
