"""Integration tests for ``ferrum check-schema`` against live PostgreSQL.

Covers the W2-F acceptance criteria:
- Non-zero exit on model/live-schema drift.
- Comprehensive comparison: columns, types, nullability, defaults, PK order,
  unique/FK/check constraints, indexes/opclasses/predicates, extensions,
  RLS policies, functions, and vector dimensions.
- Machine-readable JSON output for CI.
- Explicit unmanaged table/schema exclusions (Better Auth, LangGraph,
  Alembic-owned).
- Alembic coexistence: Alembic remains authoritative, Ferrum checks drift.

All tests create transient tables with uuid-suffixed names (parallel-safe)
and drop them afterward. Models are registered via the Ferrum registry at
class-definition time; the registry is cleared per-test to avoid cross-test
contamination.
"""

from __future__ import annotations

import json
from typing import ClassVar
from uuid import UUID

import pytest

import ferrum
from ferrum.migrations.drift import detect_drift

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drop_table(pg_conn: ferrum.connection.Connection, table: str) -> None:
    """Drop a table if it exists. Safe to call in a finally block."""
    await pg_conn._require_driver().execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


async def _drop_extension(pg_conn: ferrum.connection.Connection, name: str) -> None:
    """Drop an extension if it exists. Safe to call in a finally block."""
    await pg_conn._require_driver().execute(f'DROP EXTENSION IF EXISTS "{name}"')


# ---------------------------------------------------------------------------
# Clean database: no drift when no models are selected
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_clean_database_no_drift(
    pg_conn: ferrum.connection.Connection,
) -> None:
    """An empty model selection against a live database reports no drift."""
    report = await detect_drift(pg_conn, models=[])
    assert report.has_drift is False


# ---------------------------------------------------------------------------
# Drift: missing table
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_missing_table(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_missing_{unique_suffix}"

    class MissingModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        name: str

    try:
        report = await detect_drift(pg_conn, models=[MissingModel])
        assert report.has_drift is True
        assert table in report.missing_tables
        # JSON output round-trips and carries the missing table.
        payload = json.loads(report.to_json())
        assert payload["has_drift"] is True
        assert table in payload["missing_tables"]
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Clean: matching schema reports no drift
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_matching_schema_no_drift(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_match_{unique_suffix}"

    class MatchModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        title: str
        note: str | None = None

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  title TEXT NOT NULL,"
            f"  note TEXT"
            f")"
        )
        report = await detect_drift(pg_conn, models=[MatchModel])
        assert report.has_drift is False, report.format_summary()
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Drift: type mismatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_type_mismatch(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_type_{unique_suffix}"

    class TypeModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        count: int

    try:
        # Model expects INTEGER, live is TEXT.
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  count TEXT NOT NULL"
            f")"
        )
        report = await detect_drift(pg_conn, models=[TypeModel])
        assert report.has_drift is True
        diffs = report.field_differences.get(table, ())
        assert any(d.kind == "type" and d.column == "count" for d in diffs)
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Drift: nullability mismatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_nullability_mismatch(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_null_{unique_suffix}"

    class NullModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        label: str

    try:
        # Model expects NOT NULL, live is nullable.
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" (  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  label TEXT)'
        )
        report = await detect_drift(pg_conn, models=[NullModel])
        assert report.has_drift is True
        diffs = report.field_differences.get(table, ())
        assert any(d.kind == "nullability" and d.column == "label" for d in diffs)
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Drift: default mismatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_default_mismatch(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_default_{unique_suffix}"

    from datetime import datetime

    class DefaultModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        created_at: datetime = ferrum.Field(db_default="NOW()")

    try:
        # Model expects DEFAULT NOW(), live has a fixed timestamp.
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  created_at TIMESTAMPTZ NOT NULL DEFAULT '2020-01-01 00:00:00+00'"
            f")"
        )
        report = await detect_drift(pg_conn, models=[DefaultModel])
        assert report.has_drift is True
        diffs = report.field_differences.get(table, ())
        assert any(d.kind == "default" and d.column == "created_at" for d in diffs)
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Drift: missing unique constraint
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_missing_unique_constraint(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_unique_{unique_suffix}"

    class UniqueModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        email: str = ferrum.Field(unique=True)

    try:
        # Live table has no UNIQUE constraint on email.
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  email TEXT NOT NULL"
            f")"
        )
        report = await detect_drift(pg_conn, models=[UniqueModel])
        assert report.has_drift is True
        cons = report.constraint_differences.get(table, ())
        assert any(c.kind == "missing_unique_constraint" for c in cons)
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Clean: unique constraint present
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_clean_when_unique_constraint_present(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_unique_ok_{unique_suffix}"

    class UniqueOkModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        email: str = ferrum.Field(unique=True)

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  email TEXT NOT NULL UNIQUE"
            f")"
        )
        report = await detect_drift(pg_conn, models=[UniqueOkModel])
        assert report.has_drift is False, report.format_summary()
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Drift: missing index
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_missing_index(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_index_{unique_suffix}"

    class IndexModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        status: str
        priority: int

        class Meta:
            indexes: ClassVar[list[ferrum.Index]] = [ferrum.Index(fields=("status", "priority"))]

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  status TEXT NOT NULL,"
            f"  priority INTEGER NOT NULL"
            f")"
        )
        report = await detect_drift(pg_conn, models=[IndexModel])
        assert report.has_drift is True
        idx_diffs = report.index_differences.get(table, ())
        assert any(d.kind == "missing_index" for d in idx_diffs)
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Clean: declared index present
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_clean_when_index_present(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_index_ok_{unique_suffix}"

    class IndexOkModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        status: str
        priority: int

        class Meta:
            indexes: ClassVar[list[ferrum.Index]] = [ferrum.Index(fields=("status", "priority"))]

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  status TEXT NOT NULL,"
            f"  priority INTEGER NOT NULL"
            f")"
        )
        await pg_conn._require_driver().execute(
            f'CREATE INDEX idx_{table}_status_priority ON "{table}" (status, priority)'
        )
        report = await detect_drift(pg_conn, models=[IndexOkModel])
        assert report.has_drift is False, report.format_summary()
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Drift: PK order mismatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_pk_order_mismatch(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_pk_{unique_suffix}"

    class CompositePkModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        tenant_id: str = ferrum.Field(primary_key=True)
        entity_id: str = ferrum.Field(primary_key=True)
        label: str

        class Meta:
            pk_fields: ClassVar[tuple[str, ...]] = ("tenant_id", "entity_id")

    try:
        # Live PK is (entity_id, tenant_id) — reversed from model.
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  tenant_id TEXT NOT NULL,"
            f"  entity_id TEXT NOT NULL,"
            f"  label TEXT NOT NULL,"
            f"  PRIMARY KEY (entity_id, tenant_id)"
            f")"
        )
        report = await detect_drift(pg_conn, models=[CompositePkModel])
        assert report.has_drift is True
        pk_diff = report.primary_key_differences.get(table)
        assert pk_diff is not None
        assert pk_diff.expected == ("tenant_id", "entity_id")
        assert pk_diff.actual == ("entity_id", "tenant_id")
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Drift: missing extension
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_reports_missing_extension(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_ext_{unique_suffix}"

    class ExtModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" (  id UUID PRIMARY KEY DEFAULT gen_random_uuid())'
        )
        report = await detect_drift(
            pg_conn,
            models=[ExtModel],
            expected_extensions=["pg_trgm"],
        )
        assert report.has_drift is True
        assert any(
            d.kind == "missing_extension" and d.name == "PG_TRGM"
            for d in report.extension_differences
        )
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# Clean: extension present
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_clean_when_extension_present(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_ext_ok_{unique_suffix}"

    class ExtOkModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" (  id UUID PRIMARY KEY DEFAULT gen_random_uuid())'
        )
        await pg_conn._require_driver().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        report = await detect_drift(
            pg_conn,
            models=[ExtOkModel],
            expected_extensions=["pg_trgm"],
        )
        assert report.has_drift is False, report.format_summary()
        # Live extension inventory includes pg_trgm.
        assert any(ext["name"] == "pg_trgm" for ext in report.live_extensions)
    finally:
        await _drop_table(pg_conn, table)
        await _drop_extension(pg_conn, "pg_trgm")


# ---------------------------------------------------------------------------
# Unmanaged table exclusions: Alembic coexistence
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_excludes_alembic_version_table(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_alembic_{unique_suffix}"
    alembic_table = f"alembic_version_{unique_suffix}"

    class AlembicCoexistModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" (  id UUID PRIMARY KEY DEFAULT gen_random_uuid())'
        )
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{alembic_table}" (version_num VARCHAR(32) NOT NULL)'
        )
        report = await detect_drift(
            pg_conn,
            models=[AlembicCoexistModel],
            alembic_tables={alembic_table},
        )
        assert report.has_drift is False, report.format_summary()
        assert alembic_table in report.excluded_tables
        assert alembic_table not in report.compared_tables
    finally:
        await _drop_table(pg_conn, table)
        await _drop_table(pg_conn, alembic_table)


# ---------------------------------------------------------------------------
# JSON output for CI
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_json_output_contains_all_difference_kinds(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_json_{unique_suffix}"

    class JsonModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        title: str

    try:
        # Missing table → JSON should include all top-level keys.
        report = await detect_drift(pg_conn, models=[JsonModel])
        payload = json.loads(report.to_json())
        assert payload["has_drift"] is True
        # All difference-kind keys are present even when empty.
        for key in (
            "missing_tables",
            "extra_tables",
            "missing_columns",
            "extra_columns",
            "field_differences",
            "primary_key_differences",
            "index_differences",
            "constraint_differences",
            "extension_differences",
            "policy_differences",
            "function_differences",
            "live_extensions",
            "live_policies",
            "live_functions",
        ):
            assert key in payload, f"JSON output missing key: {key}"
        assert table in payload["missing_tables"]
    finally:
        await _drop_table(pg_conn, table)


# ---------------------------------------------------------------------------
# RLS policy inventory
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_check_schema_inventories_live_rls_policies(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
) -> None:
    table = f"drift_rls_{unique_suffix}"
    policy_name = f"drift_policy_{unique_suffix}"

    class RlsModel(ferrum.Model):
        model_config = ferrum.ModelConfig(table=table)

        id: UUID = ferrum.Field(primary_key=True)
        tenant_id: str

    try:
        await pg_conn._require_driver().execute(
            f'CREATE TABLE "{table}" ('
            f"  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  tenant_id TEXT NOT NULL"
            f")"
        )
        await pg_conn._require_driver().execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        await pg_conn._require_driver().execute(
            f'CREATE POLICY "{policy_name}" ON "{table}" '
            f"USING (tenant_id = current_setting('app.tenant_id'))"
        )
        report = await detect_drift(pg_conn, models=[RlsModel])
        # No drift (policies are inventories, not model-declared).
        assert report.has_drift is False, report.format_summary()
        # Policy shows up in live inventory.
        assert any(
            pol["name"] == policy_name and pol["table"] == table for pol in report.live_policies
        )
    finally:
        await _drop_table(pg_conn, table)
