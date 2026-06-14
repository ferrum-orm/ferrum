"""Unit tests for declarative Meta.indexes and AddIndex DDL emission."""

from __future__ import annotations

from typing import ClassVar

import pytest

import ferrum
from ferrum.migrations.operations import AddIndex
from ferrum.migrations.orchestrator import _op_to_sql, compute_plan


class TestIndexMetadata:
    def test_composite_index_name_derived(self) -> None:
        class IndexedPost(ferrum.Model):
            id: int
            author_id: int
            published_at: str

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(fields=("author_id", "published_at"))
                ]

        meta = IndexedPost.get_metadata()
        assert len(meta.indexes) == 1
        assert meta.indexes[0].name == "idx_indexed_post_author_id_published_at"
        assert meta.indexes[0].fields == ("author_id", "published_at")

    def test_explicit_index_name(self) -> None:
        class NamedIndex(ferrum.Model):
            id: int
            slug: str

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(fields=("slug",), name="idx_custom_slug")
                ]

        assert NamedIndex.get_metadata().indexes[0].name == "idx_custom_slug"

    def test_unique_index(self) -> None:
        class UniqueIndex(ferrum.Model):
            id: int
            code: str

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(fields=("code",), unique=True)
                ]

        assert UniqueIndex.get_metadata().indexes[0].unique is True

    def test_partial_index_with_using(self) -> None:
        class PartialIndex(ferrum.Model):
            id: int
            body: str
            active: bool

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(
                        fields=("body",),
                        using="gin",
                        where="active = true",
                    )
                ]

        idx = PartialIndex.get_metadata().indexes[0]
        assert idx.using == "gin"
        assert idx.where == "active = true"

    def test_unknown_field_raises_at_definition(self) -> None:
        with pytest.raises(ValueError, match="unknown field"):

            class BadIndex(ferrum.Model):
                id: int

                class Meta:
                    indexes: ClassVar[list[ferrum.Index]] = [ferrum.Index(fields=("missing",))]


class TestComputePlanIndexes:
    def test_meta_indexes_emitted_after_create_table(self) -> None:
        class PlanIndexed(ferrum.Model):
            id: int
            email: str

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(fields=("email",), using="hash")
                ]

        plan = compute_plan([PlanIndexed], existing_tables={})
        kinds = [op["kind"] for op in plan["ops"]]
        assert kinds.index("create_table") < kinds.index("add_index")
        index_op = next(op for op in plan["ops"] if op["kind"] == "add_index")
        assert index_op["using"] == "hash"
        assert index_op["columns"] == ["email"]


class TestAddIndexSql:
    def test_using_and_where_in_sql(self) -> None:
        op = AddIndex(
            "posts",
            "idx_posts_body_gin",
            ["body"],
            using="gin",
            where="active = true",
        ).to_op_dict()
        sql = _op_to_sql(op)
        assert "USING gin" in sql
        assert "WHERE active = true" in sql

    def test_unique_index_sql(self) -> None:
        op = AddIndex("users", "idx_users_email", ["email"], unique=True).to_op_dict()
        sql = _op_to_sql(op)
        assert "CREATE UNIQUE INDEX" in sql

    def test_invalid_using_raises(self) -> None:
        with pytest.raises(Exception, match="FERR-M001"):
            _op_to_sql(
                {
                    "kind": "add_index",
                    "table": "t",
                    "name": "idx_t_x",
                    "columns": ["x"],
                    "using": "evil",
                }
            )

    def test_gin_on_text_emits_trgm_opclass(self) -> None:
        class GINText(ferrum.Model):
            id: int
            body: str

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(fields=("body",), using="gin")
                ]

        plan = compute_plan([GINText], existing_tables={})
        index_op = next(op for op in plan["ops"] if op["kind"] == "add_index")
        assert index_op["opclasses"] == ["gin_trgm_ops"]
        sql = _op_to_sql(index_op)
        assert '"body" gin_trgm_ops' in sql

    def test_gin_on_tsvector_has_no_opclass(self) -> None:
        class GINTs(ferrum.Model):
            id: int
            search: ferrum.TSVector

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(fields=("search",), using="gin")
                ]

        plan = compute_plan([GINTs], existing_tables={})
        index_op = next(op for op in plan["ops"] if op["kind"] == "add_index")
        assert "opclasses" not in index_op
        sql = _op_to_sql(index_op)
        assert '"search"' in sql
        assert "gin_trgm_ops" not in sql


class TestMakemigrationsAddIndexSource:
    def test_renders_gin_index(self) -> None:
        from ferrum.cli.makemigrations_cmd import _op_to_source

        line = _op_to_source(
            {
                "kind": "add_index",
                "table": "documents",
                "name": "idx_documents_title",
                "columns": ["title"],
                "unique": False,
                "using": "gin",
            }
        )
        assert line == (
            "        ops.AddIndex('documents', 'idx_documents_title', ['title'], using='gin'),"
        )

    def test_renders_partial_unique_index(self) -> None:
        from ferrum.cli.makemigrations_cmd import _op_to_source

        line = _op_to_source(
            {
                "kind": "add_index",
                "table": "posts",
                "name": "idx_posts_created",
                "columns": ["created_at"],
                "unique": True,
                "using": "btree",
                "where": "published = true",
            }
        )
        assert "unique=True" in line
        assert "where='published = true'" in line
        assert "using=" not in line  # btree is omitted (default)
