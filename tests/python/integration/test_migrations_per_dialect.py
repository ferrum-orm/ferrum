"""Per-dialect migration coverage: dry-run, apply, revert, destructive gating.

All live tests are parameterized through the ``db_conn``/``backend`` registry
in ``conftest.py``, so they run against every backend whose DSN env var is set:

    FERRUM_TEST_DSN          → postgres
    FERRUM_TEST_MYSQL_DSN    → mysql
    FERRUM_TEST_SQLITE_DSN   → sqlite
    FERRUM_TEST_MSSQL_DSN    → mssql

The migration API under test is:
    ``ferrum.migrations.orchestrator.apply(conn, plan_json, *, dry_run, confirm)``
    where ``plan_json`` is a JSON string with an ``"ops"`` list.

API limitation note — ``makemigrations`` / ``migrate`` / ``revert``:
    The public ``ferrum makemigrations`` CLI scans ``Model.__subclasses__()``
    globally, writes migration files to disk, and stores ledger entries in the
    database.  Invoking it from a test would pollute the global model registry
    with transient test-only subclasses and leave migration files on disk.
    ``ferrum migrate`` / ``ferrum revert`` operate on ledger entries created by
    ``makemigrations``.  These three commands share mutable global state
    (model registry, filesystem, migration ledger) that cannot be safely reset
    between parallel test runs.  The lower-level ``orchestrator.apply()`` API
    is the correct surface for isolated per-dialect round-trip testing: it takes
    an explicit plan JSON, has no global side effects, and exercises the same
    SQL-generation, dialect-mapping, and destructive-confirmation code paths as
    the CLI commands.  Until a test-safe makemigrations scaffold is added to
    the harness, dry-run / apply / reverse via ``orchestrator.apply`` is the
    per-dialect regression gate.
"""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import ferrum.connection
from ferrum.errors import FerrumMigrationError
from ferrum.migrations.operations import (
    Column,
    CreateExtension,
    CreatePolicy,
    CreateTable,
    DropTable,
    EnableRLS,
)
from ferrum.migrations.orchestrator import apply as migrations_apply

from .backends import Backend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_plan_json(table: str) -> str:
    """JSON migration plan: CREATE TABLE with SERIAL pk + TEXT column.

    ``SERIAL`` is mapped by the orchestrator's ``_map_sql_type`` to
    dialect-appropriate DDL:
      - postgres: ``SERIAL``
      - mssql:    ``INT IDENTITY(1,1)``
      - mysql:    ``SERIAL`` (alias for BIGINT UNSIGNED NOT NULL AUTO_INCREMENT)
      - sqlite:   ``SERIAL`` (accepted by SQLite's flexible type affinity)
    """
    op = CreateTable(
        table_name=table,
        columns=[
            Column("id", "SERIAL", primary_key=True),
            Column("label", "TEXT", not_null=True),
        ],
    )
    return json.dumps({"ops": [op.to_op_dict()]})


def _drop_plan_json(table: str) -> str:
    op = DropTable(table_name=table)
    return json.dumps({"ops": [op.to_op_dict()]})


def _table_exists_sql(table: str, dialect: str) -> str:
    """Return a per-dialect SQL query that yields a single ``cnt`` column.

    SQLite does not implement ``information_schema``; it uses ``sqlite_master``
    instead.  MySQL's ``information_schema.tables`` is scoped to
    ``DATABASE()`` to avoid false positives from identically-named tables in
    other schemas.  PostgreSQL is scoped to ``table_schema='public'``.
    MSSQL's ``INFORMATION_SCHEMA.TABLES`` is case-insensitive.
    """
    if dialect == "sqlite":
        return f"SELECT count(*) AS cnt FROM sqlite_master WHERE type='table' AND name='{table}'"
    if dialect == "mysql":
        return (
            f"SELECT count(*) AS cnt FROM information_schema.tables "
            f"WHERE table_name = '{table}' AND table_schema = DATABASE()"
        )
    if dialect == "mssql":
        return f"SELECT count(*) AS cnt FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '{table}'"
    # postgres (default)
    return (
        f"SELECT count(*) AS cnt FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{table}'"
    )


def _drop_table_sql(table: str, dialect: str) -> str:
    """Return a per-dialect DROP TABLE statement for cleanup in finally blocks."""
    if dialect == "mssql":
        # MSSQL does not support DROP TABLE IF EXISTS; use OBJECT_ID guard.
        return f"IF OBJECT_ID(N'[{table}]') IS NOT NULL DROP TABLE [{table}]"
    if dialect == "mysql":
        return f"DROP TABLE IF EXISTS `{table}`"
    # postgres and sqlite both accept standard syntax.
    return f'DROP TABLE IF EXISTS "{table}"'


async def _count(driver: object, table: str, dialect: str) -> int:
    rows = await driver.fetch(_table_exists_sql(table, dialect))  # type: ignore[union-attr]
    return int(rows[0]["cnt"]) if rows else 0


# ---------------------------------------------------------------------------
# Live round-trip tests (parameterized over all active backends)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dry_run_no_side_effects(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    unique_suffix: str,
) -> None:
    """dry_run=True returns applied=False and must not create the table on any backend."""
    table = f"ferrum_mig_dr_{unique_suffix}"
    plan_json = _create_plan_json(table)

    result = await migrations_apply(db_conn, plan_json, dry_run=True)
    assert not result.applied, f"[{backend.name}] dry_run result.applied must be False"
    assert result.dry_run, f"[{backend.name}] dry_run result.dry_run must be True"

    driver = db_conn._require_driver()
    cnt = await _count(driver, table, backend.name)
    assert cnt == 0, f"[{backend.name}] dry_run created table '{table}' — it must not have"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_apply_and_revert_round_trip(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    unique_suffix: str,
) -> None:
    """Full round-trip: apply creates the table; drop plan with confirm=True removes it.

    This proves that the orchestrator's apply / destructive-confirm / reverse
    code paths work end-to-end on every active backend including MSSQL.
    """
    table = f"ferrum_mig_ap_{unique_suffix}"
    create_json = _create_plan_json(table)
    drop_json = _drop_plan_json(table)
    driver = db_conn._require_driver()

    try:
        # --- Apply phase ---
        result = await migrations_apply(db_conn, create_json, dry_run=False)
        assert result.applied, f"[{backend.name}] apply result.applied must be True"

        cnt = await _count(driver, table, backend.name)
        assert cnt == 1, (
            f"[{backend.name}] table '{table}' not found in schema after apply(dry_run=False)"
        )

        # --- Revert phase (destructive requires explicit confirm=True) ---
        drop_result = await migrations_apply(db_conn, drop_json, dry_run=False, confirm=True)
        assert drop_result.applied, (
            f"[{backend.name}] drop result.applied must be True when confirm=True"
        )

        cnt = await _count(driver, table, backend.name)
        assert cnt == 0, (
            f"[{backend.name}] table '{table}' still exists in schema after drop plan applied"
        )
    finally:
        with contextlib.suppress(Exception):
            await driver.execute(_drop_table_sql(table, backend.name))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_destructive_apply_requires_confirm(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    unique_suffix: str,
) -> None:
    """Applying a DropTable plan without confirm=True raises FerrumMigrationError."""
    table = f"ferrum_mig_dc_{unique_suffix}"
    create_json = _create_plan_json(table)
    drop_json = _drop_plan_json(table)
    driver = db_conn._require_driver()

    await migrations_apply(db_conn, create_json, dry_run=False)
    try:
        with pytest.raises(FerrumMigrationError, match=r"confirm"):
            await migrations_apply(db_conn, drop_json, dry_run=False)
        # Table must still exist — the operation was rejected before execution.
        cnt = await _count(driver, table, backend.name)
        assert cnt == 1, (
            f"[{backend.name}] table '{table}' was dropped despite missing confirm=True"
        )
    finally:
        with contextlib.suppress(Exception):
            await driver.execute(_drop_table_sql(table, backend.name))


# ---------------------------------------------------------------------------
# Unit tests: MSSQL rejects unsupported operation kinds (no live DB needed)
# ---------------------------------------------------------------------------


def _mock_mssql_conn() -> AsyncMock:
    conn = AsyncMock()
    driver = MagicMock()
    driver.dialect = "mssql"
    driver.execute = AsyncMock()
    conn._require_driver.return_value = driver
    conn.dialect = "mssql"
    return conn


class TestMssqlUnsupportedOpRejects:
    """MSSQL orchestrator raises FerrumMigrationError for unsupported operations."""

    @pytest.mark.asyncio
    async def test_create_extension_rejected_on_mssql(self) -> None:
        """create_extension is PostgreSQL-only; must fail before touching the driver."""
        conn = _mock_mssql_conn()
        plan_json = json.dumps({"ops": [CreateExtension(name="vector").to_op_dict()]})
        with pytest.raises(FerrumMigrationError, match=r"[Mm][Ss][Ss][Qq][Ll]"):
            await migrations_apply(conn, plan_json, dry_run=False)
        conn._require_driver.return_value.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_rls_rejected_on_mssql(self) -> None:
        """enable_rls is PostgreSQL-only; must fail before touching the driver."""
        conn = _mock_mssql_conn()
        plan_json = json.dumps({"ops": [EnableRLS(table_name="tickets").to_op_dict()]})
        with pytest.raises(FerrumMigrationError, match=r"[Mm][Ss][Ss][Qq][Ll]"):
            await migrations_apply(conn, plan_json, dry_run=False)
        conn._require_driver.return_value.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_policy_rejected_on_mssql(self) -> None:
        """create_policy is PostgreSQL-only; must fail before touching the driver."""
        conn = _mock_mssql_conn()
        plan_json = json.dumps(
            {
                "ops": [
                    CreatePolicy(
                        table_name="tickets",
                        policy_name="team_isolation",
                        using="team_id = current_setting('app.team_id')::uuid",
                    ).to_op_dict()
                ]
            }
        )
        with pytest.raises(FerrumMigrationError, match=r"[Mm][Ss][Ss][Qq][Ll]"):
            await migrations_apply(conn, plan_json, dry_run=False)
        conn._require_driver.return_value.execute.assert_not_called()
