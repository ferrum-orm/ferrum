"""Unit tests for the migration orchestrator.

Tests cover:
- Dry-run path: no DB calls, correct return value.
- Safety gates: destructive ops and non-dev env raise without confirm.
- DDL generation: identifiers are double-quoted, SQL shape is correct.
- Edge cases: unique index flag, IF EXISTS clauses.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import ferrum
from ferrum.errors import FerrumMigrationError
from ferrum.migrations import MigrationResult, _op_to_sql, apply, compute_plan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_json(
    *,
    name: str = "test_migration",
    version: str = "1",
    ops: list | None = None,
    requires_confirmation: bool = False,
) -> str:
    return json.dumps(
        {
            "name": name,
            "version": version,
            "requires_confirmation": requires_confirmation,
            "ops": ops or [],
        }
    )


def _make_conn(*, pool: object = None) -> MagicMock:
    """Return a mock Connection whose _require_pool returns ``pool``."""
    conn = MagicMock()
    conn._require_pool.return_value = pool or MagicMock()
    return conn


# ---------------------------------------------------------------------------
# test_dry_run_returns_without_applying
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_returns_without_applying() -> None:
    """dry_run=True must return MigrationResult without calling execute."""
    pool = AsyncMock()
    conn = _make_conn(pool=pool)

    plan = _plan_json(ops=[{"kind": "drop_table", "table": "old_users"}])
    result = await apply(conn, plan, dry_run=True)

    assert isinstance(result, MigrationResult)
    assert result.applied is False
    assert result.dry_run is True
    assert result.ops_count == 1

    # Pool must not be acquired at all during dry-run.
    conn._require_pool.assert_not_called()
    pool.execute.assert_not_called()


# ---------------------------------------------------------------------------
# test_requires_confirmation_raises_without_confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_confirmation_raises_without_confirm() -> None:
    """A plan with requires_confirmation=True must raise when confirm=False."""
    conn = _make_conn()
    plan = _plan_json(
        ops=[{"kind": "drop_table", "table": "users"}],
        requires_confirmation=True,
    )

    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(conn, plan, dry_run=False, confirm=False)


@pytest.mark.asyncio
async def test_requires_confirmation_applies_when_confirm_true() -> None:
    """A destructive plan proceeds when confirm=True."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn = _make_conn(pool=pool)
    plan = _plan_json(
        ops=[{"kind": "drop_table", "table": "users"}],
        requires_confirmation=True,
    )

    result = await apply(conn, plan, dry_run=False, confirm=True)

    assert result.applied is True
    assert result.ops_count == 1
    pool.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_destructive_op_with_forged_requires_confirmation_false_still_raises() -> None:
    """A destructive op must require confirm even if the plan lies with
    requires_confirmation=False.

    Regression guard for the orchestrator invariant: the destructive gate
    independently scans op kinds and never trusts the plan's own
    ``requires_confirmation`` flag (orchestrator.py). A crafted plan that sets
    the flag to False while including a ``drop_table`` op must still be blocked
    when ``confirm=False``.
    """
    pool = AsyncMock()
    conn = _make_conn(pool=pool)
    plan = _plan_json(
        ops=[{"kind": "drop_table", "table": "users"}],
        requires_confirmation=False,  # forged: claims "safe" while dropping a table
    )

    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(conn, plan, dry_run=False, confirm=False)

    pool.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_non_dev_env_raises_without_confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_dev_env_raises_without_confirm() -> None:
    """Non-development env without confirm must raise FerrumMigrationError."""
    conn = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    with pytest.raises(FerrumMigrationError, match="--confirm"):
        await apply(conn, plan, dry_run=False, confirm=False, env="production")


@pytest.mark.asyncio
async def test_non_dev_env_applies_when_confirm_true() -> None:
    """Non-development env with confirm=True must proceed."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn = _make_conn(pool=pool)
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    result = await apply(conn, plan, dry_run=False, confirm=True, env="staging")

    assert result.applied is True


# ---------------------------------------------------------------------------
# _op_to_sql: create_table — identifiers must be double-quoted
# ---------------------------------------------------------------------------


def test_create_table_sql_quoted_identifiers() -> None:
    """CREATE TABLE emits double-quoted table and column names."""
    op = {
        "kind": "create_table",
        "table": "user accounts",  # space in name — must still be quoted correctly
        "columns": [
            {"name": "id", "sql_type": "BIGSERIAL", "primary_key": True},
            {"name": "email address", "sql_type": "TEXT", "not_null": True},
        ],
    }
    sql = _op_to_sql(op)

    assert '"user accounts"' in sql
    assert '"id"' in sql
    assert '"email address"' in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "PRIMARY KEY" in sql
    assert "NOT NULL" in sql


def test_create_table_sql_with_default() -> None:
    """CREATE TABLE column with a DEFAULT clause is included."""
    op = {
        "kind": "create_table",
        "table": "items",
        "columns": [
            {"name": "active", "sql_type": "BOOLEAN", "not_null": True, "default": "true"},
        ],
    }
    sql = _op_to_sql(op)
    assert "DEFAULT true" in sql


# ---------------------------------------------------------------------------
# _op_to_sql: drop_table — must use IF EXISTS
# ---------------------------------------------------------------------------


def test_drop_table_sql_uses_if_exists() -> None:
    """DROP TABLE emits IF EXISTS and double-quotes the table name."""
    op = {"kind": "drop_table", "table": "old_logs"}
    sql = _op_to_sql(op)

    assert sql == 'DROP TABLE IF EXISTS "old_logs"'


# ---------------------------------------------------------------------------
# _op_to_sql: add_column
# ---------------------------------------------------------------------------


def test_add_column_sql_quoted_identifiers() -> None:
    """ADD COLUMN emits double-quoted table and column names."""
    op = {
        "kind": "add_column",
        "table": "users",
        "name": "age",
        "sql_type": "INT",
        "not_null": False,
        "primary_key": False,
    }
    sql = _op_to_sql(op)
    assert sql == 'ALTER TABLE "users" ADD COLUMN "age" INT'


def test_add_column_not_null() -> None:
    op = {
        "kind": "add_column",
        "table": "users",
        "name": "score",
        "sql_type": "FLOAT",
        "not_null": True,
        "primary_key": False,
    }
    sql = _op_to_sql(op)
    assert '"score" FLOAT NOT NULL' in sql


# ---------------------------------------------------------------------------
# _op_to_sql: drop_column — must use IF EXISTS
# ---------------------------------------------------------------------------


def test_drop_column_sql_uses_if_exists() -> None:
    op = {"kind": "drop_column", "table": "users", "column": "legacy_field"}
    sql = _op_to_sql(op)
    assert sql == 'ALTER TABLE "users" DROP COLUMN IF EXISTS "legacy_field"'


# ---------------------------------------------------------------------------
# _op_to_sql: rename_column
# ---------------------------------------------------------------------------


def test_rename_column_sql() -> None:
    op = {"kind": "rename_column", "table": "users", "from": "fname", "to": "first_name"}
    sql = _op_to_sql(op)
    assert 'RENAME COLUMN "fname" TO "first_name"' in sql
    assert '"users"' in sql


# ---------------------------------------------------------------------------
# _op_to_sql: add_index — unique flag
# ---------------------------------------------------------------------------


def test_add_index_unique_flag() -> None:
    """UNIQUE INDEX emits the UNIQUE keyword; non-unique does not."""
    unique_op = {
        "kind": "add_index",
        "name": "idx_users_email",
        "table": "users",
        "columns": ["email"],
        "unique": True,
    }
    non_unique_op = {**unique_op, "unique": False, "name": "idx_users_name"}

    unique_sql = _op_to_sql(unique_op)
    non_unique_sql = _op_to_sql(non_unique_op)

    assert "UNIQUE INDEX" in unique_sql
    assert "UNIQUE INDEX" not in non_unique_sql
    assert "IF NOT EXISTS" in unique_sql
    assert '"idx_users_email"' in unique_sql
    assert '"users"' in unique_sql
    assert '"email"' in unique_sql


def test_add_index_multi_column() -> None:
    op = {
        "kind": "add_index",
        "name": "idx_compound",
        "table": "orders",
        "columns": ["user_id", "created_at"],
        "unique": False,
    }
    sql = _op_to_sql(op)
    assert '"user_id", "created_at"' in sql


# ---------------------------------------------------------------------------
# _op_to_sql: drop_index
# ---------------------------------------------------------------------------


def test_drop_index_sql_uses_if_exists() -> None:
    op = {"kind": "drop_index", "name": "idx_old"}
    sql = _op_to_sql(op)
    assert sql == 'DROP INDEX IF EXISTS "idx_old"'


# ---------------------------------------------------------------------------
# _op_to_sql: raw_sql pass-through
# ---------------------------------------------------------------------------


def test_raw_sql_passthrough() -> None:
    stmt = "CREATE EXTENSION IF NOT EXISTS pgcrypto"
    op = {"kind": "raw_sql", "sql": stmt, "safe": True}
    assert _op_to_sql(op) == stmt


# ---------------------------------------------------------------------------
# _op_to_sql: unknown kind raises
# ---------------------------------------------------------------------------


def test_unknown_op_kind_raises() -> None:
    with pytest.raises(FerrumMigrationError, match="Unknown migration op kind"):
        _op_to_sql({"kind": "teleport_table", "table": "x"})


# ---------------------------------------------------------------------------
# apply(): multiple ops all executed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_executes_all_ops() -> None:
    """apply() calls pool.execute once per op in order."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn = _make_conn(pool=pool)

    plan = _plan_json(
        ops=[
            {"kind": "drop_table", "table": "a"},
            {"kind": "drop_table", "table": "b"},
            {"kind": "drop_table", "table": "c"},
        ]
    )
    result = await apply(conn, plan, dry_run=False, confirm=True)

    assert result.applied is True
    assert result.ops_count == 3
    assert pool.execute.await_count == 3


# ---------------------------------------------------------------------------
# apply(): empty plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_empty_plan() -> None:
    """An empty ops list applies successfully with ops_count=0."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn = _make_conn(pool=pool)

    result = await apply(conn, _plan_json(), dry_run=False, confirm=False)

    assert result.applied is True
    assert result.ops_count == 0
    pool.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# MigrationResult is exported from the top-level ferrum package
# ---------------------------------------------------------------------------


def test_migration_result_exported_from_top_level() -> None:
    assert hasattr(ferrum, "MigrationResult")
    assert ferrum.MigrationResult is MigrationResult


def test_ferrum_migration_error_exported_from_top_level() -> None:
    assert hasattr(ferrum, "FerrumMigrationError")
    assert ferrum.FerrumMigrationError is FerrumMigrationError


# ---------------------------------------------------------------------------
# compute_plan: fresh DB creates table
# ---------------------------------------------------------------------------


def test_compute_plan_fresh_db_creates_table() -> None:
    """Empty existing_tables → CreateTable op for every model."""

    class Article(ferrum.Model):
        id: int
        title: str

    plan = compute_plan([Article], existing_tables={})

    assert plan["ops"], "Expected at least one op"
    assert plan["ops"][0]["kind"] == "create_table"
    assert plan["ops"][0]["table"] == "article"
    col_names = [c["name"] for c in plan["ops"][0]["columns"]]
    assert "id" in col_names
    assert "title" in col_names


# ---------------------------------------------------------------------------
# compute_plan: existing table with missing column → AddColumn
# ---------------------------------------------------------------------------


def test_compute_plan_existing_table_adds_column() -> None:
    """Table exists but is missing a column → AddColumn op emitted."""

    class Product(ferrum.Model):
        id: int
        name: str
        price: float

    # Simulate DB that already has id and name but not price.
    plan = compute_plan([Product], existing_tables={"product": ["id", "name"]})

    assert len(plan["ops"]) == 1, f"Expected 1 op, got {plan['ops']}"
    op = plan["ops"][0]
    assert op["kind"] == "add_column"
    assert op["table"] == "product"
    assert op["name"] == "price"
    assert op["sql_type"] == "REAL"


# ---------------------------------------------------------------------------
# compute_plan: fully in-sync schema → no ops
# ---------------------------------------------------------------------------


def test_compute_plan_up_to_date_no_ops() -> None:
    """All columns present in existing_tables → empty ops list."""

    class Tag(ferrum.Model):
        id: int
        label: str

    plan = compute_plan([Tag], existing_tables={"tag": ["id", "label"]})

    assert plan["ops"] == [], f"Expected no ops, got {plan['ops']}"


# ---------------------------------------------------------------------------
# apply(): invalid token raises FerrumMigrationError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_with_invalid_token_raises() -> None:
    """apply(..., confirm=True, token='wrong') must raise FerrumMigrationError."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn = _make_conn(pool=pool)

    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    with pytest.raises(FerrumMigrationError, match=r"FERR-M001"):
        await apply(conn, plan, dry_run=False, confirm=True, token="wrong")  # noqa: S106
