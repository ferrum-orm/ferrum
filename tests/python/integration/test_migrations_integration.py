"""Integration tests for migration dry-run/apply and ledger against live PostgreSQL."""

from __future__ import annotations

import json

import pytest
from helpers import raw_pool

import ferrum
from ferrum.errors import FerrumMigrationError
from ferrum.migrations import MigrationResult, apply
from ferrum.migrations.ledger import (
    compute_digest,
    ensure_ledger,
    is_applied,
    record_applied,
)


def _create_table_plan(table: str) -> str:
    return json.dumps(
        {
            "name": f"int_{table}",
            "version": "1",
            "requires_confirmation": False,
            "ops": [
                {
                    "kind": "create_table",
                    "table": table,
                    "columns": [
                        {
                            "name": "id",
                            "sql_type": "BIGSERIAL",
                            "primary_key": True,
                            "not_null": True,
                        },
                        {"name": "label", "sql_type": "TEXT", "not_null": True},
                    ],
                }
            ],
        }
    )


@pytest.mark.integration
async def test_dry_run_does_not_touch_database(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_int_mig_dry_{unique_suffix}"
    plan = _create_table_plan(table)

    result = await apply(pg_conn, plan, dry_run=True)

    assert isinstance(result, MigrationResult)
    assert result.dry_run is True
    assert result.applied is False
    assert result.ops_count == 1

    pool = raw_pool(pg_conn)
    row = await pool.fetchrow(
        "SELECT 1 FROM information_schema.tables WHERE table_name = $1",
        table,
    )
    assert row is None


@pytest.mark.integration
async def test_apply_creates_table(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_int_mig_apply_{unique_suffix}"
    plan = _create_table_plan(table)
    drop_plan = json.dumps(
        {
            "name": f"drop_{table}",
            "version": "1",
            "ops": [{"kind": "drop_table", "table": table}],
        }
    )

    try:
        dry = await apply(pg_conn, plan, dry_run=True)
        assert dry.applied is False

        applied = await apply(pg_conn, plan, dry_run=False)
        assert applied.applied is True
        assert applied.ops_count == 1

        pool = raw_pool(pg_conn)
        row = await pool.fetchrow(
            "SELECT 1 FROM information_schema.tables WHERE table_name = $1",
            table,
        )
        assert row is not None
    finally:
        await apply(pg_conn, drop_plan, dry_run=False, confirm=True)


@pytest.mark.integration
async def test_destructive_apply_requires_confirm(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"ferrum_int_mig_dest_{unique_suffix}"
    create_plan = _create_table_plan(table)
    drop_plan = json.dumps(
        {
            "name": f"drop_{table}",
            "version": "1",
            "ops": [{"kind": "drop_table", "table": table}],
        }
    )

    await apply(pg_conn, create_plan, dry_run=False)

    with pytest.raises(FerrumMigrationError, match="confirm"):
        await apply(pg_conn, drop_plan, dry_run=False, confirm=False)

    result = await apply(pg_conn, drop_plan, dry_run=False, confirm=True)
    assert result.applied is True


@pytest.mark.integration
async def test_ledger_record_and_replay_guard(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    digest = compute_digest(f"migration_{unique_suffix}", "CREATE TABLE t();")

    await ensure_ledger(pg_conn)
    assert await is_applied(pg_conn, digest) is False

    await record_applied(
        pg_conn,
        digest,
        environment="development",
        description=f"migration_{unique_suffix}",
    )
    assert await is_applied(pg_conn, digest) is True

    with pytest.raises(FerrumMigrationError, match="already been applied"):
        await record_applied(
            pg_conn,
            digest,
            environment="development",
            description=f"migration_{unique_suffix}",
        )
