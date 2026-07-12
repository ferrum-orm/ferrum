"""Unit tests for makemigrations index/default/nullability autodiff.

Covers the bugs fixed in the ``makemigrations_index_autodiff`` plan:

1. ``Field(db_default=..., nullable=...)`` flows through ``FieldMeta`` correctly.
2. ``_normalize_db_default`` uppercases recognised tokens (``now()`` → ``NOW()``).
3. Fresh ``compute_plan`` emits NOT NULL + DEFAULT + add_index for new tables.
4. Existing-table autodiff: add_index / drop_index on index changes.
5. Existing-table autodiff: alter_column for default/nullability changes.
6. ``_build_existing_state`` round-trip replays create + add_index + alter_column.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import ferrum
from ferrum.cli.makemigrations_cmd import _build_existing_state, _op_to_source
from ferrum.migrations import operations as ops
from ferrum.migrations.orchestrator import (
    ColumnState,
    IndexState,
    SchemaState,
    compute_plan,
)
from ferrum.models import _normalize_db_default

# ---------------------------------------------------------------------------
# 1. Field API — db_default= and nullable= params on Field()
# ---------------------------------------------------------------------------


class TestFieldDbDefaultParam:
    def test_explicit_db_default_sets_field_meta(self) -> None:
        class EvtA(ferrum.Model):
            id: int
            created_at: Annotated[datetime, ferrum.Field(db_default="now()", nullable=False)]

        f = next(fm for fm in EvtA.get_metadata().fields if fm.name == "created_at")
        assert f.db_default == "NOW()"

    def test_explicit_db_default_normalized_uppercase(self) -> None:
        class EvtB(ferrum.Model):
            id: int
            ts: Annotated[datetime, ferrum.Field(db_default="CURRENT_TIMESTAMP", nullable=False)]

        f = next(fm for fm in EvtB.get_metadata().fields if fm.name == "ts")
        assert f.db_default == "CURRENT_TIMESTAMP"

    def test_explicit_db_default_lowercase_now(self) -> None:
        class EvtC(ferrum.Model):
            id: int
            ts: Annotated[datetime | None, ferrum.Field(db_default="now()")]

        f = next(fm for fm in EvtC.get_metadata().fields if fm.name == "ts")
        assert f.db_default == "NOW()"

    def test_explicit_db_default_overrides_uuid_generate(self) -> None:
        """Explicit db_default= wins over uuid_generate."""
        from uuid import UUID

        class UuidCustom(ferrum.Model):
            id: Annotated[
                UUID, ferrum.Field(primary_key=True, uuid_generate="v4", db_default="UUIDV7()")
            ]
            name: str

        f = next(fm for fm in UuidCustom.get_metadata().fields if fm.name == "id")
        assert f.db_default == "UUIDV7()"


class TestFieldNullableParam:
    def test_nullable_false_overrides_optional_annotation(self) -> None:
        """Field(nullable=False) on a ``T | None`` annotation forces NOT NULL."""

        class EvtD(ferrum.Model):
            id: int
            created_at: Annotated[datetime | None, ferrum.Field(db_default="now()", nullable=False)]

        f = next(fm for fm in EvtD.get_metadata().fields if fm.name == "created_at")
        assert f.nullable is False

    def test_nullable_true_overrides_non_optional_annotation(self) -> None:
        """Field(nullable=True) on a non-optional annotation forces nullable."""

        class EvtE(ferrum.Model):
            id: int
            name: Annotated[str, ferrum.Field(nullable=True)]

        f = next(fm for fm in EvtE.get_metadata().fields if fm.name == "name")
        assert f.nullable is True

    def test_nullable_none_leaves_annotation_derived(self) -> None:
        """Omitting nullable= leaves annotation-derived nullability unchanged."""

        class EvtF(ferrum.Model):
            id: int
            bio: str | None = None

        f = next(fm for fm in EvtF.get_metadata().fields if fm.name == "bio")
        assert f.nullable is True


# ---------------------------------------------------------------------------
# 2. _normalize_db_default
# ---------------------------------------------------------------------------


class TestNormalizeDbDefault:
    def test_now_lower(self) -> None:
        assert _normalize_db_default("now()") == "NOW()"

    def test_now_upper(self) -> None:
        assert _normalize_db_default("NOW()") == "NOW()"

    def test_current_timestamp_lower(self) -> None:
        assert _normalize_db_default("current_timestamp") == "CURRENT_TIMESTAMP"

    def test_gen_random_uuid_lower(self) -> None:
        assert _normalize_db_default("gen_random_uuid()") == "GEN_RANDOM_UUID()"

    def test_uuidv7_lower(self) -> None:
        assert _normalize_db_default("uuidv7()") == "UUIDV7()"

    def test_unknown_expression_unchanged(self) -> None:
        val = "my_custom_func()"
        assert _normalize_db_default(val) == val

    def test_empty_string_unchanged(self) -> None:
        assert _normalize_db_default("''") == "''"


# ---------------------------------------------------------------------------
# 3. Fresh compute_plan — NOT NULL, DEFAULT, add_index in create_table
# ---------------------------------------------------------------------------


class TestComputePlanFreshTable:
    def test_not_null_emitted_in_create(self) -> None:
        class LogA(ferrum.Model):
            id: int
            created_at: Annotated[datetime | None, ferrum.Field(db_default="now()", nullable=False)]

        plan = compute_plan([LogA], existing_tables={})
        ct = next(op for op in plan["ops"] if op["kind"] == "create_table")
        col = next(c for c in ct["columns"] if c["name"] == "created_at")
        assert col["not_null"] is True

    def test_db_default_emitted_in_create(self) -> None:
        class LogB(ferrum.Model):
            id: int
            created_at: Annotated[datetime | None, ferrum.Field(db_default="now()", nullable=False)]

        plan = compute_plan([LogB], existing_tables={})
        ct = next(op for op in plan["ops"] if op["kind"] == "create_table")
        col = next(c for c in ct["columns"] if c["name"] == "created_at")
        assert col["default"] == "NOW()"

    def test_db_index_field_emits_add_index(self) -> None:
        class LogC(ferrum.Model):
            id: int
            ts: Annotated[datetime, ferrum.Field(db_index=True)]

        plan = compute_plan([LogC], existing_tables={})
        idx_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert any(op["name"] == "idx_log_c_ts" for op in idx_ops)

    def test_meta_index_emits_add_index(self) -> None:
        class LogD(ferrum.Model):
            id: int
            kind: str

            class Meta:
                indexes = (ferrum.Index(fields=("kind",), name="idx_log_d_kind"),)

        plan = compute_plan([LogD], existing_tables={})
        idx_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert any(op["name"] == "idx_log_d_kind" for op in idx_ops)


# ---------------------------------------------------------------------------
# 4. Existing-table index autodiff
# ---------------------------------------------------------------------------


class TestIndexAutodiff:
    def _make_state(
        self, table: str, cols: list[str], indexes: dict[str, dict] | None = None
    ) -> SchemaState:
        col_states = {c: ColumnState(sql_type="TEXT") for c in cols}
        idx_states: dict[str, IndexState] = {}
        if indexes:
            for name, info in indexes.items():
                idx_states[name] = IndexState(
                    table=info.get("table", table),
                    columns=info.get("columns", []),
                )
        return SchemaState(tables={table: col_states}, indexes=idx_states)

    def test_add_index_for_existing_column_with_db_index(self) -> None:
        """Flipping db_index=True on an existing column emits add_index."""

        class IdxA(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

        state = self._make_state("idx_a", ["id", "slug"])
        plan = compute_plan([IdxA], existing_tables=state)
        idx_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert any(op["name"] == "idx_idx_a_slug" for op in idx_ops)

    def test_no_add_index_when_already_tracked(self) -> None:
        """No add_index when the index is already in SchemaState."""

        class IdxB(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

        state = self._make_state(
            "idx_b",
            ["id", "slug"],
            {"idx_idx_b_slug": {"table": "idx_b", "columns": ["slug"]}},
        )
        plan = compute_plan([IdxB], existing_tables=state)
        idx_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert not any(op["name"] == "idx_idx_b_slug" for op in idx_ops)

    def test_drop_index_when_db_index_removed(self) -> None:
        """Removing db_index from a field emits drop_index for the tracked index."""

        class IdxC(ferrum.Model):
            id: int
            slug: str  # db_index no longer set

        state = self._make_state(
            "idx_c",
            ["id", "slug"],
            {"idx_idx_c_slug": {"table": "idx_c", "columns": ["slug"]}},
        )
        plan = compute_plan([IdxC], existing_tables=state)
        drop_ops = [op for op in plan["ops"] if op["kind"] == "drop_index"]
        assert any(op["name"] == "idx_idx_c_slug" for op in drop_ops)

    def test_add_meta_index_for_existing_columns(self) -> None:
        """Adding a Meta.indexes entry for existing columns emits add_index."""

        class IdxD(ferrum.Model):
            id: int
            kind: str

            class Meta:
                indexes = (ferrum.Index(fields=("kind",), name="idx_idx_d_kind"),)

        state = self._make_state("idx_d", ["id", "kind"])
        plan = compute_plan([IdxD], existing_tables=state)
        idx_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert any(op["name"] == "idx_idx_d_kind" for op in idx_ops)

    def test_no_add_index_when_column_is_new(self) -> None:
        """db_index on a newly added column uses add_column path, not autodiff."""

        class IdxE(ferrum.Model):
            id: int
            new_col: Annotated[str, ferrum.Field(db_index=True)]

        state = self._make_state("idx_e", ["id"])  # new_col not in state
        plan = compute_plan([IdxE], existing_tables=state)
        add_col_ops = [op for op in plan["ops"] if op["kind"] == "add_column"]
        add_idx_ops = [op for op in plan["ops"] if op["kind"] == "add_index"]
        assert any(op["name"] == "new_col" for op in add_col_ops)
        # add_index from the new-column path (not autodiff)
        assert any(op["name"] == "idx_idx_e_new_col" for op in add_idx_ops)


# ---------------------------------------------------------------------------
# 5. Column default/nullability autodiff
# ---------------------------------------------------------------------------


class TestColumnAttrAutodiff:
    def _make_state(self, table: str, cols: dict[str, ColumnState]) -> SchemaState:
        return SchemaState(tables={table: cols})

    def test_add_default_emits_alter_column(self) -> None:
        """A field gains db_default that wasn't in the state → alter_column with default."""

        class AltA(ferrum.Model):
            id: int
            ts: Annotated[datetime | None, ferrum.Field(db_default="now()", nullable=False)]

        state = self._make_state(
            "alt_a",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "ts": ColumnState(sql_type="TIMESTAMPTZ", not_null=True, default=None),
            },
        )
        plan = compute_plan([AltA], existing_tables=state)
        alter_ops = [op for op in plan["ops"] if op["kind"] == "alter_column"]
        assert any(op["column"] == "ts" and op.get("default") == "NOW()" for op in alter_ops)

    def test_drop_default_emits_alter_column_drop(self) -> None:
        """db_default removed from a field that had one → alter_column drop_default."""

        class AltB(ferrum.Model):
            id: int
            ts: datetime  # no db_default

        state = self._make_state(
            "alt_b",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "ts": ColumnState(sql_type="TIMESTAMPTZ", not_null=True, default="NOW()"),
            },
        )
        plan = compute_plan([AltB], existing_tables=state)
        alter_ops = [op for op in plan["ops"] if op["kind"] == "alter_column"]
        assert any(op["column"] == "ts" and op.get("drop_default") is True for op in alter_ops)

    def test_nullable_change_emits_alter_column(self) -> None:
        """Annotation-derived nullable=True vs state not_null=True → alter_column."""

        class AltC(ferrum.Model):
            id: int
            bio: str | None  # nullable=True

        state = self._make_state(
            "alt_c",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "bio": ColumnState(sql_type="TEXT", not_null=True),  # was NOT NULL
            },
        )
        plan = compute_plan([AltC], existing_tables=state)
        alter_ops = [op for op in plan["ops"] if op["kind"] == "alter_column"]
        assert any(op["column"] == "bio" and op.get("not_null") is False for op in alter_ops)

    def test_no_alter_when_state_matches(self) -> None:
        """No ops emitted when model and state are identical."""

        class AltD(ferrum.Model):
            id: int
            ts: Annotated[datetime | None, ferrum.Field(db_default="now()", nullable=False)]

        state = self._make_state(
            "alt_d",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "ts": ColumnState(sql_type="TIMESTAMPTZ", not_null=True, default="NOW()"),
            },
        )
        plan = compute_plan([AltD], existing_tables=state)
        assert plan["ops"] == []

    def test_legacy_lowercase_default_normalized_for_comparison(self) -> None:
        """State has lowercase 'now()'; model has 'NOW()' → no spurious alter_column."""

        class AltE(ferrum.Model):
            id: int
            ts: Annotated[datetime | None, ferrum.Field(db_default="now()", nullable=False)]

        state = self._make_state(
            "alt_e",
            {
                "id": ColumnState(sql_type="BIGSERIAL", not_null=True),
                "ts": ColumnState(sql_type="TIMESTAMPTZ", not_null=True, default="now()"),
            },
        )
        plan = compute_plan([AltE], existing_tables=state)
        alter_ops = [op for op in plan["ops"] if op["kind"] == "alter_column"]
        assert not any(op["column"] == "ts" for op in alter_ops)


# ---------------------------------------------------------------------------
# 6. _build_existing_state round-trip
# ---------------------------------------------------------------------------


class _FakeMigrationModule:
    """Minimal stub of loader.MigrationModule for testing _build_existing_state."""

    def __init__(self, *operations: ops.Operation) -> None:
        self.migration = self

        class _Inner:
            pass

        self.migration = type("Mig", (), {"operations": list(operations)})()  # type: ignore[assignment]
        self.name = "0001_test"


class TestBuildExistingState:
    def test_create_table_replayed(self) -> None:
        mod = _FakeMigrationModule(
            ops.CreateTable(
                "logs",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("created_at", "TIMESTAMPTZ", not_null=True, default="NOW()"),
                ],
            )
        )
        state = _build_existing_state([mod])
        assert "logs" in state.tables
        assert "id" in state.tables["logs"]
        assert state.tables["logs"]["created_at"].not_null is True
        assert state.tables["logs"]["created_at"].default == "NOW()"

    def test_add_index_replayed(self) -> None:
        mod = _FakeMigrationModule(
            ops.CreateTable("logs2", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.AddIndex("logs2", "idx_logs2_id", ["id"]),
        )
        state = _build_existing_state([mod])
        assert "idx_logs2_id" in state.indexes
        assert state.indexes["idx_logs2_id"].table == "logs2"

    def test_drop_index_replayed(self) -> None:
        mod = _FakeMigrationModule(
            ops.CreateTable("logs3", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.AddIndex("logs3", "idx_logs3_id", ["id"]),
            ops.DropIndex("idx_logs3_id"),
        )
        state = _build_existing_state([mod])
        assert "idx_logs3_id" not in state.indexes

    def test_alter_column_replayed(self) -> None:
        mod = _FakeMigrationModule(
            ops.CreateTable(
                "logs4",
                [ops.Column("id", "BIGSERIAL", not_null=True), ops.Column("ts", "TIMESTAMPTZ")],
            ),
            ops.AlterColumn("logs4", "ts", not_null=True, default="NOW()"),
        )
        state = _build_existing_state([mod])
        ts_state = state.tables["logs4"]["ts"]
        assert ts_state.not_null is True
        assert ts_state.default == "NOW()"

    def test_alter_column_drop_default_replayed(self) -> None:
        mod = _FakeMigrationModule(
            ops.CreateTable(
                "logs5",
                [
                    ops.Column("id", "BIGSERIAL", not_null=True),
                    ops.Column("ts", "TIMESTAMPTZ", default="NOW()"),
                ],
            ),
            ops.AlterColumn("logs5", "ts", drop_default=True),
        )
        state = _build_existing_state([mod])
        assert state.tables["logs5"]["ts"].default is None

    def test_drop_table_replayed(self) -> None:
        mod = _FakeMigrationModule(
            ops.CreateTable("gone", [ops.Column("id", "BIGSERIAL", not_null=True)]),
            ops.DropTable("gone"),
        )
        state = _build_existing_state([mod])
        assert "gone" not in state.tables

    def test_drop_column_replayed(self) -> None:
        mod = _FakeMigrationModule(
            ops.CreateTable(
                "partial",
                [ops.Column("id", "BIGSERIAL", not_null=True), ops.Column("old", "TEXT")],
            ),
            ops.DropColumn("partial", "old"),
        )
        state = _build_existing_state([mod])
        assert "old" not in state.tables["partial"]

    def test_legacy_lowercase_default_normalized_in_state(self) -> None:
        """Defaults recorded in old migration files (e.g. 'now()') are normalised."""
        mod = _FakeMigrationModule(
            ops.CreateTable(
                "logs6",
                [ops.Column("ts", "TIMESTAMPTZ", default="now()")],
            )
        )
        state = _build_existing_state([mod])
        assert state.tables["logs6"]["ts"].default == "NOW()"


# ---------------------------------------------------------------------------
# 7. _op_to_source round-trip for new op kinds
# ---------------------------------------------------------------------------


class TestOpToSource:
    def test_drop_index_rendered(self) -> None:
        src = _op_to_source({"kind": "drop_index", "name": "idx_foo_bar", "table": "foo"})
        assert "ops.DropIndex" in src
        assert "idx_foo_bar" in src

    def test_alter_column_default_rendered(self) -> None:
        src = _op_to_source(
            {"kind": "alter_column", "table": "foo", "column": "ts", "default": "NOW()"}
        )
        assert "ops.AlterColumn" in src
        assert "default='NOW()'" in src

    def test_alter_column_not_null_rendered(self) -> None:
        src = _op_to_source(
            {"kind": "alter_column", "table": "foo", "column": "bio", "not_null": False}
        )
        assert "not_null=False" in src

    def test_alter_column_drop_default_rendered(self) -> None:
        src = _op_to_source(
            {"kind": "alter_column", "table": "foo", "column": "ts", "drop_default": True}
        )
        assert "drop_default=True" in src
