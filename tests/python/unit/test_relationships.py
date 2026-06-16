"""Unit tests for relationship metadata and migration planning."""

from __future__ import annotations

from typing import ClassVar

import pytest

import ferrum
from ferrum.errors import FerrumMigrationError
from ferrum.migrations.operations import DropForeignKey
from ferrum.migrations.orchestrator import _op_to_sql, apply, compute_plan


def _ops_by_kind(plan: dict, kind: str) -> list[dict]:
    return [op for op in plan["ops"] if op["kind"] == kind]


def test_relationship_descriptors_produce_relation_meta_entries() -> None:
    class RelAuthor(ferrum.Model):
        id: int

    class RelProfile(ferrum.Model):
        id: int
        owner: ClassVar[ferrum.OneToOne] = ferrum.OneToOne(to="RelAuthor")

    class RelPost(ferrum.Model):
        id: int
        author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(to="RelAuthor")
        tags: ClassVar[ferrum.ManyToMany] = ferrum.ManyToMany(to="RelTag")

    class RelTag(ferrum.Model):
        id: int

    post_rels = {rel.field_name: rel for rel in RelPost.get_metadata().relations}
    profile_rels = {rel.field_name: rel for rel in RelProfile.get_metadata().relations}

    assert post_rels["author"].kind == "fk"
    assert post_rels["author"].to_model == RelAuthor.__name__
    assert post_rels["tags"].kind == "m2m"
    assert post_rels["tags"].through_table == "rel_post_rel_tag"
    assert profile_rels["owner"].kind == "one_to_one"


def test_fk_backing_column_defaults_to_field_name_id_and_is_auto_added() -> None:
    class RelAutoAuthor(ferrum.Model):
        id: int

    class RelAutoPost(ferrum.Model):
        id: int
        author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(to="RelAutoAuthor")

    meta = RelAutoPost.get_metadata()
    relation = meta.relations[0]
    backing_field = next(field for field in meta.fields if field.name == "author_id")

    assert relation.db_column == "author_id"
    assert backing_field.column_name == "author_id"
    assert backing_field.field_type == "int"


def test_fk_db_column_override_is_honoured() -> None:
    class RelCustomAuthor(ferrum.Model):
        id: int

    class RelCustomPost(ferrum.Model):
        id: int
        author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
            to="RelCustomAuthor",
            db_column="created_by_id",
        )

    meta = RelCustomPost.get_metadata()

    assert meta.relations[0].db_column == "created_by_id"
    assert any(field.column_name == "created_by_id" for field in meta.fields)


def test_invalid_on_delete_raises_at_metadata_build_time() -> None:
    with pytest.raises(ValueError, match="Invalid on_delete"):

        class RelInvalidDeletePost(ferrum.Model):
            id: int
            owner: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
                to="RelInvalidDeleteUser",
                on_delete="DROP TABLE users",
            )


def test_compute_plan_emits_add_fk_for_fk_and_one_to_one() -> None:
    class RelPlanUser(ferrum.Model):
        id: int

    class RelPlanPost(ferrum.Model):
        id: int
        author: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(to="RelPlanUser")

    class RelPlanProfile(ferrum.Model):
        id: int
        user: ClassVar[ferrum.OneToOne] = ferrum.OneToOne(
            to="RelPlanUser",
            on_delete="SET NULL",
        )

    plan = compute_plan(models=[RelPlanUser, RelPlanPost, RelPlanProfile], conn=None)
    fk_ops = _ops_by_kind(plan, "add_fk")

    assert {(op["table"], op["column"], op["ref_table"], op["on_delete"]) for op in fk_ops} == {
        ("rel_plan_post", "author_id", "rel_plan_user", "CASCADE"),
        ("rel_plan_profile", "user_id", "rel_plan_user", "SET NULL"),
    }


def test_many_to_many_emits_deduplicated_join_table_and_two_fks() -> None:
    class RelM2MPost(ferrum.Model):
        id: int
        tags: ClassVar[ferrum.ManyToMany] = ferrum.ManyToMany(
            to="RelM2MTag",
            through="rel_m2m_post_tags",
        )

    class RelM2MTag(ferrum.Model):
        id: int
        posts: ClassVar[ferrum.ManyToMany] = ferrum.ManyToMany(
            to="RelM2MPost",
            through="rel_m2m_post_tags",
        )

    plan = compute_plan(models=[RelM2MPost, RelM2MTag], conn=None)
    join_creates = [
        op for op in _ops_by_kind(plan, "create_table") if op["table"] == "rel_m2m_post_tags"
    ]
    join_fks = [op for op in _ops_by_kind(plan, "add_fk") if op["table"] == "rel_m2m_post_tags"]

    assert len(join_creates) == 1
    assert len(join_fks) == 2
    assert {op["column"] for op in join_fks} == {"rel_m2_m_post_id", "rel_m2_m_tag_id"}


def test_add_fk_sql_validates_on_delete_allowlist_and_quotes_identifiers() -> None:
    sql = _op_to_sql(
        {
            "kind": "add_fk",
            "table": "order item",
            "name": "fk order user",
            "column": "created by",
            "ref_table": "user account",
            "ref_column": "id",
            "on_delete": "RESTRICT",
        }
    )

    assert sql == (
        'ALTER TABLE "order item" ADD CONSTRAINT "fk order user" '
        'FOREIGN KEY ("created by") REFERENCES "user account" ("id") ON DELETE RESTRICT'
    )

    with pytest.raises(FerrumMigrationError, match="Unsupported ON DELETE"):
        _op_to_sql(
            {
                "kind": "add_fk",
                "table": "orders",
                "name": "fk_orders_user_id",
                "column": "user_id",
                "ref_table": "users",
                "ref_column": "id",
                "on_delete": "CASCADE; DROP TABLE users",
            }
        )


@pytest.mark.asyncio
async def test_drop_fk_is_destructive_and_blocked_without_confirm() -> None:
    class _Pool:
        async def execute(self, sql: str) -> None:
            raise AssertionError(f"destructive SQL should be gated before execute: {sql}")

    class _Conn:
        def _require_pool(self) -> _Pool:
            return _Pool()

    assert DropForeignKey("orders", "fk_orders_user_id").classification == "destructive"

    plan_json = (
        '{"ops":[{"kind":"drop_fk","table":"orders","name":"fk_orders_user_id"}],'
        '"requires_confirmation":false}'
    )
    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(_Conn(), plan_json, dry_run=False, confirm=False)
