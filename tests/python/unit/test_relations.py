"""Unit tests for W2-C relation loading: bounded batching, nested prefetch,
reverse OTO, composite PK, ordering, and explicit-access enforcement.

These tests use mocks for the driver/connection so they run without a live
database. Integration tests against live PostgreSQL are in
``tests/python/integration/test_relations_bulk.py``.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumRelationNotLoadedError
from ferrum.relations import (
    _chunk_ids,
    _collect_loaded_related,
    _resolve_through_columns,
    _split_nested_prefetch,
    prefetch_related_objects,
    resolve_prefetch_name,
    safe_batch_size,
)

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


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


class RelProfile(ferrum.Model):
    id: int = 0
    user_id: int = 0
    bio: str = ""
    user: ClassVar[ferrum.OneToOne] = ferrum.OneToOne(
        to="RelUser", related_name="profile", on_delete="CASCADE"
    )


class RelTag(ferrum.Model):
    id: int = 0
    name: str = ""


class RelArticle(ferrum.Model):
    id: int = 0
    tags: ClassVar[ferrum.ManyToMany] = ferrum.ManyToMany(to="RelTag", through="rel_article_tags")


class RelComment(ferrum.Model):
    id: int = 0
    post_id: int = 0
    body: str = ""
    post: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
        to="RelPost", related_name="comments", on_delete="CASCADE"
    )


# ---------------------------------------------------------------------------
# Bounded batching helper
# ---------------------------------------------------------------------------


class TestChunkIds:
    def test_chunk_ids_respects_max_params(self) -> None:
        ids = list(range(100))
        chunks = _chunk_ids(ids, max_params=30)
        assert len(chunks) == 4  # 30 + 30 + 30 + 10
        assert sum(len(c) for c in chunks) == 100
        assert all(len(c) <= 30 for c in chunks)

    def test_chunk_ids_single_batch_when_under_limit(self) -> None:
        ids = list(range(10))
        chunks = _chunk_ids(ids, max_params=100)
        assert len(chunks) == 1
        assert chunks[0] == ids

    def test_chunk_ids_empty(self) -> None:
        assert _chunk_ids([]) == []

    def test_chunk_ids_clamps_min(self) -> None:
        chunks = _chunk_ids([1, 2, 3], max_params=0)
        assert all(len(c) <= 1 for c in chunks)


class TestSafeBatchSize:
    def test_under_limit(self) -> None:
        assert safe_batch_size(3, requested=1000) == 1000

    def test_over_limit_clamped(self) -> None:
        # 65535 / 3 = 21845
        assert safe_batch_size(3, requested=30000) == 21845

    def test_single_field(self) -> None:
        assert safe_batch_size(1, requested=1000) == 1000

    def test_zero_fields_returns_requested(self) -> None:
        assert safe_batch_size(0, requested=500) == 500

    def test_min_one(self) -> None:
        assert safe_batch_size(100000, requested=10) >= 1


# ---------------------------------------------------------------------------
# Nested prefetch name splitting
# ---------------------------------------------------------------------------


class TestSplitNestedPrefetch:
    def test_flat_name(self) -> None:
        assert _split_nested_prefetch("posts") == ("posts", None)

    def test_one_level_nested(self) -> None:
        assert _split_nested_prefetch("posts__tags") == ("posts", "tags")

    def test_two_levels_nested(self) -> None:
        assert _split_nested_prefetch("a__b__c") == ("a", "b__c")


# ---------------------------------------------------------------------------
# Prefetch name resolution
# ---------------------------------------------------------------------------


class TestResolvePrefetchName:
    def test_reverse_fk(self) -> None:
        kind, meta = resolve_prefetch_name(RelUser.get_metadata(), "posts")
        assert kind == "reverse"
        assert meta.related_model_name == "RelPost"  # type: ignore[union-attr]
        assert meta.fk_column == "author_id"  # type: ignore[union-attr]

    def test_reverse_oto(self) -> None:
        kind, meta = resolve_prefetch_name(RelUser.get_metadata(), "profile")
        assert kind == "reverse"
        assert meta.kind == "one_to_one"  # type: ignore[union-attr]

    def test_forward_m2m(self) -> None:
        kind, meta = resolve_prefetch_name(RelArticle.get_metadata(), "tags")
        assert kind == "m2m"
        assert meta.to_model == "RelTag"  # type: ignore[union-attr]

    def test_rejects_forward_fk(self) -> None:
        with pytest.raises(FerrumCompileError, match="select_related"):
            resolve_prefetch_name(RelPost.get_metadata(), "author")

    def test_unknown_relation(self) -> None:
        with pytest.raises(FerrumCompileError, match="Unknown relation"):
            resolve_prefetch_name(RelUser.get_metadata(), "nonexistent")

    def test_nested_resolves_first_level(self) -> None:
        kind, meta = resolve_prefetch_name(RelUser.get_metadata(), "posts__comments")
        assert kind == "reverse"
        assert meta.related_model_name == "RelPost"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Through-table column resolution
# ---------------------------------------------------------------------------


class TestResolveThroughColumns:
    def test_forward_m2m_columns(self) -> None:
        rel = next(r for r in RelArticle.get_metadata().relations if r.field_name == "tags")
        owner_col, target_col = _resolve_through_columns(RelArticle.get_metadata(), rel)
        assert owner_col == "rel_article_id"
        assert target_col == "rel_tag_id"

    def test_missing_through_table_raises(self) -> None:
        # Build a fake RelationMeta with no through_table
        from ferrum.models import RelationMeta

        rel = RelationMeta(field_name="x", kind="m2m", to_model="RelTag")
        with pytest.raises(FerrumCompileError, match="missing through_table"):
            _resolve_through_columns(RelArticle.get_metadata(), rel)


# ---------------------------------------------------------------------------
# Explicit access enforcement (no hidden I/O)
# ---------------------------------------------------------------------------


class TestExplicitAccess:
    def test_unloaded_forward_relation_raises(self) -> None:
        post = RelPost.model_construct(id=1, author_id=2, title="t")
        with pytest.raises(FerrumRelationNotLoadedError):
            _ = post.author

    def test_unloaded_reverse_fk_returns_unbound_queryset(self) -> None:
        """Per §5a: reverse FK/OTO may return an unbound QuerySet — no I/O."""
        pytest.importorskip("ferrum._native")
        from ferrum.queryset import QuerySet

        user = RelUser.model_construct(id=1, email="a@example.com")
        qs = user.posts
        assert isinstance(qs, QuerySet)
        # No I/O has occurred — the queryset is unbound and unexecuted.
        # Accessing the filter column proves it was filtered without a connection.
        assert qs._predicate_q is not None

    def test_unloaded_reverse_oto_returns_unbound_queryset(self) -> None:
        pytest.importorskip("ferrum._native")
        from ferrum.queryset import QuerySet

        user = RelUser.model_construct(id=1, email="a@example.com")
        qs = user.profile
        assert isinstance(qs, QuerySet)

    def test_loaded_reverse_fk_returns_cached_list(self) -> None:
        from ferrum.relations import set_relation

        user = RelUser.model_construct(id=1, email="a@example.com")
        posts = [RelPost.model_construct(id=10, author_id=1, title="a")]
        set_relation(user, "posts", posts)
        assert user.posts is posts

    def test_loaded_reverse_oto_returns_single_object(self) -> None:
        from ferrum.relations import set_relation

        user = RelUser.model_construct(id=1, email="a@example.com")
        profile = RelProfile.model_construct(id=5, user_id=1, bio="bio")
        set_relation(user, "profile", profile)
        assert user.profile is profile


# ---------------------------------------------------------------------------
# Bounded batching in prefetch
# ---------------------------------------------------------------------------


class TestBoundedPrefetch:
    @pytest.mark.asyncio
    async def test_reverse_fk_prefetch_chunks_large_parent_set(self) -> None:
        """Prefetch on >_MAX_PREFETCH_PARAMS parents issues multiple queries."""
        driver = MagicMock()
        # Return one post per parent, keyed by author_id.
        call_count = 0

        async def mock_fetch(sql, *args):
            nonlocal call_count
            call_count += 1
            rows = []
            for pid in args:
                rows.append({"id": pid * 10, "author_id": pid, "title": "t", "published": 0})
            return rows

        driver.fetch = mock_fetch
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        # Create 200 parents — with max_params=50, this needs 4 batches.
        users = [RelUser.model_construct(id=i, email=f"u{i}@x.com") for i in range(200)]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ferrum.relations._MAX_PREFETCH_PARAMS", 50)
            await prefetch_related_objects(users, RelUser, ("posts",), conn)

        assert call_count == 4
        for user in users:
            assert len(user.posts) == 1
            assert user.posts[0].author_id == user.id

    @pytest.mark.asyncio
    async def test_reverse_fk_prefetch_orders_by_pk(self) -> None:
        driver = MagicMock()
        driver.fetch = AsyncMock(
            return_value=[
                {"id": 3, "author_id": 1, "title": "c", "published": 0},
                {"id": 1, "author_id": 1, "title": "a", "published": 0},
                {"id": 2, "author_id": 1, "title": "b", "published": 0},
            ]
        )
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        user = RelUser.model_construct(id=1, email="a@example.com")
        await prefetch_related_objects([user], RelUser, ("posts",), conn)

        sql = driver.fetch.await_args.args[0]
        assert "ORDER BY" in sql
        assert '"id"' in sql

    @pytest.mark.asyncio
    async def test_m2m_prefetch_chunks_large_parent_set(self) -> None:
        driver = MagicMock()
        through_call_count = 0
        target_call_count = 0

        async def mock_fetch(sql, *args):
            nonlocal through_call_count, target_call_count
            if "rel_article_tags" in sql:
                through_call_count += 1
                return [{"rel_article_id": pid, "rel_tag_id": pid + 100} for pid in args]
            else:
                target_call_count += 1
                return [{"id": tid, "name": f"tag{tid}"} for tid in args]

        driver.fetch = mock_fetch
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        articles = [RelArticle.model_construct(id=i) for i in range(200)]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ferrum.relations._MAX_PREFETCH_PARAMS", 50)
            await prefetch_related_objects(articles, RelArticle, ("tags",), conn)

        assert through_call_count == 4
        assert target_call_count >= 1
        for article in articles:
            assert len(article.tags) == 1
            assert article.tags[0].id == article.id + 100

    @pytest.mark.asyncio
    async def test_m2m_prefetch_empty_target_ids(self) -> None:
        driver = MagicMock()
        driver.fetch = AsyncMock(return_value=[])
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        article = RelArticle.model_construct(id=1)
        await prefetch_related_objects([article], RelArticle, ("tags",), conn)
        assert article.tags == []


# ---------------------------------------------------------------------------
# Reverse OTO prefetch stores single object
# ---------------------------------------------------------------------------


class TestReverseOtoPrefetch:
    @pytest.mark.asyncio
    async def test_reverse_oto_stores_single_object(self) -> None:
        driver = MagicMock()
        driver.fetch = AsyncMock(return_value=[{"id": 5, "user_id": 1, "bio": "hello"}])
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        user = RelUser.model_construct(id=1, email="a@example.com")
        await prefetch_related_objects([user], RelUser, ("profile",), conn)

        assert isinstance(user.profile, RelProfile)
        assert user.profile.bio == "hello"

    @pytest.mark.asyncio
    async def test_reverse_oto_stores_none_when_empty(self) -> None:
        driver = MagicMock()
        driver.fetch = AsyncMock(return_value=[])
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        user = RelUser.model_construct(id=1, email="a@example.com")
        await prefetch_related_objects([user], RelUser, ("profile",), conn)

        assert user.profile is None


# ---------------------------------------------------------------------------
# Nested prefetch
# ---------------------------------------------------------------------------


class TestNestedPrefetch:
    @pytest.mark.asyncio
    async def test_nested_prefetch_loads_two_levels(self) -> None:
        """prefetch_related('posts__comments') loads posts, then comments on posts."""
        call_count = 0

        async def mock_fetch(sql, *args):
            nonlocal call_count
            call_count += 1
            if "rel_post" in sql.lower() and "rel_comment" not in sql.lower():
                # First-level: reverse FK posts
                return [
                    {"id": 10, "author_id": 1, "title": "p1", "published": 0},
                    {"id": 20, "author_id": 1, "title": "p2", "published": 0},
                ]
            else:
                # Second-level: reverse FK comments on posts
                results = []
                for pid in args:
                    results.append({"id": pid * 100, "post_id": pid, "body": f"c{pid}"})
                return results

        driver = MagicMock()
        driver.fetch = mock_fetch
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        user = RelUser.model_construct(id=1, email="a@example.com")
        await prefetch_related_objects([user], RelUser, ("posts__comments",), conn)

        assert call_count == 2  # one for posts, one for comments
        assert len(user.posts) == 2
        assert len(user.posts[0].comments) == 1
        assert user.posts[0].comments[0].body == "c10"
        assert len(user.posts[1].comments) == 1
        assert user.posts[1].comments[0].body == "c20"

    @pytest.mark.asyncio
    async def test_nested_prefetch_shared_prefix(self) -> None:
        """prefetch_related('posts', 'posts__comments') loads posts once."""
        call_count = 0

        async def mock_fetch(sql, *args):
            nonlocal call_count
            call_count += 1
            if "rel_comment" in sql.lower():
                results = []
                for pid in args:
                    results.append({"id": pid * 100, "post_id": pid, "body": f"c{pid}"})
                return results
            else:
                return [
                    {"id": 10, "author_id": 1, "title": "p1", "published": 0},
                ]

        driver = MagicMock()
        driver.fetch = mock_fetch
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        user = RelUser.model_construct(id=1, email="a@example.com")
        await prefetch_related_objects([user], RelUser, ("posts", "posts__comments"), conn)

        # posts loaded once (call 1), comments loaded once (call 2)
        assert call_count == 2
        assert len(user.posts) == 1
        assert len(user.posts[0].comments) == 1


class TestCollectLoadedRelated:
    def test_collect_from_list(self) -> None:
        from ferrum.relations import set_relation

        user1 = RelUser.model_construct(id=1, email="a")
        user2 = RelUser.model_construct(id=2, email="b")
        posts1 = [RelPost.model_construct(id=10, author_id=1, title="x")]
        posts2 = [RelPost.model_construct(id=20, author_id=2, title="y")]
        set_relation(user1, "posts", posts1)
        set_relation(user2, "posts", posts2)

        from ferrum.relations import reverse_for

        rev = reverse_for("RelUser")["posts"]
        result = _collect_loaded_related([user1, user2], "posts", "reverse", rev)
        assert len(result) == 2
        assert result[0].id == 10
        assert result[1].id == 20

    def test_collect_from_single_object(self) -> None:
        from ferrum.relations import reverse_for, set_relation

        user = RelUser.model_construct(id=1, email="a")
        profile = RelProfile.model_construct(id=5, user_id=1, bio="bio")
        set_relation(user, "profile", profile)

        rev = reverse_for("RelUser")["profile"]
        result = _collect_loaded_related([user], "profile", "reverse", rev)
        assert len(result) == 1
        assert result[0].id == 5

    def test_collect_skips_none(self) -> None:
        from ferrum.relations import reverse_for, set_relation

        user = RelUser.model_construct(id=1, email="a")
        set_relation(user, "profile", None)

        rev = reverse_for("RelUser")["profile"]
        result = _collect_loaded_related([user], "profile", "reverse", rev)
        assert result == []

    def test_collect_skips_unloaded(self) -> None:
        from ferrum.relations import reverse_for

        user = RelUser.model_construct(id=1, email="a")
        rev = reverse_for("RelUser")["posts"]
        result = _collect_loaded_related([user], "posts", "reverse", rev)
        assert result == []


# ---------------------------------------------------------------------------
# Dialect-aware SQL (regression coverage)
# ---------------------------------------------------------------------------


class TestDialectAwarePrefetch:
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
        self, dialect: str, quoted_table: str, placeholder: str
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
        self, dialect: str, quoted_through: str, placeholder: str
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
    async def test_reverse_prefetch_coerces_sqlite_integer_boolean(self) -> None:
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
