"""Unit tests for the extended migration autodiff (w3-c-autodiff).

Covers :mod:`ferrum.migrations.autodiff`:

- :func:`build_extended_state` replays all migration op kinds into
  :class:`AutodiffSchemaState` (FK, extension, RLS, policy, function, FTS,
  rename_column).
- :func:`compute_autodiff_plan` detects:
  - column type changes (always destructive per W1-C),
  - column renames (require explicit hints; never guesses),
  - FK add/drop on existing tables,
  - index attribute changes (drop + re-add),
  - FTS add/drop,
  - destructive conversion rejection and rename hint enforcement.
- Schema-state round-trip: replay ops → state → verify state matches expected.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest

import ferrum
from ferrum.errors import FerrumMigrationError
from ferrum.migrations import operations as ops
from ferrum.migrations.autodiff import (
    AutodiffSchemaState,
    ForeignKeyState,
    FullTextIndexState,
    build_extended_state,
    compute_autodiff_plan,
)
from ferrum.migrations.orchestrator import ColumnState, IndexState, SchemaState

# ---------------------------------------------------------------------------
# Test helpers — minimal migration module stub
# ---------------------------------------------------------------------------


class _FakeMigrationModule:
    """Minimal stub of :class:`loader.MigrationModule` for state replay tests."""

    def __init__(self, name: str, *operations: ops.Operation) -> None:
        self.name = name
        self.migration = type("Mig", (), {"operations": list(operations)})()  # type: ignore[assignment]


def _state_with_table(table: str, cols: dict[str, ColumnState]) -> AutodiffSchemaState:
    return AutodiffSchemaState(tables={table: cols})


# ---------------------------------------------------------------------------
# 1. build_extended_state — full op-kind replay
# ---------------------------------------------------------------------------


class TestBuildExtendedStateReplay:
    def test_create_table_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                "logs",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("body", "TEXT", not_null=True),
                ],
            ),
        )
        state = build_extended_state([mod])
        assert "logs" in state.tables
        assert state.tables["logs"]["id"].sql_type == "BIGSERIAL"
        assert state.tables["logs"]["id"].not_null is True
        assert state.tables["logs"]["body"].sql_type == "TEXT"

    def test_rename_column_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                "t",
                [ops.Column("id", "BIGSERIAL", not_null=True), ops.Column("old", "TEXT")],
            ),
            ops.RenameColumn("t", "old", "new"),
        )
        state = build_extended_state([mod])
        assert "new" in state.tables["t"]
        assert "old" not in state.tables["t"]
        assert state.tables["t"]["new"].sql_type == "TEXT"

    def test_add_fk_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("parent", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.CreateTable(
                "child",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True),
                    ops.Column("parent_id", "INTEGER"),
                ],
            ),
            ops.AddForeignKey("child", "fk_child_parent_id", "parent_id", "parent"),
        )
        state = build_extended_state([mod])
        assert "fk_child_parent_id" in state.foreign_keys
        assert state.foreign_keys["fk_child_parent_id"].table == "child"
        assert state.foreign_keys["fk_child_parent_id"].column == "parent_id"
        assert state.foreign_keys["fk_child_parent_id"].ref_table == "parent"

    def test_drop_fk_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("parent", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.CreateTable("child", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.AddForeignKey("child", "fk_child_parent_id", "parent_id", "parent"),
            ops.DropForeignKey("child", "fk_child_parent_id"),
        )
        state = build_extended_state([mod])
        assert "fk_child_parent_id" not in state.foreign_keys

    def test_create_extension_replayed(self) -> None:
        mod = _FakeMigrationModule("0001", ops.CreateExtension("pgcrypto"))
        state = build_extended_state([mod])
        assert "pgcrypto" in state.extensions

    def test_drop_extension_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateExtension("pgcrypto"),
            ops.DropExtension("pgcrypto"),
        )
        state = build_extended_state([mod])
        assert "pgcrypto" not in state.extensions

    def test_enable_rls_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("docs", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.EnableRLS("docs"),
        )
        state = build_extended_state([mod])
        assert state.rls_enabled.get("docs") is True

    def test_enable_rls_force_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("docs", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.EnableRLS("docs", force=True),
        )
        state = build_extended_state([mod])
        assert state.rls_enabled.get("docs") is True
        assert state.rls_forced.get("docs") is True

    def test_disable_rls_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("docs", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.EnableRLS("docs"),
            ops.DisableRLS("docs"),
        )
        state = build_extended_state([mod])
        assert "docs" not in state.rls_enabled
        assert "docs" not in state.rls_forced

    def test_create_policy_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("docs", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.CreatePolicy("docs_tenant", "docs", "tenant_id = current_setting('app.tenant_id')"),
        )
        state = build_extended_state([mod])
        assert "docs_tenant" in state.policies
        assert state.policies["docs_tenant"].table == "docs"
        assert "tenant_id" in state.policies["docs_tenant"].using

    def test_drop_policy_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("docs", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.CreatePolicy("docs_tenant", "docs", "true"),
            ops.DropPolicy("docs_tenant", "docs"),
        )
        state = build_extended_state([mod])
        assert "docs_tenant" not in state.policies

    def test_create_function_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateFunction(
                "uuidv7",
                "CREATE OR REPLACE FUNCTION uuidv7() RETURNS uuid AS $$ ... $$ LANGUAGE plpgsql",
            ),
        )
        state = build_extended_state([mod])
        assert "uuidv7" in state.functions

    def test_drop_function_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateFunction("uuidv7", "BODY"),
            ops.DropFunction("uuidv7"),
        )
        state = build_extended_state([mod])
        assert "uuidv7" not in state.functions

    def test_create_full_text_index_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                "articles",
                [ops.Column("id", "BIGSERIAL", not_null=True), ops.Column("body", "TEXT")],
            ),
            ops.CreateFullTextIndex("articles", "fts_articles_body", ["body"], config="english"),
        )
        state = build_extended_state([mod])
        assert "fts_articles_body" in state.full_text_indexes
        assert state.full_text_indexes["fts_articles_body"].table == "articles"
        assert state.full_text_indexes["fts_articles_body"].columns == ["body"]
        assert state.full_text_indexes["fts_articles_body"].config == "english"

    def test_drop_full_text_index_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("articles", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.CreateFullTextIndex("articles", "fts_articles_body", ["body"]),
            ops.DropFullTextIndex("articles", "fts_articles_body"),
        )
        state = build_extended_state([mod])
        assert "fts_articles_body" not in state.full_text_indexes

    def test_drop_table_cascade_replays(self) -> None:
        """Dropping a table cascades to its indexes, FKs, RLS, policies, FTS."""
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("docs", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.AddIndex("docs", "idx_docs_id", ["id"]),
            ops.EnableRLS("docs"),
            ops.CreatePolicy("p", "docs", "true"),
            ops.CreateFullTextIndex("docs", "fts_docs", ["id"]),
            ops.DropTable("docs"),
        )
        state = build_extended_state([mod])
        assert "docs" not in state.tables
        assert "idx_docs_id" not in state.indexes
        assert "docs" not in state.rls_enabled
        assert "p" not in state.policies
        assert "fts_docs" not in state.full_text_indexes

    def test_alter_column_type_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                "t", [ops.Column("id", "BIGSERIAL", not_null=True), ops.Column("n", "INTEGER")]
            ),
            ops.AlterColumn("t", "n", sql_type="BIGINT"),
        )
        state = build_extended_state([mod])
        assert state.tables["t"]["n"].sql_type == "BIGINT"

    def test_alter_column_drop_default_replayed(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                "t",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True),
                    ops.Column("ts", "TIMESTAMPTZ", default="NOW()"),
                ],
            ),
            ops.AlterColumn("t", "ts", drop_default=True),
        )
        state = build_extended_state([mod])
        assert state.tables["t"]["ts"].default is None

    def test_legacy_lowercase_default_normalized_in_state(self) -> None:
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable("t", [ops.Column("ts", "TIMESTAMPTZ", default="now()")]),
        )
        state = build_extended_state([mod])
        assert state.tables["t"]["ts"].default == "NOW()"


# ---------------------------------------------------------------------------
# 2. Type change detection
# ---------------------------------------------------------------------------


class TestTypeChangeDetection:
    def test_type_change_emits_destructive_alter_column(self) -> None:
        class TcA(ferrum.Model):
            id: int
            count: int  # maps to INTEGER

        # State has the column as SMALLINT; model wants INTEGER.
        state = _state_with_table(
            "tc_a",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "count": ColumnState(sql_type="SMALLINT", not_null=False),
            },
        )
        plan = compute_autodiff_plan([TcA], state)
        alter_ops = [
            op for op in plan["ops"] if op["kind"] == "alter_column" and op.get("column") == "count"
        ]
        assert any(op.get("sql_type") for op in alter_ops)
        # Destructive flag must be set (W1-C consistency).
        assert plan["destructive"] is True
        assert plan["requires_confirmation"] is True

    def test_type_change_merges_into_existing_alter(self) -> None:
        """When compute_plan already emitted alter_column for nullability, the
        type change merges into the same op (single ALTER TABLE)."""

        class TcB(ferrum.Model):
            id: int
            count: Annotated[int, ferrum.Field(nullable=False)]

        state = _state_with_table(
            "tc_b",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                # State: SMALLINT, nullable. Model: INTEGER, NOT NULL.
                "count": ColumnState(sql_type="SMALLINT", not_null=False),
            },
        )
        plan = compute_autodiff_plan([TcB], state)
        alter_ops = [
            op for op in plan["ops"] if op["kind"] == "alter_column" and op.get("column") == "count"
        ]
        # Should be a single merged alter_column with both sql_type and not_null.
        assert len(alter_ops) == 1
        assert alter_ops[0].get("sql_type") is not None
        assert alter_ops[0].get("not_null") is True

    def test_no_type_change_when_state_matches(self) -> None:
        class TcC(ferrum.Model):
            id: int
            count: int | None  # nullable=True matches state not_null=False

        state = _state_with_table(
            "tc_c",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "count": ColumnState(sql_type="INTEGER", not_null=False),
            },
        )
        plan = compute_autodiff_plan([TcC], state)
        alter_ops = [op for op in plan["ops"] if op["kind"] == "alter_column"]
        assert alter_ops == []

    def test_type_change_case_insensitive_comparison(self) -> None:
        """Type comparison is case-insensitive ('integer' vs 'INTEGER' is no change)."""

        class TcD(ferrum.Model):
            id: int
            count: int | None  # nullable matches state

        state = _state_with_table(
            "tc_d",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "count": ColumnState(sql_type="integer", not_null=False),
            },
        )
        plan = compute_autodiff_plan([TcD], state)
        alter_ops = [
            op for op in plan["ops"] if op["kind"] == "alter_column" and op.get("column") == "count"
        ]
        assert alter_ops == []


# ---------------------------------------------------------------------------
# 3. Rename detection (with and without hints)
# ---------------------------------------------------------------------------


class TestRenameDetection:
    def test_rename_with_hint_emits_rename_column(self) -> None:
        class RnA(ferrum.Model):
            id: int
            new_name: str  # was "old_name", same type TEXT

        state = _state_with_table(
            "rn_a",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "old_name": ColumnState(sql_type="TEXT", not_null=False),
            },
        )
        plan = compute_autodiff_plan([RnA], state, rename_hints={"rn_a": {"old_name": "new_name"}})
        rename_ops = [op for op in plan["ops"] if op["kind"] == "rename_column"]
        assert len(rename_ops) == 1
        assert rename_ops[0]["table"] == "rn_a"
        assert rename_ops[0]["from"] == "old_name"
        assert rename_ops[0]["to"] == "new_name"
        # No spurious add_column for the rename target.
        add_ops = [
            op for op in plan["ops"] if op["kind"] == "add_column" and op.get("name") == "new_name"
        ]
        assert add_ops == []

    def test_rename_without_hint_raises(self) -> None:
        class RnB(ferrum.Model):
            id: int
            new_name: str

        state = _state_with_table(
            "rn_b",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "old_name": ColumnState(sql_type="TEXT", not_null=False),
            },
        )
        with pytest.raises(FerrumMigrationError, match="old_name"):
            compute_autodiff_plan([RnB], state)

    def test_rename_with_incompatible_types_raises(self) -> None:
        class RnC(ferrum.Model):
            id: int
            new_name: int  # type changed from TEXT to INTEGER — incompatible

        state = _state_with_table(
            "rn_c",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "old_name": ColumnState(sql_type="TEXT", not_null=False),
            },
        )
        with pytest.raises(FerrumMigrationError, match="incompatible types"):
            compute_autodiff_plan([RnC], state, rename_hints={"rn_c": {"old_name": "new_name"}})

    def test_rename_hint_target_not_in_model_raises(self) -> None:
        class RnD(ferrum.Model):
            id: int
            actual_new: str

        state = _state_with_table(
            "rn_d",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "old_name": ColumnState(sql_type="TEXT", not_null=False),
            },
        )
        # Hint says old_name -> wrong_target, but model has actual_new.
        with pytest.raises(FerrumMigrationError, match="does not match"):
            compute_autodiff_plan([RnD], state, rename_hints={"rn_d": {"old_name": "wrong_target"}})


# ---------------------------------------------------------------------------
# 4. Foreign-key add/drop on existing tables
# ---------------------------------------------------------------------------


class TestForeignKeyAutodiff:
    def test_new_fk_on_existing_table_emits_add_fk(self) -> None:
        class FkParent(ferrum.Model):
            id: int

        class FkChild(ferrum.Model):
            id: int
            parent_id: int
            parent: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
                to="FkParent", on_delete="CASCADE"
            )

        state = AutodiffSchemaState(
            tables={
                "fk_parent": {"id": ColumnState(sql_type="BIGSERIAL", not_null=True)},
                "fk_child": {
                    "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                    "parent_id": ColumnState(sql_type="INTEGER", not_null=False),
                },
            }
        )
        plan = compute_autodiff_plan([FkParent, FkChild], state)
        add_fk_ops = [op for op in plan["ops"] if op["kind"] == "add_fk"]
        assert any(op["name"] == "fk_fk_child_parent_id" for op in add_fk_ops)

    def test_removed_fk_emits_drop_fk(self) -> None:
        class FkKeep(ferrum.Model):
            id: int

        class FkTarget(ferrum.Model):
            id: int
            keep_id: int
            keep: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(to="FkKeep", on_delete="CASCADE")

        state = AutodiffSchemaState(
            tables={
                "fk_keep": {"id": ColumnState(sql_type="BIGSERIAL", not_null=True)},
                "fk_target": {
                    "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                    "keep_id": ColumnState(sql_type="INTEGER", not_null=False),
                },
            },
            foreign_keys={
                "fk_fk_target_keep_id": ForeignKeyState(
                    table="fk_target", column="keep_id", ref_table="fk_keep"
                ),
                "fk_fk_target_dropped_id": ForeignKeyState(
                    table="fk_target", column="dropped_id", ref_table="fk_keep"
                ),
            },
        )
        plan = compute_autodiff_plan([FkKeep, FkTarget], state)
        drop_fk_ops = [op for op in plan["ops"] if op["kind"] == "drop_fk"]
        assert any(op["name"] == "fk_fk_target_dropped_id" for op in drop_fk_ops)
        # The kept FK should not be dropped.
        assert not any(op["name"] == "fk_fk_target_keep_id" for op in drop_fk_ops)


# ---------------------------------------------------------------------------
# 5. Index attribute changes (drop + re-add)
# ---------------------------------------------------------------------------


class TestIndexAttributeAutodiff:
    def test_index_unique_change_emits_drop_and_add(self) -> None:
        class IxA(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

            class Meta:
                indexes = (ferrum.Index(fields=("slug",), name="idx_ix_a_slug", unique=True),)

        # State has idx_ix_a_slug as non-unique; model wants unique.
        state = AutodiffSchemaState(
            tables={
                "ix_a": {
                    "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                    "slug": ColumnState(sql_type="TEXT", not_null=False),
                },
            },
            indexes={
                "idx_ix_a_slug": IndexState(
                    table="ix_a", columns=["slug"], unique=False, using="btree"
                ),
            },
        )
        plan = compute_autodiff_plan([IxA], state)
        drop_ops = [
            op
            for op in plan["ops"]
            if op["kind"] == "drop_index" and op.get("name") == "idx_ix_a_slug"
        ]
        add_ops = [
            op
            for op in plan["ops"]
            if op["kind"] == "add_index" and op.get("name") == "idx_ix_a_slug"
        ]
        assert len(drop_ops) == 1
        assert len(add_ops) == 1
        assert add_ops[0].get("unique") is True

    def test_index_no_change_when_attributes_match(self) -> None:
        class IxB(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

        state = AutodiffSchemaState(
            tables={
                "ix_b": {
                    "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                    "slug": ColumnState(sql_type="TEXT", not_null=False),
                },
            },
            indexes={
                "idx_ix_b_slug": IndexState(
                    table="ix_b", columns=["slug"], unique=False, using="btree"
                ),
            },
        )
        plan = compute_autodiff_plan([IxB], state)
        drop_ops = [op for op in plan["ops"] if op["kind"] == "drop_index"]
        add_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert drop_ops == []
        assert add_ops == []


# ---------------------------------------------------------------------------
# 6. Full-text index add/drop
# ---------------------------------------------------------------------------


class TestFtsAutodiff:
    def test_new_fts_index_on_existing_table_emits_create(self) -> None:
        class FtsA(ferrum.Model):
            id: int
            body: str

            class Meta:
                full_text_indexes = (ferrum.FullTextIndex(fields=("body",), name="fts_fts_a_body"),)

        state = AutodiffSchemaState(
            tables={
                "fts_a": {
                    "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                    "body": ColumnState(sql_type="TEXT", not_null=False),
                },
            },
        )
        plan = compute_autodiff_plan([FtsA], state)
        fts_ops = [op for op in plan["ops"] if op["kind"] == "create_full_text_index"]
        assert any(op["name"] == "fts_fts_a_body" for op in fts_ops)

    def test_removed_fts_index_emits_drop(self) -> None:
        class FtsB(ferrum.Model):
            id: int
            body: str
            # No full_text_indexes in Meta — the FTS index was removed.

        state = AutodiffSchemaState(
            tables={
                "fts_b": {
                    "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                    "body": ColumnState(sql_type="TEXT", not_null=False),
                },
            },
            full_text_indexes={
                "fts_fts_b_body": FullTextIndexState(table="fts_b", columns=["body"]),
            },
        )
        plan = compute_autodiff_plan([FtsB], state)
        drop_ops = [op for op in plan["ops"] if op["kind"] == "drop_full_text_index"]
        assert any(op["name"] == "fts_fts_b_body" for op in drop_ops)


# ---------------------------------------------------------------------------
# 7. Destructive conversion rejection — type changes are always destructive
# ---------------------------------------------------------------------------


class TestDestructiveRejection:
    def test_type_widening_still_classified_destructive(self) -> None:
        """Even a 'safe' widening (INTEGER -> BIGINT) is classified destructive
        per W1-C — the autodiff never guesses a safe conversion."""

        class DrA(ferrum.Model):
            id: int
            n: int  # was INTEGER, now BIGINT

        state = _state_with_table(
            "dr_a",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "n": ColumnState(sql_type="INTEGER", not_null=False),
            },
        )
        plan = compute_autodiff_plan([DrA], state)
        # The plan must flag destructive because of the type-change alter_column.
        assert plan["destructive"] is True
        assert plan["requires_confirmation"] is True

    def test_drop_fk_classified_destructive_in_plan(self) -> None:
        class DrParent(ferrum.Model):
            id: int

        class DrChild(ferrum.Model):
            id: int
            # No parent_id / FK — it was removed.

        state = AutodiffSchemaState(
            tables={
                "dr_parent": {"id": ColumnState(sql_type="BIGSERIAL", not_null=True)},
                "dr_child": {"id": ColumnState(sql_type="BIGSERIAL", not_null=True)},
            },
            foreign_keys={
                "fk_dr_child_parent_id": ForeignKeyState(
                    table="dr_child", column="parent_id", ref_table="dr_parent"
                ),
            },
        )
        plan = compute_autodiff_plan([DrParent, DrChild], state)
        # drop_fk is in _DESTRUCTIVE_KINDS — plan must flag destructive.
        assert plan["destructive"] is True


# ---------------------------------------------------------------------------
# 8. Schema-state round-trip (empty → state → no-op plan)
# ---------------------------------------------------------------------------


class TestSchemaStateRoundTrip:
    def test_round_trip_no_changes_when_state_matches_model(self) -> None:
        """Replay ops into state, then compute plan against matching models → no ops."""

        class RtUser(ferrum.Model):
            id: int
            email: str

        class RtPost(ferrum.Model):
            id: int
            user_id: int
            user: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(to="RtUser", on_delete="CASCADE")

        # Build state from migration ops that match the models exactly.
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                "rt_user",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("email", "TEXT", not_null=True),
                ],
            ),
            ops.CreateTable(
                "rt_post",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("user_id", "INTEGER", not_null=True),
                ],
            ),
            ops.AddForeignKey("rt_post", "fk_rt_post_user_id", "user_id", "rt_user"),
        )
        state = build_extended_state([mod])
        plan = compute_autodiff_plan([RtUser, RtPost], state)
        assert plan["ops"] == []

    def test_round_trip_with_all_op_kinds_replayed(self) -> None:
        """Replay ops covering all op kinds; verify state tracks each correctly."""

        class RtDoc(ferrum.Model):
            id: int
            body: str

        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                "rt_doc",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("body", "TEXT"),
                ],
            ),
            ops.AddIndex("rt_doc", "idx_rt_doc_body", ["body"]),
            ops.CreateExtension("pg_trgm"),
            ops.EnableRLS("rt_doc", force=True),
            ops.CreatePolicy("rt_tenant", "rt_doc", "true"),
            ops.CreateFunction("rt_helper", "BODY"),
            ops.CreateFullTextIndex("rt_doc", "fts_rt_doc_body", ["body"], config="english"),
        )
        state = build_extended_state([mod])
        assert "rt_doc" in state.tables
        assert "idx_rt_doc_body" in state.indexes
        assert "pg_trgm" in state.extensions
        assert state.rls_enabled.get("rt_doc") is True
        assert state.rls_forced.get("rt_doc") is True
        assert "rt_tenant" in state.policies
        assert "rt_helper" in state.functions
        assert "fts_rt_doc_body" in state.full_text_indexes

    def test_round_trip_revert_via_reverse_ops(self) -> None:
        """Replay forward ops, then reverse ops, and verify state returns to empty."""

        class RtTemp(ferrum.Model):
            id: int

        forward = _FakeMigrationModule(
            "0001",
            ops.CreateTable("rt_temp", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.AddIndex("rt_temp", "idx_rt_temp_id", ["id"]),
        )
        reverse = _FakeMigrationModule(
            "0002",
            ops.DropIndex("idx_rt_temp_id"),
            ops.DropTable("rt_temp"),
        )
        state = build_extended_state([forward, reverse])
        assert "rt_temp" not in state.tables
        assert "idx_rt_temp_id" not in state.indexes


# ---------------------------------------------------------------------------
# 9. AutodiffSchemaState is a SchemaState (subclass relationship)
# ---------------------------------------------------------------------------


class TestSubclassRelationship:
    def test_autodiff_state_is_schema_state(self) -> None:
        state = AutodiffSchemaState()
        assert isinstance(state, SchemaState)

    def test_autodiff_state_accepted_by_compute_plan(self) -> None:
        """The base compute_plan accepts an AutodiffSchemaState via isinstance."""

        class ScA(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

        state = AutodiffSchemaState(
            tables={
                "sc_a": {
                    "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                    "slug": ColumnState(sql_type="TEXT", not_null=False),
                },
            },
        )
        # compute_plan (the base, not the extended one) should accept this.
        from ferrum.migrations.orchestrator import compute_plan

        plan = compute_plan([ScA], state)
        # The base autodiff should run (rich state) and detect db_index.
        idx_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert any(op["name"] == "idx_sc_a_slug" for op in idx_ops)
