"""Integration tests for W2-C: reverse relation loading, nested prefetch,
bounded batching, bulk composite-key operations, and cascade behavior.

These tests run against a live PostgreSQL instance via ``pg_conn``.
Multi-backend tests use ``db_conn`` + ``backend`` where the feature is
portable (reverse FK prefetch, bulk create/update/delete).
PostgreSQL-specific features (upsert, M2M through tables) use ``pg_conn``.
"""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest

import ferrum

from .backends import Backend
from .helpers import transient_table as pg_transient_table
from .schema import Column, transient_table

# ---------------------------------------------------------------------------
# Model factories (unique table names per test to avoid collisions)
# ---------------------------------------------------------------------------


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


def _profile_model(table_name: str, author_model_name: str) -> type[ferrum.Model]:
    class Profile(ferrum.Model):
        id: int = 0
        user_id: int = 0
        bio: str = ""
        user: ClassVar[ferrum.OneToOne] = ferrum.OneToOne(
            to=author_model_name,
            related_name="profile",
            on_delete="CASCADE",
        )

        class Meta:
            table = table_name

    return Profile


def _comment_model(table_name: str, post_model_name: str) -> type[ferrum.Model]:
    class Comment(ferrum.Model):
        id: int = 0
        post_id: int = 0
        body: str = ""
        post: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to=post_model_name,
            related_name="comments",
            on_delete="CASCADE",
        )

        class Meta:
            table = table_name

    return Comment


# ---------------------------------------------------------------------------
# Reverse FK prefetch (multi-backend)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_prefetch_reverse_fk_populates_posts(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    author_table = f"ferrum_w2c_rev_fk_author_{unique_suffix}"
    post_table = f"ferrum_w2c_rev_fk_post_{unique_suffix}"
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


@pytest.mark.integration
async def test_prefetch_reverse_fk_ordered_by_pk(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Prefetched reverse FK rows are ordered by PK for deterministic results."""
    author_table = f"ferrum_w2c_order_author_{unique_suffix}"
    post_table = f"ferrum_w2c_order_post_{unique_suffix}"
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
        # Insert in reverse order to test ORDER BY
        p3 = await Post.objects.create(db_conn, author_id=author.id, title="three")
        p1 = await Post.objects.create(db_conn, author_id=author.id, title="one")
        p2 = await Post.objects.create(db_conn, author_id=author.id, title="two")
        users = await Author.objects.filter(id=author.id).prefetch_related("posts").all(db_conn)
        posts = users[0].posts
        assert [p.id for p in posts] == sorted([p1.id, p2.id, p3.id])


# ---------------------------------------------------------------------------
# Reverse one-to-one prefetch (multi-backend)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_prefetch_reverse_oto_stores_single_object(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Reverse OTO prefetch stores a single Profile, not a list."""
    author_table = f"ferrum_w2c_oto_author_{unique_suffix}"
    profile_table = f"ferrum_w2c_oto_profile_{unique_suffix}"
    Author = _author_model(author_table)
    Profile = _profile_model(profile_table, Author.__name__)
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
            profile_table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column(
                    "user_id",
                    "int",
                    null=True,
                    extra=f"REFERENCES {q(author_table)}({q('id')})",
                ),
                Column("bio", "text", null=False),
            ],
        ),
    ):
        author = await Author.objects.create(db_conn, email="u@example.com")
        await Profile.objects.create(db_conn, user_id=author.id, bio="hello")
        users = await Author.objects.filter(id=author.id).prefetch_related("profile").all(db_conn)
        assert len(users) == 1
        assert isinstance(users[0].profile, Profile)
        assert users[0].profile.bio == "hello"


@pytest.mark.integration
async def test_prefetch_reverse_oto_none_when_no_profile(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Reverse OTO prefetch stores None when no related row exists."""
    author_table = f"ferrum_w2c_oto_none_author_{unique_suffix}"
    profile_table = f"ferrum_w2c_oto_none_profile_{unique_suffix}"
    Author = _author_model(author_table)
    _profile_model(profile_table, Author.__name__)
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
            profile_table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column(
                    "user_id",
                    "int",
                    null=True,
                    extra=f"REFERENCES {q(author_table)}({q('id')})",
                ),
                Column("bio", "text", null=False),
            ],
        ),
    ):
        author = await Author.objects.create(db_conn, email="u@example.com")
        users = await Author.objects.filter(id=author.id).prefetch_related("profile").all(db_conn)
        assert len(users) == 1
        assert users[0].profile is None


# ---------------------------------------------------------------------------
# Nested prefetch (multi-backend)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_nested_prefetch_two_levels(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """prefetch_related('posts__comments') loads posts then comments on each post."""
    author_table = f"ferrum_w2c_nested_author_{unique_suffix}"
    post_table = f"ferrum_w2c_nested_post_{unique_suffix}"
    comment_table = f"ferrum_w2c_nested_comment_{unique_suffix}"
    Author = _author_model(author_table)
    Post = _post_model(post_table, Author.__name__)
    Comment = _comment_model(comment_table, Post.__name__)
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
        transient_table(
            db_conn,
            comment_table,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column(
                    "post_id",
                    "int",
                    null=False,
                    extra=f"REFERENCES {q(post_table)}({q('id')})",
                ),
                Column("body", "text", null=False),
            ],
        ),
    ):
        author = await Author.objects.create(db_conn, email="u@example.com")
        post1 = await Post.objects.create(db_conn, author_id=author.id, title="p1")
        post2 = await Post.objects.create(db_conn, author_id=author.id, title="p2")
        await Comment.objects.create(db_conn, post_id=post1.id, body="c1a")
        await Comment.objects.create(db_conn, post_id=post1.id, body="c1b")
        await Comment.objects.create(db_conn, post_id=post2.id, body="c2a")

        users = await (
            Author.objects.filter(id=author.id).prefetch_related("posts__comments").all(db_conn)
        )
        assert len(users) == 1
        posts = users[0].posts
        assert len(posts) == 2
        # post1 has 2 comments, post2 has 1 comment
        post_by_title = {p.title: p for p in posts}
        assert len(post_by_title["p1"].comments) == 2
        assert len(post_by_title["p2"].comments) == 1
        bodies = {c.body for c in post_by_title["p1"].comments}
        assert bodies == {"c1a", "c1b"}


# ---------------------------------------------------------------------------
# M2M prefetch (PostgreSQL — through tables)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_m2m_prefetch_forward(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Forward M2M prefetch loads related objects via through table."""
    article_table = f"ferrum_w2c_m2m_article_{unique_suffix}"
    tag_table = f"ferrum_w2c_m2m_tag_{unique_suffix}"
    through_table = f"ferrum_w2c_m2m_at_{unique_suffix}"

    class Article(ferrum.Model):
        id: int = 0
        title: str = ""
        tags: ClassVar[ferrum.ManyToMany] = ferrum.ManyToMany(to="Tag", through=through_table)

        class Meta:
            table = article_table

    class Tag(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = tag_table

    create_sql = f"""
        CREATE TABLE "{article_table}" (id SERIAL PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE "{tag_table}" (id SERIAL PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE "{through_table}" (
            "{article_table}_id" INTEGER NOT NULL REFERENCES "{article_table}"("id"),
            "{tag_table}_id" INTEGER NOT NULL REFERENCES "{tag_table}"("id")
        );
    """
    drop_sql = f"""
        DROP TABLE IF EXISTS "{through_table}";
        DROP TABLE IF EXISTS "{article_table}";
        DROP TABLE IF EXISTS "{tag_table}";
    """

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        a1 = await Article.objects.create(pg_conn, title="a1")
        a2 = await Article.objects.create(pg_conn, title="a2")
        t1 = await Tag.objects.create(pg_conn, name="t1")
        t2 = await Tag.objects.create(pg_conn, name="t2")
        t3 = await Tag.objects.create(pg_conn, name="t3")
        # Link a1 → t1, t2; a2 → t2, t3
        await pg_conn._require_driver().execute(
            f'INSERT INTO "{through_table}" ("{article_table}_id", "{tag_table}_id") VALUES '
            f"({a1.id}, {t1.id}), ({a1.id}, {t2.id}), ({a2.id}, {t2.id}), ({a2.id}, {t3.id})"
        )

        articles = await Article.objects.prefetch_related("tags").all(pg_conn)
        assert len(articles) == 2
        article_by_title = {a.title: a for a in articles}
        tag_names_a1 = {t.name for t in article_by_title["a1"].tags}
        tag_names_a2 = {t.name for t in article_by_title["a2"].tags}
        assert tag_names_a1 == {"t1", "t2"}
        assert tag_names_a2 == {"t2", "t3"}


# ---------------------------------------------------------------------------
# Explicit access enforcement (no hidden I/O)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unloaded_forward_m2m_raises(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Accessing a forward M2M without prefetch raises, not returns the descriptor."""
    from ferrum.errors import FerrumRelationNotLoadedError

    article_table = f"ferrum_w2c_raise_article_{unique_suffix}"
    tag_table = f"ferrum_w2c_raise_tag_{unique_suffix}"
    through_table = f"ferrum_w2c_raise_at_{unique_suffix}"

    class RaiseArticle(ferrum.Model):
        id: int = 0
        tags: ClassVar[ferrum.ManyToMany] = ferrum.ManyToMany(to="RaiseTag", through=through_table)

        class Meta:
            table = article_table

    class RaiseTag(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = tag_table

    create_sql = f"""
        CREATE TABLE "{article_table}" (id SERIAL PRIMARY KEY);
        CREATE TABLE "{tag_table}" (id SERIAL PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE "{through_table}" (
            "{article_table}_id" INTEGER NOT NULL REFERENCES "{article_table}"("id"),
            "{tag_table}_id" INTEGER NOT NULL REFERENCES "{tag_table}"("id")
        );
    """
    drop_sql = f"""
        DROP TABLE IF EXISTS "{through_table}";
        DROP TABLE IF EXISTS "{article_table}";
        DROP TABLE IF EXISTS "{tag_table}";
    """

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        # Insert a row directly (model has no writable fields besides auto-PK)
        await pg_conn._require_driver().execute(
            f'INSERT INTO "{article_table}" ("id") VALUES (DEFAULT) RETURNING "id"'
        )
        loaded = await RaiseArticle.objects.all(pg_conn)
        with pytest.raises(FerrumRelationNotLoadedError):
            _ = loaded[0].tags


@pytest.mark.integration
async def test_unloaded_reverse_fk_returns_unbound_queryset(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Per §5a: reverse FK returns an unbound QuerySet — no hidden I/O."""
    from ferrum.queryset import QuerySet

    author_table = f"ferrum_w2c_ub_author_{unique_suffix}"
    post_table = f"ferrum_w2c_ub_post_{unique_suffix}"
    Author = _author_model(author_table)
    _post_model(post_table, Author.__name__)

    create_sql = f"""
        CREATE TABLE "{author_table}" (id SERIAL PRIMARY KEY, email TEXT NOT NULL);
        CREATE TABLE "{post_table}" (
            id SERIAL PRIMARY KEY,
            author_id INTEGER NOT NULL REFERENCES "{author_table}"("id"),
            title TEXT NOT NULL
        )
    """
    drop_sql = f"""
        DROP TABLE IF EXISTS "{post_table}";
        DROP TABLE IF EXISTS "{author_table}";
    """

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        author = await Author.objects.create(pg_conn, email="u@example.com")
        loaded = await Author.objects.filter(id=author.id).all(pg_conn)
        qs = loaded[0].posts
        assert isinstance(qs, QuerySet)
        # Execute with explicit conn — this is the explicit I/O path
        posts = await qs.all(pg_conn)
        assert posts == []


# ---------------------------------------------------------------------------
# Bulk operations with composite PK (PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_bulk_create_update_delete_composite_pk(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Bulk create/update/delete with composite primary key."""
    table_name = f"ferrum_w2c_composite_{unique_suffix}"

    class CompositeItem(ferrum.Model):
        tenant_id: Annotated[int, ferrum.Field(primary_key=True)]
        entity_id: Annotated[int, ferrum.Field(primary_key=True)]
        label: str = ""

        class Meta:
            table = table_name
            pk_fields = (0, 1)

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            tenant_id INTEGER NOT NULL,
            entity_id INTEGER NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (tenant_id, entity_id)
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        # bulk_create
        created = await CompositeItem.objects.bulk_create(
            pg_conn,
            [
                {"tenant_id": 1, "entity_id": 10, "label": "a"},
                {"tenant_id": 1, "entity_id": 20, "label": "b"},
                {"tenant_id": 2, "entity_id": 10, "label": "c"},
            ],
            batch_size=2,
        )
        assert len(created) == 3
        assert all(isinstance(row, CompositeItem) for row in created)

        # bulk_update
        for row in created:
            row.label = row.label.upper()
        updated = await CompositeItem.objects.bulk_update(
            pg_conn, created, ("label",), batch_size=2
        )
        assert updated == 3

        # Verify updates
        stored = await CompositeItem.objects.filter(tenant_id=1, entity_id=10).get(pg_conn)
        assert stored.label == "A"

        # bulk_delete with composite PK
        deleted = await CompositeItem.objects.bulk_delete(
            pg_conn,
            [(1, 10), (1, 20), (2, 10)],
            batch_size=2,
        )
        assert deleted == 3
        assert await CompositeItem.objects.count(pg_conn) == 0


@pytest.mark.integration
async def test_bulk_create_batch_sizing_large_rows(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """bulk_create with small batch_size issues multiple statements."""
    table_name = f"ferrum_w2c_batch_{unique_suffix}"

    class BatchItem(ferrum.Model):
        id: int = 0
        val: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            val INTEGER NOT NULL DEFAULT 0
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        rows = [{"val": i} for i in range(10)]
        created = await BatchItem.objects.bulk_create(pg_conn, rows, batch_size=3, returning=True)
        assert len(created) == 10
        assert await BatchItem.objects.count(pg_conn) == 10


@pytest.mark.integration
async def test_bulk_upsert_conflict_do_update(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """bulk_upsert with ON CONFLICT DO UPDATE upserts and updates existing rows."""
    table_name = f"ferrum_w2c_upsert_{unique_suffix}"

    class UpsertItem(ferrum.Model):
        id: int = 0
        name: str = ""
        count: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 0
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        # Initial insert
        await UpsertItem.objects.create(pg_conn, name="a", count=1)

        # Upsert: update existing, insert new
        existing = await UpsertItem.objects.filter(name="a").first(pg_conn)
        assert existing is not None
        total = await UpsertItem.objects.bulk_upsert(
            pg_conn,
            [
                {"id": existing.id, "name": "a", "count": 99},
                {"name": "b", "count": 2},
            ],
            conflict_fields=["id"],
            update_fields=["name", "count"],
            batch_size=100,
        )
        assert total == 2

        all_items = await UpsertItem.objects.all(pg_conn)
        assert len(all_items) == 2
        by_name = {item.name: item for item in all_items}
        assert by_name["a"].count == 99
        assert by_name["b"].count == 2


@pytest.mark.integration
async def test_bulk_upsert_do_nothing(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """bulk_upsert with DO NOTHING skips conflicting rows."""
    table_name = f"ferrum_w2c_dn_{unique_suffix}"

    class DNItem(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        await DNItem.objects.create(pg_conn, name="first")
        existing = await DNItem.objects.filter(name="first").first(pg_conn)
        assert existing is not None

        total = await DNItem.objects.bulk_upsert(
            pg_conn,
            [
                {"id": existing.id, "name": "first"},  # conflict → skip
                {"name": "second"},  # new → insert
            ],
            conflict_fields=["id"],
            update_fields=[],
            batch_size=100,
        )
        assert total == 2  # count mode returns total attempted, not affected
        all_items = await DNItem.objects.all(pg_conn)
        assert len(all_items) == 2


# ---------------------------------------------------------------------------
# Cascade behavior — database-driven (PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cascade_delete_propagates_via_database(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """ON DELETE CASCADE is enforced by PostgreSQL, not by Ferrum Python code.

    Deleting the parent row triggers the database FK cascade action on all
    referencing child rows — no Python-side cascade traversal.
    """
    author_table = f"ferrum_w2c_cascade_author_{unique_suffix}"
    post_table = f"ferrum_w2c_cascade_post_{unique_suffix}"
    Author = _author_model(author_table)
    Post = _post_model(post_table, Author.__name__)

    create_sql = f"""
        CREATE TABLE "{author_table}" (id SERIAL PRIMARY KEY, email TEXT NOT NULL);
        CREATE TABLE "{post_table}" (
            id SERIAL PRIMARY KEY,
            author_id INTEGER NOT NULL
                REFERENCES "{author_table}"("id") ON DELETE CASCADE,
            title TEXT NOT NULL
        )
    """
    drop_sql = f"""
        DROP TABLE IF EXISTS "{post_table}";
        DROP TABLE IF EXISTS "{author_table}";
    """

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        author = await Author.objects.create(pg_conn, email="u@example.com")
        await Post.objects.create(pg_conn, author_id=author.id, title="p1")
        await Post.objects.create(pg_conn, author_id=author.id, title="p2")
        assert await Post.objects.count(pg_conn) == 2

        # Delete parent — database cascades to posts
        await Author.objects.filter(id=author.id).delete(pg_conn)
        assert await Post.objects.count(pg_conn) == 0


@pytest.mark.integration
async def test_cascade_set_null_on_delete(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """ON DELETE SET NULL nullifies the FK column — database-driven."""
    parent_table = f"ferrum_w2c_sn_parent_{unique_suffix}"
    child_table = f"ferrum_w2c_sn_child_{unique_suffix}"

    class CascadeParent(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = parent_table

    class CascadeChild(ferrum.Model):
        id: int = 0
        parent_id: int | None = None
        label: str = ""
        parent: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="CascadeParent",
            db_column="parent_id",
            on_delete="SET NULL",
        )

        class Meta:
            table = child_table

    create_sql = f"""
        CREATE TABLE "{parent_table}" (id SERIAL PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE "{child_table}" (
            id SERIAL PRIMARY KEY,
            parent_id INTEGER REFERENCES "{parent_table}"("id") ON DELETE SET NULL,
            label TEXT NOT NULL DEFAULT ''
        )
    """
    drop_sql = f"""
        DROP TABLE IF EXISTS "{child_table}";
        DROP TABLE IF EXISTS "{parent_table}";
    """

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        parent = await CascadeParent.objects.create(pg_conn, name="p")
        await CascadeChild.objects.create(pg_conn, parent_id=parent.id, label="c1")
        await CascadeChild.objects.create(pg_conn, parent_id=parent.id, label="c2")

        # Delete parent — database sets parent_id to NULL on children
        await CascadeParent.objects.filter(id=parent.id).delete(pg_conn)
        children = await CascadeChild.objects.all(pg_conn)
        assert len(children) == 2
        assert all(c.parent_id is None for c in children)


# ---------------------------------------------------------------------------
# Benchmark: backfill/batch workload memory and latency
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_backfill_bulk_update_latency(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Bulk update of 500 rows completes in a reasonable time (backfill workload).

    This exercises the batched bulk_update path used by Ticket Analyzer
    backfills. Measures wall-clock latency, not a hard assertion — the goal
    is to detect regressions, not enforce a specific performance target.
    """
    import time

    table_name = f"ferrum_w2c_bench_{unique_suffix}"

    class BenchItem(ferrum.Model):
        id: int = 0
        label: str = ""
        qty: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            qty INTEGER NOT NULL DEFAULT 0
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        created = await BenchItem.objects.bulk_create(
            pg_conn,
            [{"label": f"item-{i}", "qty": i} for i in range(500)],
            batch_size=100,
        )
        assert len(created) == 500

        for row in created:
            row.qty = row.qty * 2

        t0 = time.monotonic()
        updated = await BenchItem.objects.bulk_update(pg_conn, created, ("qty",), batch_size=100)
        elapsed = time.monotonic() - t0
        assert updated == 500
        # Sanity: 500 rows in 5 batches should complete in under 10 seconds
        # (generous bound for CI variance)
        assert elapsed < 10.0, f"bulk_update took {elapsed:.2f}s for 500 rows"

        # Verify a sample
        sample = await BenchItem.objects.filter(id=created[0].id).get(pg_conn)
        assert sample.qty == 0  # 0 * 2 = 0
        sample_last = await BenchItem.objects.filter(id=created[-1].id).get(pg_conn)
        assert sample_last.qty == 499 * 2
