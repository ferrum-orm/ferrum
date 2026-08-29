"""Integration tests for extended migration autodiff against live PostgreSQL.

Covers schema-state replay round-trip from empty schema through upgrade/revert
using :mod:`ferrum.migrations.autodiff`. Tests verify that:

- :func:`build_extended_state` correctly replays all migration op kinds.
- :func:`compute_autodiff_plan` produces a no-op plan when the model matches
  the replayed state.
- Applying an autodiff plan to a live database and then reverting it returns
  the schema to its prior state.
- Type changes, renames (with hints), and FK add/drop are detected correctly
  against a live schema.

These tests require a live PostgreSQL instance (``FERRUM_TEST_DSN``). They
create and drop transient tables scoped by ``unique_suffix`` for parallel
safety.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

import ferrum
from ferrum.migrations import operations as ops
from ferrum.migrations.autodiff import (
    build_extended_state,
    compute_autodiff_plan,
)
from ferrum.migrations.orchestrator import apply


def _plan_json(plan: dict) -> str:
    """Serialize a plan dict to the JSON string expected by apply()."""
    return json.dumps(plan)


class _FakeMigrationModule:
    """Minimal stub of loader.MigrationModule for state replay tests."""

    def __init__(self, name: str, *operations: ops.Operation) -> None:
        self.name = name
        self.migration = type("Mig", (), {"operations": list(operations)})()


@pytest.mark.integration
async def test_round_trip_empty_schema_through_upgrade_and_revert(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    """Build state from ops, compute a no-op plan, apply + revert against live PG.

    Verifies the full round-trip: replay ops → state → compute plan (no changes
    when model matches) → apply forward ops to live DB → revert → DB is clean.
    """
    table = f"ad_rt_{unique_suffix}"

    forward_ops = [
        ops.CreateTable(
            table,
            [
                ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                ops.Column("name", "TEXT", not_null=True),
            ],
        ),
        ops.AddIndex(table, f"idx_{table}_name", ["name"]),
    ]
    forward_plan = {
        "version": 1,
        "name": f"forward_{table}",
        "ops": [op.to_op_dict() for op in forward_ops],
    }
    revert_plan = {
        "version": 1,
        "name": f"revert_{table}",
        "ops": [
            {"kind": "drop_index", "name": f"idx_{table}_name", "table": table},
            {"kind": "drop_table", "table": table},
        ],
    }

    try:
        # Apply forward ops to the live DB.
        result = await apply(pg_conn, _plan_json(forward_plan), dry_run=False)
        assert result.applied is True

        # Build state from the forward ops (simulating makemigrations replay).
        mod = _FakeMigrationModule("0001", *forward_ops)
        state = build_extended_state([mod])
        assert table in state.tables
        assert f"idx_{table}_name" in state.indexes

        # Define a model matching the applied schema.
        class RtModel(ferrum.Model):
            model_config = ferrum.ModelConfig(table=table)
            id: int
            name: str

            class Meta:
                indexes: ClassVar[list[ferrum.Index]] = [
                    ferrum.Index(fields=("name",), name=f"idx_{table}_name")
                ]

        # Compute the autodiff plan — should be empty (state matches model).
        plan = compute_autodiff_plan([RtModel], state)
        assert plan["ops"] == []
    finally:
        # Revert: drop the index and table.
        await apply(pg_conn, _plan_json(revert_plan), dry_run=False, confirm=True)

        # Verify the table is gone.
        row = await pg_conn._require_driver().fetchrow(
            "SELECT 1 FROM information_schema.tables WHERE table_name = $1",
            table,
        )
        assert row is None


@pytest.mark.integration
async def test_type_change_detected_against_live_schema(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    """Apply a table with SMALLINT, then autodiff detects the type change to INTEGER.

    The autodiff plan must emit an alter_column with sql_type (destructive per
    W1-C), and applying it to the live DB must actually change the column type.
    """
    table = f"ad_tc_{unique_suffix}"

    initial_plan = {
        "version": 1,
        "name": f"init_{table}",
        "ops": [
            {
                "kind": "create_table",
                "table": table,
                "columns": [
                    {"name": "id", "sql_type": "BIGSERIAL", "primary_key": True, "not_null": True},
                    {"name": "count", "sql_type": "SMALLINT", "not_null": False},
                ],
            }
        ],
    }
    drop_plan = {
        "version": 1,
        "name": f"drop_{table}",
        "ops": [{"kind": "drop_table", "table": table}],
    }

    try:
        await apply(pg_conn, _plan_json(initial_plan), dry_run=False)

        # Build state from the initial ops (column is SMALLINT, nullable).
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                table,
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("count", "SMALLINT"),
                ],
            ),
        )
        state = build_extended_state([mod])

        # Model now wants INTEGER for count.
        class TcModel(ferrum.Model):
            model_config = ferrum.ModelConfig(table=table)
            id: int
            count: int | None  # nullable matches state; type changes SMALLINT → INTEGER

        plan = compute_autodiff_plan([TcModel], state)
        alter_ops = [
            op for op in plan["ops"] if op["kind"] == "alter_column" and op.get("column") == "count"
        ]
        assert len(alter_ops) == 1
        assert alter_ops[0].get("sql_type") == "INTEGER"
        assert plan["destructive"] is True

        # Apply the type-change plan to the live DB.
        await apply(pg_conn, _plan_json(plan), dry_run=False, confirm=True)

        # Verify the column type actually changed in the live DB.
        row = await pg_conn._require_driver().fetchrow(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            "count",
        )
        assert row is not None
        # PostgreSQL reports "integer" for INTEGER, "smallint" for SMALLINT.
        data_type = row.get("data_type", row[0]) if isinstance(row, dict) else row[0]
        assert data_type == "integer"
    finally:
        await apply(pg_conn, _plan_json(drop_plan), dry_run=False, confirm=True)


@pytest.mark.integration
async def test_rename_detected_with_hint_against_live_schema(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    """Apply a table with 'old_name', then autodiff detects the rename to 'new_name'."""
    table = f"ad_rn_{unique_suffix}"

    initial_plan = {
        "version": 1,
        "name": f"init_{table}",
        "ops": [
            {
                "kind": "create_table",
                "table": table,
                "columns": [
                    {"name": "id", "sql_type": "BIGSERIAL", "primary_key": True, "not_null": True},
                    {"name": "old_name", "sql_type": "TEXT", "not_null": False},
                ],
            }
        ],
    }
    drop_plan = {
        "version": 1,
        "name": f"drop_{table}",
        "ops": [{"kind": "drop_table", "table": table}],
    }

    try:
        await apply(pg_conn, _plan_json(initial_plan), dry_run=False)

        # Build state from the initial ops.
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                table,
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("old_name", "TEXT"),
                ],
            ),
        )
        state = build_extended_state([mod])

        # Model now has 'new_name' instead of 'old_name'.
        class RnModel(ferrum.Model):
            model_config = ferrum.ModelConfig(table=table)
            id: int
            new_name: str | None

        # With a rename hint, the autodiff should emit rename_column.
        plan = compute_autodiff_plan(
            [RnModel], state, rename_hints={table: {"old_name": "new_name"}}
        )
        rename_ops = [op for op in plan["ops"] if op["kind"] == "rename_column"]
        assert len(rename_ops) == 1
        assert rename_ops[0]["from"] == "old_name"
        assert rename_ops[0]["to"] == "new_name"

        # Apply the rename to the live DB.
        await apply(pg_conn, _plan_json(plan), dry_run=False)

        # Verify the column was renamed in the live DB.
        row = await pg_conn._require_driver().fetchrow(
            "SELECT 1 FROM information_schema.columns WHERE table_name = $1 AND column_name = $2",
            table,
            "new_name",
        )
        assert row is not None

        row = await pg_conn._require_driver().fetchrow(
            "SELECT 1 FROM information_schema.columns WHERE table_name = $1 AND column_name = $2",
            table,
            "old_name",
        )
        assert row is None
    finally:
        await apply(pg_conn, _plan_json(drop_plan), dry_run=False, confirm=True)


@pytest.mark.integration
async def test_fk_add_detected_against_live_schema(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    """Apply parent + child tables without FK, then autodiff detects the new FK."""
    parent = f"ad_fk_parent_{unique_suffix}"
    child = f"ad_fk_child_{unique_suffix}"

    initial_plan = {
        "version": 1,
        "name": f"init_{parent}",
        "ops": [
            {
                "kind": "create_table",
                "table": parent,
                "columns": [
                    {"name": "id", "sql_type": "BIGSERIAL", "primary_key": True, "not_null": True},
                ],
            },
            {
                "kind": "create_table",
                "table": child,
                "columns": [
                    {"name": "id", "sql_type": "BIGSERIAL", "primary_key": True, "not_null": True},
                    {"name": "parent_id", "sql_type": "INTEGER", "not_null": True},
                ],
            },
        ],
    }
    drop_plan = {
        "version": 1,
        "name": f"drop_{parent}",
        "ops": [
            {"kind": "drop_table", "table": child},
            {"kind": "drop_table", "table": parent},
        ],
    }

    try:
        await apply(pg_conn, _plan_json(initial_plan), dry_run=False)

        # Build state from the initial ops (no FK).
        mod = _FakeMigrationModule(
            "0001",
            ops.CreateTable(
                parent, [ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True)]
            ),
            ops.CreateTable(
                child,
                [
                    ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                    ops.Column("parent_id", "INTEGER", not_null=True),
                ],
            ),
        )
        state = build_extended_state([mod])

        # Models now declare a FK from child to parent.
        class FkParentModel(ferrum.Model):
            model_config = ferrum.ModelConfig(table=parent)
            id: int

        class FkChildModel(ferrum.Model):
            model_config = ferrum.ModelConfig(table=child)
            id: int
            parent_id: int
            parent: ClassVar[ferrum.ForeignKey] = ferrum.ForeignKey(
                to="FkParentModel", on_delete="CASCADE"
            )

        plan = compute_autodiff_plan([FkParentModel, FkChildModel], state)
        add_fk_ops = [op for op in plan["ops"] if op["kind"] == "add_fk"]
        assert any(op["name"] == f"fk_{child}_parent_id" for op in add_fk_ops)

        # Apply the FK addition to the live DB.
        await apply(pg_conn, _plan_json(plan), dry_run=False)

        # Verify the FK constraint exists in the live DB.
        row = await pg_conn._require_driver().fetchrow(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = $1 AND constraint_type = 'FOREIGN KEY' "
            "AND constraint_name = $2",
            child,
            f"fk_{child}_parent_id",
        )
        assert row is not None
    finally:
        await apply(pg_conn, _plan_json(drop_plan), dry_run=False, confirm=True)


@pytest.mark.integration
async def test_full_state_replay_with_all_op_kinds(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    """Replay migration ops covering all op kinds and verify state tracking.

    This is a schema-state replay test: build_extended_state must correctly
    track tables, indexes, FKs, extensions, RLS, policies, functions, and FTS
    indexes after replaying a comprehensive set of migration ops.
    """
    table = f"ad_all_{unique_suffix}"

    # We only replay into state (no live DB apply for RLS/extensions/functions
    # since those require specific PG privileges). Verify state tracking.
    mod = _FakeMigrationModule(
        "0001",
        ops.CreateTable(
            table,
            [
                ops.Column("id", "BIGSERIAL", not_null=True, primary_key=True),
                ops.Column("body", "TEXT"),
            ],
        ),
        ops.AddIndex(table, f"idx_{table}_body", ["body"]),
        ops.CreateExtension("pg_trgm"),
        ops.EnableRLS(table, force=True),
        ops.CreatePolicy(f"pol_{table}", table, "true"),
        ops.CreateFunction(f"fn_{table}", "CREATE OR REPLACE FUNCTION ... $$ ... $$"),
        ops.CreateFullTextIndex(table, f"fts_{table}_body", ["body"], config="english"),
    )
    state = build_extended_state([mod])

    # Verify all op kinds were tracked.
    assert table in state.tables
    assert state.tables[table]["id"].sql_type == "BIGSERIAL"
    assert f"idx_{table}_body" in state.indexes
    assert "pg_trgm" in state.extensions
    assert state.rls_enabled.get(table) is True
    assert state.rls_forced.get(table) is True
    assert f"pol_{table}" in state.policies
    assert f"fn_{table}" in state.functions
    assert f"fts_{table}_body" in state.full_text_indexes
    assert state.full_text_indexes[f"fts_{table}_body"].config == "english"
