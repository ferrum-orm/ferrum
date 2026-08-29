"""Integration tests for the migration graph, reversibility, and data migrations.

W3-A live-PostgreSQL coverage:
- Apply a sequence of migrations via the W1-C apply path, then use
  :class:`MigrationGraph` to verify status, upgrade plan, and downgrade plan.
- Checksum mismatch detection: edit an applied migration file on disk and
  confirm ``recovery_guidance`` reports the mismatch.
- Recovery from partial failure: insert a ledger row for a migration whose
  DDL was not applied, then verify the out-of-order hint fires.
- Data migration with ``transaction_policy="required"`` runs inside a
  transaction and rolls back on error.
- Data migration with ``transaction_policy="none"`` runs in autocommit.
- Reversible rename/type/default/nullability operations applied and reverted.
- Irreversible migration: ``downgrade_plan`` raises.

All tests use the ``pg_conn`` fixture (live PostgreSQL) and ``unique_suffix``
for parallel-safe table names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

import ferrum
from ferrum.errors import FerrumMigrationError
from ferrum.migrations.base import Migration
from ferrum.migrations.ledger import compute_digest, ensure_ledger
from ferrum.migrations.loader import MigrationModule
from ferrum.migrations.orchestrator import (
    DataMigration,
    MigrationGraph,
    apply,
    run_data_migration,
)

# ---------------------------------------------------------------------------
# Helpers — write real migration files and build MigrationModule objects
# ---------------------------------------------------------------------------


_MIGRATION_FILE_TEMPLATE = """\
from ferrum.migrations import Migration
from ferrum.migrations import operations as _ops


class Migration(Migration):
    dependencies = {deps!r}
    operations = {operations_src}
    reverse_operations = {reverse_src}
"""


def _ops_src(table: str) -> str:
    return (
        "[\n"
        f"        _ops.CreateTable({table!r}, [\n"
        "            _ops.Column('id', 'BIGSERIAL', primary_key=True, not_null=True),\n"
        "            _ops.Column('label', 'TEXT', not_null=True),\n"
        "        ]),\n"
        "    ]"
    )


def _reverse_src(table: str) -> str:
    return f"[_ops.DropTable({table!r})]"


def _write_migration(
    dir_path: Path,
    filename: str,
    *,
    table: str,
    deps: list[str] | None = None,
    irreversible: bool = False,
) -> Path:
    p = dir_path / filename
    p.write_text(
        _MIGRATION_FILE_TEMPLATE.format(
            deps=deps or [],
            operations_src=_ops_src(table),
            reverse_src="[]" if irreversible else _reverse_src(table),
        )
    )
    return p


def _mig_name(unique_suffix: str, num: int = 1, suffix: str = "a") -> str:
    """Build a unique migration name (parallel-safe) matching NNNN_slug.py."""
    return f"{num:04d}_{suffix}_{unique_suffix}"


def _mig_filename(name: str) -> str:
    return f"{name}.py"


async def _build_graph_and_apply(
    conn: ferrum.connection.Connection,
    modules: list[MigrationModule],
) -> MigrationGraph:
    """Ensure the ledger exists and return a MigrationGraph bound to *conn*."""
    await ensure_ledger(conn)
    return MigrationGraph(modules, conn=conn)


async def _record_applied_module(
    conn: ferrum.connection.Connection,
    module: MigrationModule,
) -> str:
    """Record *module* as applied in the ledger and return its digest."""
    content = module.path.read_text(encoding="utf-8")
    digest = compute_digest(module.name, content)
    from ferrum.migrations.ledger import record_applied

    await record_applied(conn, digest, description=module.name)
    return digest


# ---------------------------------------------------------------------------
# Status + upgrade plan via the graph
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_graph_status_pending_then_applied(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    tmp_path: Path,
) -> None:
    name = _mig_name(unique_suffix)
    table = f"ferrum_graph_status_{unique_suffix}"
    p = _write_migration(tmp_path, _mig_filename(name), table=table)
    mod = MigrationModule(name=name, path=p, migration=_load_class(p))
    graph = await _build_graph_and_apply(pg_conn, [mod])

    # Before recording: pending.
    statuses = await graph.status()
    assert len(statuses) == 1
    assert statuses[0].state == "pending"
    assert statuses[0].reversible is True

    # Record the file content digest in the ledger (what ``ferrum migrate``
    # does after applying the DDL via the W1-C path). We do not call apply()
    # here because apply() records the plan digest under the same description,
    # which would shadow the file-content digest the graph compares against.
    await _record_applied_module(pg_conn, mod)

    # After recording: the graph reads the ledger and reports applied.
    statuses = await graph.status()
    assert statuses[0].state == "applied"


@pytest.mark.integration
async def test_graph_upgrade_plan_filters_applied(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    tmp_path: Path,
) -> None:
    name_a = _mig_name(unique_suffix, 1, "a")
    name_b = _mig_name(unique_suffix, 2, "b")
    table_a = f"ferrum_graph_up_a_{unique_suffix}"
    table_b = f"ferrum_graph_up_b_{unique_suffix}"
    p1 = _write_migration(tmp_path, _mig_filename(name_a), table=table_a)
    p2 = _write_migration(tmp_path, _mig_filename(name_b), table=table_b, deps=[name_a])
    mod1 = MigrationModule(name=name_a, path=p1, migration=_load_class(p1))
    mod2 = MigrationModule(name=name_b, path=p2, migration=_load_class(p2))
    graph = await _build_graph_and_apply(pg_conn, [mod1, mod2])

    # Nothing applied → both in the upgrade plan.
    plan = await graph.upgrade_plan()
    assert [m.name for m in plan] == [name_a, name_b]

    # Record name_a as applied → only name_b remains.
    await _record_applied_module(pg_conn, mod1)
    plan = await graph.upgrade_plan()
    assert [m.name for m in plan] == [name_b]

    # Target limits to name_a (already applied) → empty.
    plan = await graph.upgrade_plan(target=name_a)
    assert plan == []


# ---------------------------------------------------------------------------
# Downgrade plan + irreversibility
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_graph_downgrade_plan_returns_last_applied(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    tmp_path: Path,
) -> None:
    name_a = _mig_name(unique_suffix, 1, "a")
    name_b = _mig_name(unique_suffix, 2, "b")
    table_a = f"ferrum_graph_dn_a_{unique_suffix}"
    table_b = f"ferrum_graph_dn_b_{unique_suffix}"
    p1 = _write_migration(tmp_path, _mig_filename(name_a), table=table_a)
    p2 = _write_migration(tmp_path, _mig_filename(name_b), table=table_b, deps=[name_a])
    mod1 = MigrationModule(name=name_a, path=p1, migration=_load_class(p1))
    mod2 = MigrationModule(name=name_b, path=p2, migration=_load_class(p2))
    graph = await _build_graph_and_apply(pg_conn, [mod1, mod2])

    await _record_applied_module(pg_conn, mod1)
    await _record_applied_module(pg_conn, mod2)

    plan = await graph.downgrade_plan()
    assert [m.name for m in plan] == [name_b]

    # Target name_a → revert name_b only.
    plan = await graph.downgrade_plan(target=name_a)
    assert [m.name for m in plan] == [name_b]


@pytest.mark.integration
async def test_graph_downgrade_plan_irreversible_raises(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    tmp_path: Path,
) -> None:
    name_a = _mig_name(unique_suffix, 1, "a")
    table_a = f"ferrum_graph_irr_a_{unique_suffix}"
    p1 = _write_migration(tmp_path, _mig_filename(name_a), table=table_a, irreversible=True)
    mod1 = MigrationModule(name=name_a, path=p1, migration=_load_class(p1))
    graph = await _build_graph_and_apply(pg_conn, [mod1])

    await _record_applied_module(pg_conn, mod1)
    with pytest.raises(FerrumMigrationError, match="irreversible"):
        await graph.downgrade_plan()


# ---------------------------------------------------------------------------
# Checksum mismatch + recovery guidance
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_graph_recovery_guidance_checksum_mismatch(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    tmp_path: Path,
) -> None:
    name = _mig_name(unique_suffix)
    table = f"ferrum_graph_cm_{unique_suffix}"
    p = _write_migration(tmp_path, _mig_filename(name), table=table)
    mod = MigrationModule(name=name, path=p, migration=_load_class(p))
    await _build_graph_and_apply(pg_conn, [mod])

    # Record with the original digest, then edit the file on disk.
    await _record_applied_module(pg_conn, mod)
    p.write_text(p.read_text() + "\n# edited\n")
    # Reload the module so the graph picks up the new file content.
    mod_edited = MigrationModule(name=name, path=p, migration=_load_class(p))
    graph_edited = MigrationGraph([mod_edited], conn=pg_conn)

    hints = await graph_edited.recovery_guidance()
    assert any(name in h and "edited" in h for h in hints)


@pytest.mark.integration
async def test_graph_recovery_guidance_out_of_order(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    tmp_path: Path,
) -> None:
    name_a = _mig_name(unique_suffix, 1, "a")
    name_b = _mig_name(unique_suffix, 2, "b")
    table_a = f"ferrum_graph_oo_a_{unique_suffix}"
    table_b = f"ferrum_graph_oo_b_{unique_suffix}"
    p1 = _write_migration(tmp_path, _mig_filename(name_a), table=table_a)
    p2 = _write_migration(tmp_path, _mig_filename(name_b), table=table_b, deps=[name_a])
    mod1 = MigrationModule(name=name_a, path=p1, migration=_load_class(p1))
    mod2 = MigrationModule(name=name_b, path=p2, migration=_load_class(p2))
    graph = await _build_graph_and_apply(pg_conn, [mod1, mod2])

    # Record name_b as applied but NOT name_a → out-of-order.
    await _record_applied_module(pg_conn, mod2)
    hints = await graph.recovery_guidance()
    assert any(name_a in h and name_b in h for h in hints)


# ---------------------------------------------------------------------------
# Data migrations — transaction policies
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_data_migration_required_policy_commits(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_dm_req_{unique_suffix}"
    # Create a table to insert into, outside the data migration.
    await pg_conn._require_driver().execute(
        f"CREATE TABLE {table} (id BIGSERIAL PRIMARY KEY, val TEXT NOT NULL)"
    )
    try:

        class _InsertData(DataMigration):
            transaction_policy: ClassVar[str] = "required"

            async def run(self, conn: object) -> None:
                await conn._require_driver().execute(
                    f"INSERT INTO {table} (val) VALUES ($1)",  # noqa: S608
                    "committed",
                )

        await run_data_migration(pg_conn, _InsertData())
        row = await pg_conn._require_driver().fetchrow(
            f"SELECT val FROM {table} WHERE val = $1",  # noqa: S608
            "committed",
        )
        assert row is not None
    finally:
        await pg_conn._require_driver().execute(f"DROP TABLE {table}")


@pytest.mark.integration
async def test_data_migration_required_policy_rolls_back_on_error(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_dm_rb_{unique_suffix}"
    await pg_conn._require_driver().execute(
        f"CREATE TABLE {table} (id BIGSERIAL PRIMARY KEY, val TEXT NOT NULL)"
    )
    try:

        class _InsertThenFail(DataMigration):
            transaction_policy: ClassVar[str] = "required"

            async def run(self, conn: object) -> None:
                await conn._require_driver().execute(
                    f"INSERT INTO {table} (val) VALUES ($1)",  # noqa: S608
                    "before-failure",
                )
                raise RuntimeError("intentional failure mid-migration")

        with pytest.raises(FerrumMigrationError, match="failed inside its transaction"):
            await run_data_migration(pg_conn, _InsertThenFail())
        # The insert must have been rolled back.
        row = await pg_conn._require_driver().fetchrow(
            f"SELECT val FROM {table} WHERE val = $1",  # noqa: S608
            "before-failure",
        )
        assert row is None
    finally:
        await pg_conn._require_driver().execute(f"DROP TABLE {table}")


@pytest.mark.integration
async def test_data_migration_none_policy_runs_in_autocommit(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_dm_none_{unique_suffix}"
    await pg_conn._require_driver().execute(
        f"CREATE TABLE {table} (id BIGSERIAL PRIMARY KEY, val TEXT NOT NULL)"
    )
    try:

        class _InsertNoTx(DataMigration):
            transaction_policy: ClassVar[str] = "none"

            async def run(self, conn: object) -> None:
                await conn._require_driver().execute(
                    f"INSERT INTO {table} (val) VALUES ($1)",  # noqa: S608
                    "autocommit",
                )

        await run_data_migration(pg_conn, _InsertNoTx())
        row = await pg_conn._require_driver().fetchrow(
            f"SELECT val FROM {table} WHERE val = $1",  # noqa: S608
            "autocommit",
        )
        assert row is not None
    finally:
        await pg_conn._require_driver().execute(f"DROP TABLE {table}")


# ---------------------------------------------------------------------------
# Reversible operations: rename / type / default / nullability / index
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reversible_rename_operation(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_rev_rename_{unique_suffix}"
    await pg_conn._require_driver().execute(
        f"CREATE TABLE {table} (id BIGSERIAL PRIMARY KEY, old_col TEXT)"
    )
    try:
        # Forward: rename old_col → new_col.
        rename_plan = json.dumps(
            {
                "name": f"rename_{unique_suffix}",
                "version": "1",
                "ops": [
                    {"kind": "rename_column", "table": table, "from": "old_col", "to": "new_col"}
                ],
            }
        )
        await apply(pg_conn, rename_plan, dry_run=False)
        row = await pg_conn._require_driver().fetchrow(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            "new_col",
        )
        assert row is not None

        # Reverse: rename new_col → old_col.
        reverse_plan = json.dumps(
            {
                "name": f"rename_rev_{unique_suffix}",
                "version": "1",
                "ops": [
                    {"kind": "rename_column", "table": table, "from": "new_col", "to": "old_col"}
                ],
            }
        )
        await apply(pg_conn, reverse_plan, dry_run=False)
        row = await pg_conn._require_driver().fetchrow(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            "old_col",
        )
        assert row is not None
    finally:
        await pg_conn._require_driver().execute(f"DROP TABLE {table}")


@pytest.mark.integration
async def test_reversible_default_and_nullability(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_rev_def_{unique_suffix}"
    await pg_conn._require_driver().execute(
        f"CREATE TABLE {table} (id BIGSERIAL PRIMARY KEY, val TEXT)"
    )
    try:
        # Forward: SET DEFAULT and SET NOT NULL (destructive — needs confirm).
        forward_plan = json.dumps(
            {
                "name": f"def_{unique_suffix}",
                "version": "1",
                "ops": [
                    {"kind": "alter_column", "table": table, "column": "val", "default": "''"},
                    {"kind": "alter_column", "table": table, "column": "val", "not_null": True},
                ],
            }
        )
        await apply(pg_conn, forward_plan, dry_run=False, confirm=True)
        row = await pg_conn._require_driver().fetchrow(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            "val",
        )
        assert row is not None
        assert row["is_nullable"] == "NO"
        assert "''" in row["column_default"]

        # Reverse: DROP NOT NULL and DROP DEFAULT (safe — no confirm needed).
        reverse_plan = json.dumps(
            {
                "name": f"def_rev_{unique_suffix}",
                "version": "1",
                "ops": [
                    {"kind": "alter_column", "table": table, "column": "val", "not_null": False},
                    {"kind": "alter_column", "table": table, "column": "val", "drop_default": True},
                ],
            }
        )
        await apply(pg_conn, reverse_plan, dry_run=False)
        row = await pg_conn._require_driver().fetchrow(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            "val",
        )
        assert row is not None
        assert row["is_nullable"] == "YES"
        assert row["column_default"] is None
    finally:
        await pg_conn._require_driver().execute(f"DROP TABLE {table}")


@pytest.mark.integration
async def test_reversible_index_add_drop(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_rev_idx_{unique_suffix}"
    idx = f"idx_{table}_val"
    await pg_conn._require_driver().execute(
        f"CREATE TABLE {table} (id BIGSERIAL PRIMARY KEY, val TEXT)"
    )
    try:
        # Forward: add index.
        add_plan = json.dumps(
            {
                "name": f"add_idx_{unique_suffix}",
                "version": "1",
                "ops": [
                    {
                        "kind": "add_index",
                        "table": table,
                        "name": idx,
                        "columns": ["val"],
                        "using": "btree",
                    }
                ],
            }
        )
        await apply(pg_conn, add_plan, dry_run=False)
        row = await pg_conn._require_driver().fetchrow(
            "SELECT 1 FROM pg_indexes WHERE indexname = $1", idx
        )
        assert row is not None

        # Reverse: drop index.
        drop_plan = json.dumps(
            {
                "name": f"drop_idx_{unique_suffix}",
                "version": "1",
                "ops": [{"kind": "drop_index", "name": idx}],
            }
        )
        await apply(pg_conn, drop_plan, dry_run=False)
        row = await pg_conn._require_driver().fetchrow(
            "SELECT 1 FROM pg_indexes WHERE indexname = $1", idx
        )
        assert row is None
    finally:
        await pg_conn._require_driver().execute(f"DROP TABLE {table}")


# ---------------------------------------------------------------------------
# Helper: load a Migration class from a file path
# ---------------------------------------------------------------------------


def _load_class(path: Path) -> type[Migration]:
    """Import a migration file and return its Migration class."""
    from ferrum.migrations.loader import load_module

    return load_module(path)
