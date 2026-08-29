"""Unit tests for the migration orchestrator.

Tests cover:
- Dry-run path: no DB calls, correct return value.
- Safety gates: destructive ops and non-dev env raise without confirm.
- DDL generation: identifiers are double-quoted, SQL shape is correct.
- Edge cases: unique index flag, IF EXISTS clauses.
- W1-C: transactional apply, advisory lock, atomic ledger, alter_column
  destructive confirm, non-transactional phase splitting.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


class _FakeRawConn:
    """Minimal fake asyncpg connection for transactional apply tests (W1-C).

    Supports ``transaction()`` as an async context manager, ``execute`` /
    ``fetchrow`` / ``fetch`` as AsyncMocks, and records SQL calls in order.
    """

    def __init__(self) -> None:
        self.execute = AsyncMock(return_value=None)
        self.fetchrow = AsyncMock(return_value=None)  # ledger check = not applied
        self.fetch = AsyncMock(return_value=[])
        self.executed_sql: list[str] = []

    @asynccontextmanager
    async def transaction(self, **kwargs: object) -> AsyncIterator[None]:
        yield None


def _make_conn(
    *,
    pool: object | None = None,
    raw_conn: _FakeRawConn | None = None,
    dialect: str = "postgres",
) -> tuple[MagicMock, _FakeRawConn]:
    """Return a (mock_conn, raw_conn) pair for apply tests (W1-C).

    For PostgreSQL, ``conn.acquire()`` yields the raw_conn and
    ``conn._require_driver()`` returns the pool/driver for pre/post-tx ops.
    For non-PostgreSQL, only ``_require_driver`` is used.
    """
    conn = MagicMock()
    conn.dialect = dialect
    raw = raw_conn or _FakeRawConn()

    @asynccontextmanager
    async def _acquire() -> AsyncIterator[_FakeRawConn]:
        yield raw

    conn.acquire = _acquire
    conn._require_driver.return_value = pool or AsyncMock()
    return conn, raw


# ---------------------------------------------------------------------------
# test_dry_run_returns_without_applying
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_returns_without_applying() -> None:
    """dry_run=True must return MigrationResult without calling execute."""
    conn, raw = _make_conn()

    plan = _plan_json(ops=[{"kind": "drop_table", "table": "old_users"}])
    result = await apply(conn, plan, dry_run=True)

    assert isinstance(result, MigrationResult)
    assert result.applied is False
    assert result.dry_run is True
    assert result.ops_count == 1

    # Pool/raw_conn must not be acquired at all during dry-run.
    conn._require_driver.assert_not_called()
    raw.execute.assert_not_called()


# ---------------------------------------------------------------------------
# test_requires_confirmation_raises_without_confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_confirmation_raises_without_confirm() -> None:
    """A plan with requires_confirmation=True must raise when confirm=False."""
    conn, _raw = _make_conn()
    plan = _plan_json(
        ops=[{"kind": "drop_table", "table": "users"}],
        requires_confirmation=True,
    )

    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(conn, plan, dry_run=False, confirm=False)


@pytest.mark.asyncio
async def test_requires_confirmation_applies_when_confirm_true() -> None:
    """A destructive plan proceeds when confirm=True (W1-C: transactional apply)."""
    conn, raw = _make_conn()
    plan = _plan_json(
        ops=[{"kind": "drop_table", "table": "users"}],
        requires_confirmation=True,
    )

    result = await apply(conn, plan, dry_run=False, confirm=True)

    assert result.applied is True
    assert result.ops_count == 1
    # The DROP TABLE SQL runs inside the transaction on the raw connection.
    raw.execute.assert_awaited()
    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    assert any("DROP TABLE" in s for s in executed)


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
    conn, raw = _make_conn()
    plan = _plan_json(
        ops=[{"kind": "drop_table", "table": "users"}],
        requires_confirmation=False,  # forged: claims "safe" while dropping a table
    )

    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(conn, plan, dry_run=False, confirm=False)

    raw.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_non_dev_env_raises_without_confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_dev_env_raises_without_confirm() -> None:
    """Non-development env without confirm must raise FerrumMigrationError."""
    conn, _raw = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    with pytest.raises(FerrumMigrationError, match="--confirm"):
        await apply(conn, plan, dry_run=False, confirm=False, env="production")


@pytest.mark.asyncio
async def test_non_dev_env_applies_when_confirm_true() -> None:
    """Non-development env with confirm=True must proceed."""
    conn, _raw = _make_conn()
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


def test_alter_column_sql_type_change() -> None:
    op = {
        "kind": "alter_column",
        "table": "items",
        "column": "qty",
        "sql_type": "BIGINT",
    }
    sql = _op_to_sql(op)
    assert 'ALTER TABLE "items"' in sql
    assert 'ALTER COLUMN "qty" TYPE BIGINT' in sql


def test_alter_column_set_not_null_is_destructive_class() -> None:
    from ferrum.migrations.operations import AlterColumn

    op = AlterColumn("items", "qty", not_null=True)
    assert op.classification == "destructive"


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
    """W1-C: apply() runs all transactional ops inside the pinned transaction."""
    conn, raw = _make_conn()

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
    # The 3 DROP TABLE statements run inside the transaction on raw_conn.
    # (raw.execute is also called for advisory lock, CREATE TABLE ledger,
    # SET LOCAL, ledger INSERT — so count >= 3, not == 3.)
    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    drop_count = sum(1 for s in executed if "DROP TABLE" in s)
    assert drop_count == 3


# ---------------------------------------------------------------------------
# apply(): empty plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_empty_plan() -> None:
    """An empty ops list applies successfully with ops_count=0."""
    conn, raw = _make_conn()

    result = await apply(conn, _plan_json(), dry_run=False, confirm=False)

    assert result.applied is True
    assert result.ops_count == 0
    # Advisory lock + ledger check + ledger INSERT still run even for empty plans.
    # No DDL op SQL is executed (only ledger management SQL).
    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    # Filter out ledger management SQL (advisory lock, CREATE TABLE ferrum_migrations, INSERT).
    ddl_ops = [
        s
        for s in executed
        if "ferrum_migrations" not in s and "pg_advisory" not in s and "SET LOCAL" not in s
    ]
    assert ddl_ops == [], f"Expected no DDL ops for empty plan, got: {ddl_ops}"


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


def test_compute_plan_add_column_emits_empty_string_default_for_text_field() -> None:
    class User(ferrum.Model):
        id: int
        email: str
        name: str = ferrum.Field(default="")

    plan = compute_plan([User], existing_tables={"user": ["id", "email"]})

    assert len(plan["ops"]) == 1
    op = plan["ops"][0]
    assert op["kind"] == "add_column"
    assert op["table"] == "user"
    assert op["name"] == "name"
    assert op["default"] == "''"


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
    conn, _raw = _make_conn()

    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    with pytest.raises(FerrumMigrationError, match=r"FERR-M001"):
        await apply(conn, plan, dry_run=False, confirm=True, token="wrong")  # noqa: S106


# ---------------------------------------------------------------------------
# W1-C: advisory lock, transactional apply, atomic ledger, destructive
# alter_column, non-transactional phases, replay guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_acquires_advisory_lock() -> None:
    """W1-C: apply() calls pg_advisory_xact_lock inside the transaction."""
    conn, raw = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    await apply(conn, plan, dry_run=False)

    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    assert any("pg_advisory_xact_lock" in s for s in executed), (
        f"Expected advisory lock acquisition, got: {executed}"
    )


@pytest.mark.asyncio
async def test_apply_replay_guard_rejects_duplicate() -> None:
    """W1-C: apply() checks the ledger inside the tx and rejects replays."""
    conn, raw = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    # Simulate the ledger already having this digest.
    raw.fetchrow = AsyncMock(return_value={"1": 1})

    with pytest.raises(FerrumMigrationError, match="already been applied"):
        await apply(conn, plan, dry_run=False)

    # No DDL op should have been executed (only the advisory lock + ledger check).
    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    assert not any('CREATE TABLE "t"' in s for s in executed)


@pytest.mark.asyncio
async def test_apply_writes_ledger_atomically() -> None:
    """W1-C: the ledger INSERT runs inside the same transaction as the DDL."""
    conn, raw = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    await apply(conn, plan, dry_run=False)

    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    # Ledger INSERT must be present.
    assert any("INSERT INTO ferrum_migrations" in s for s in executed), (
        f"Expected ledger INSERT, got: {executed}"
    )


@pytest.mark.asyncio
async def test_apply_failed_op_rolls_back_no_ledger() -> None:
    """W1-C: a failed DDL op rolls back the transaction; ledger is NOT written."""
    conn, raw = _make_conn()
    # The first DDL execute raises; advisory lock + ledger check succeed.
    call_count = 0

    async def _failing_execute(sql: str, *args: object) -> None:
        nonlocal call_count
        call_count += 1
        # Advisory lock (call 1), CREATE TABLE ledger (call 2), ledger check
        # is fetchrow not execute. The DDL op execute is the one we fail.
        # The create_table op emits "CREATE TABLE IF NOT EXISTS \"t\"".
        if '"t"' in sql and "ferrum_migrations" not in sql and "pg_advisory" not in sql:
            raise RuntimeError("simulated DDL failure")

    raw.execute = AsyncMock(side_effect=_failing_execute)
    raw.fetchrow = AsyncMock(return_value=None)  # not applied

    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    with pytest.raises(FerrumMigrationError, match="Failed to apply"):
        await apply(conn, plan, dry_run=False)

    # Ledger INSERT must NOT have been called (transaction rolled back).
    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    assert not any("INSERT INTO ferrum_migrations" in s for s in executed)


@pytest.mark.asyncio
async def test_alter_column_set_not_null_requires_confirm() -> None:
    """W1-C: alter_column SET NOT NULL hits the destructive confirm gate."""
    conn, _raw = _make_conn()
    plan = _plan_json(
        ops=[
            {"kind": "alter_column", "table": "t", "column": "c", "not_null": True},
        ]
    )

    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(conn, plan, dry_run=False, confirm=False)


@pytest.mark.asyncio
async def test_alter_column_type_narrowing_requires_confirm() -> None:
    """W1-C: alter_column type narrowing hits the destructive confirm gate."""
    conn, _raw = _make_conn()
    plan = _plan_json(
        ops=[
            {"kind": "alter_column", "table": "t", "column": "c", "sql_type": "INT"},
        ]
    )

    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(conn, plan, dry_run=False, confirm=False)


@pytest.mark.asyncio
async def test_alter_column_drop_not_null_does_not_require_confirm() -> None:
    """W1-C: alter_column DROP NOT NULL is safe (no confirm needed)."""
    conn, _raw = _make_conn()
    plan = _plan_json(
        ops=[
            {"kind": "alter_column", "table": "t", "column": "c", "not_null": False},
        ]
    )

    result = await apply(conn, plan, dry_run=False, confirm=False)

    assert result.applied is True


@pytest.mark.asyncio
async def test_non_transactional_post_tx_phase_runs_after_tx() -> None:
    """W1-C: CREATE INDEX CONCURRENTLY runs as a post-tx phase (autocommit)."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn, _raw = _make_conn(pool=pool)
    plan = _plan_json(
        ops=[
            {"kind": "create_table", "table": "t", "columns": []},
            {
                "kind": "add_index",
                "name": "idx_t_c",
                "table": "t",
                "columns": ["c"],
                "concurrently": True,
            },
        ]
    )

    result = await apply(conn, plan, dry_run=False)

    assert result.applied is True
    # The CONCURRENTLY index runs via driver.execute (post-tx, autocommit).
    pool_execute_calls = [call.args[0] for call in pool.execute.call_args_list if call.args]
    assert any("CREATE INDEX CONCURRENTLY" in s for s in pool_execute_calls), (
        f"Expected CONCURRENTLY index on driver, got: {pool_execute_calls}"
    )


@pytest.mark.asyncio
async def test_non_transactional_pre_tx_phase_runs_before_tx() -> None:
    """W1-C: CREATE EXTENSION runs as a pre-tx phase (autocommit)."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn, _raw = _make_conn(pool=pool)
    plan = _plan_json(
        ops=[
            {"kind": "create_extension", "name": "pgcrypto"},
            {"kind": "create_table", "table": "t", "columns": []},
        ]
    )

    result = await apply(conn, plan, dry_run=False)

    assert result.applied is True
    pool_execute_calls = [call.args[0] for call in pool.execute.call_args_list if call.args]
    assert any("CREATE EXTENSION" in s for s in pool_execute_calls), (
        f"Expected CREATE EXTENSION on driver (pre-tx), got: {pool_execute_calls}"
    )


@pytest.mark.asyncio
async def test_interspersed_non_transactional_ops_rejected() -> None:
    """W1-C: non-tx ops interspersed with tx ops are rejected (invalid plan)."""
    conn, _raw = _make_conn()
    plan = _plan_json(
        ops=[
            {"kind": "create_table", "table": "t", "columns": []},
            {
                "kind": "add_index",
                "name": "idx",
                "table": "t",
                "columns": ["c"],
                "concurrently": True,
            },
            {"kind": "add_column", "table": "t", "name": "c2", "sql_type": "TEXT"},
        ]
    )

    with pytest.raises(FerrumMigrationError, match="Invalid migration plan"):
        await apply(conn, plan, dry_run=False)


@pytest.mark.asyncio
async def test_lock_timeout_validation_rejects_invalid() -> None:
    """W1-C: lock_timeout must be a plain number+unit, not arbitrary SQL."""
    conn, _raw = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    with pytest.raises(FerrumMigrationError, match="Invalid lock_timeout"):
        await apply(conn, plan, dry_run=False, lock_timeout="5; DROP TABLE t")


@pytest.mark.asyncio
async def test_statement_timeout_validation_rejects_invalid() -> None:
    """W1-C: statement_timeout must be a plain number+unit, not arbitrary SQL."""
    conn, _raw = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    with pytest.raises(FerrumMigrationError, match="Invalid statement_timeout"):
        await apply(conn, plan, dry_run=False, statement_timeout="0; SELECT 1")


@pytest.mark.asyncio
async def test_lock_timeout_valid_applied_as_set_local() -> None:
    """W1-C: a valid lock_timeout is applied as SET LOCAL inside the tx."""
    conn, raw = _make_conn()
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    await apply(conn, plan, dry_run=False, lock_timeout="5s", statement_timeout="30s")

    executed = [call.args[0] for call in raw.execute.call_args_list if call.args]
    assert any("SET LOCAL lock_timeout = 5s" in s for s in executed)
    assert any("SET LOCAL statement_timeout = 30s" in s for s in executed)


@pytest.mark.asyncio
async def test_non_postgres_backend_uses_thin_parity() -> None:
    """W1-C: non-PostgreSQL backends keep autocommit-per-op (no advisory lock)."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn, _raw = _make_conn(pool=pool, dialect="mysql")
    plan = _plan_json(ops=[{"kind": "create_table", "table": "t", "columns": []}])

    result = await apply(conn, plan, dry_run=False)

    assert result.applied is True
    pool_execute_calls = [call.args[0] for call in pool.execute.call_args_list if call.args]
    # No advisory lock on MySQL.
    assert not any("pg_advisory" in s for s in pool_execute_calls)


# ---------------------------------------------------------------------------
# W1-C: alter_column classification (operations.py)
# ---------------------------------------------------------------------------


def test_alter_column_sql_type_change_is_destructive_class() -> None:
    """W1-C: AlterColumn with sql_type (type narrowing) is destructive."""
    from ferrum.migrations.operations import AlterColumn

    op = AlterColumn("items", "qty", sql_type="INT")
    assert op.classification == "destructive"


def test_alter_column_drop_not_null_is_safe_class() -> None:
    from ferrum.migrations.operations import AlterColumn

    op = AlterColumn("items", "qty", not_null=False)
    assert op.classification == "safe"


def test_alter_column_set_default_is_safe_class() -> None:
    from ferrum.migrations.operations import AlterColumn

    op = AlterColumn("items", "qty", default="0")
    assert op.classification == "safe"


# ---------------------------------------------------------------------------
# W1-C: AddIndex concurrently classification and SQL
# ---------------------------------------------------------------------------


def test_add_index_concurrently_is_non_transactional_class() -> None:
    from ferrum.migrations.operations import AddIndex

    op = AddIndex("t", "idx", ["c"], concurrently=True)
    assert op.classification == "non_transactional"


def test_add_index_concurrently_sql_emission() -> None:
    """W1-C: concurrently=True emits CREATE INDEX CONCURRENTLY (no IF NOT EXISTS)."""
    sql = _op_to_sql(
        {
            "kind": "add_index",
            "name": "idx_t_c",
            "table": "t",
            "columns": ["c"],
            "concurrently": True,
        }
    )
    assert "CREATE INDEX CONCURRENTLY" in sql
    assert "IF NOT EXISTS" not in sql
    assert '"idx_t_c"' in sql
    assert '"t"' in sql


def test_add_index_concurrently_rejected_on_non_postgres() -> None:
    """W1-C: CREATE INDEX CONCURRENTLY is PostgreSQL-only."""
    with pytest.raises(FerrumMigrationError, match="PostgreSQL-only"):
        _op_to_sql(
            {
                "kind": "add_index",
                "name": "idx",
                "table": "t",
                "columns": ["c"],
                "concurrently": True,
            },
            dialect="mysql",
        )
