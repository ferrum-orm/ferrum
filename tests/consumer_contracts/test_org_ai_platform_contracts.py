"""Live-PostgreSQL and source-inspection contract tests for Org AI Platform
(Onyx-fork) persistence patterns.

Most of Org AI Platform's distinctive patterns have no Ferrum equivalent at
all (schema-per-tenant routing, shard registry, ``SELECT ... FOR UPDATE
[SKIP LOCKED|NOWAIT]``), so those entries are proven by absence: a direct
check that the named API surface does not exist anywhere in ``ferrum``,
matching a repo-wide grep of ``python/ferrum/`` performed during the audit.
The two entries that are genuinely testable behavior differences (bulk_upsert
conflict semantics, schema-scoped drift detection) get a live-PostgreSQL
reproduction.

Manifest coverage: oai-01 (schema-per-tenant), oai-02 (shard routing),
oai-03/oai-04 (SELECT ... FOR UPDATE [SKIP LOCKED|NOWAIT]), oai-06 (nested
Pydantic JSONB field type), oai-07 (conditional-COALESCE upsert),
oai-10 (schema-scoped drift detection).
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated

import pydantic
import pytest

import ferrum
from ferrum.migrations import apply
from ferrum.migrations import operations as ops
from ferrum.migrations.drift import detect_drift
from ferrum.queryset import QuerySet


def test_schema_per_tenant_routing_is_not_available_missing_api() -> None:
    """Manifest oai-01: no schema_transaction()/schema_translate_map
    equivalent exists on Connection, Transaction, or connect()."""
    assert not hasattr(ferrum.connection.Connection, "schema_transaction")
    assert not hasattr(ferrum.connection.Connection, "with_schema")
    assert not hasattr(ferrum.session, "schema_transaction")
    connect_params = inspect.signature(ferrum.connect).parameters
    assert "schema" not in connect_params
    assert "schema_translate_map" not in connect_params


def test_shard_router_registry_is_not_available_missing_api() -> None:
    """Manifest oai-02: no multi-DSN connection registry/shard router type
    exists anywhere in the public ferrum namespace."""
    assert not hasattr(ferrum, "ShardRouter")
    assert not hasattr(ferrum, "ConnectionRegistry")
    assert not hasattr(ferrum.connection, "ShardRouter")
    assert not hasattr(ferrum.connection, "ConnectionRegistry")


def test_select_for_update_skip_locked_is_not_available_missing_api() -> None:
    """Manifest oai-03/oai-04: QuerySet has no row-lock modifier of any kind
    (plain FOR UPDATE, SKIP LOCKED, or NOWAIT)."""
    for name in ("select_for_update", "for_update", "lock", "with_for_update"):
        assert not hasattr(QuerySet, name), f"QuerySet unexpectedly has {name!r}"


def test_nested_pydantic_model_field_falls_back_to_text_type_missing_api() -> None:
    """Manifest oai-06: annotating a field with a nested Pydantic BaseModel
    (Org AI Platform's PydanticType TypeDecorator use case) does not raise
    and does not map to JSONB — it silently falls back to a plain TEXT
    column, which is a wrong-DDL-type defect distinct from a clean rejection.
    """

    class NestedPayload(pydantic.BaseModel):
        key: str = ""

    class DocWithNestedPayload(ferrum.Model):
        id: Annotated[int, ferrum.Field(primary_key=True)]
        payload: NestedPayload = NestedPayload()

    metadata = DocWithNestedPayload.get_metadata()
    payload_field = next(f for f in metadata.fields if f.name == "payload")
    assert payload_field.field_type == "text"
    assert payload_field.sql_type == "TEXT"


@pytest.mark.integration
async def test_bulk_upsert_cannot_express_conditional_coalesce_update(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Manifest oai-07: entities.py's transfer_entity
    ``on_conflict_do_update(set_=dict(entity_key=func.coalesce(KGEntity.entity_key,
    entity.entity_key), ...))`` cannot be expressed via bulk_upsert's static
    update_fields list — a new row's None always overwrites an existing
    non-null value.
    """
    kv_table = f"cc_oai_kv_{unique_suffix}"

    class Kv(ferrum.Model):
        class Meta:
            table = kv_table

        key: Annotated[str, ferrum.Field(primary_key=True)]
        value: str | None = None

    plan = json.dumps(
        {
            "name": f"cc_oai_kv_create_{unique_suffix}",
            "version": "1",
            "requires_confirmation": False,
            "ops": [
                ops.CreateTable(
                    kv_table,
                    [
                        ops.Column("key", "TEXT", not_null=True, primary_key=True),
                        ops.Column("value", "TEXT"),
                    ],
                ).to_op_dict()
            ],
        }
    )
    await apply(pg_conn, plan, dry_run=False)
    try:
        await Kv.objects.create(pg_conn, key="k1", value="original")

        incoming = Kv.model_construct(key="k1", value=None)
        await Kv.objects.bulk_upsert(
            pg_conn,
            [incoming],
            conflict_fields=["key"],
            update_fields=["value"],
            returning=False,
        )

        fetched = await Kv.objects.filter(key="k1").get(pg_conn)
        # This is the gap: a real COALESCE-based upsert would have preserved
        # "original" when the incoming value is None. Ferrum's bulk_upsert
        # always overwrites with the incoming value.
        assert fetched.value is None
    finally:
        drop_plan = json.dumps(
            {
                "name": f"cc_oai_kv_drop_{unique_suffix}",
                "version": "1",
                "requires_confirmation": False,
                "ops": [ops.DropTable(kv_table).to_op_dict()],
            }
        )
        await apply(pg_conn, drop_plan, dry_run=False, confirm=True)


@pytest.mark.integration
async def test_detect_drift_compares_a_named_non_public_schema(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Manifest oai-10: detect_drift(conn, models, schema=<tenant schema>)
    correctly reports missing/extra columns for a table created outside
    'public' — the read-only fidelity-check primitive Onyx's Alembic
    env.py (include_schemas=True + per-tenant schema selection) would need
    for a per-shard drift check, independent of Ferrum's numbered-SQL-only
    migration-apply policy.
    """
    schema_name = f"cc_oai_tenant_{unique_suffix}"
    table_name = f"cc_oai_doc_{unique_suffix}"
    driver = pg_conn._require_driver()
    await driver.execute(f'CREATE SCHEMA "{schema_name}"')
    try:
        # Live table has "legacy_col" (unknown to the model) and omits
        # "extra_field" (declared on the model but absent live).
        await driver.execute(
            f'CREATE TABLE "{schema_name}"."{table_name}" ('
            f"id BIGINT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
            f"legacy_col TEXT NOT NULL DEFAULT ''"
            f")"
        )

        class Doc(ferrum.Model):
            class Meta:
                table = table_name

            id: Annotated[int, ferrum.Field(primary_key=True)]
            name: str = ""
            extra_field: str = ""

        report = await detect_drift(pg_conn, [Doc], schema=schema_name)
        assert report.has_drift is True
        assert report.missing_columns.get(table_name) == ("extra_field",)
        assert report.extra_columns.get(table_name) == ("legacy_col",)
    finally:
        await driver.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
