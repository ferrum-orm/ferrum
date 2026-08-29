"""Unit tests for read-only PostgreSQL schema-fidelity comparison."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

import pytest

import ferrum
from ferrum.migrations.drift import (
    default_unmanaged_tables,
    detect_drift,
)


class _SchemaDriver:
    """Test double for the Ferrum async driver.

    Dispatches fetch calls by SQL pattern so each introspection query
    returns its own canned rows. Older tests that only supply ``columns``
    and ``primary_keys`` still work — the new queries default to empty.
    """

    def __init__(
        self,
        columns: list[dict[str, Any]],
        primary_keys: list[dict[str, Any]],
        *,
        indexes: list[dict[str, Any]] | None = None,
        constraints: list[dict[str, Any]] | None = None,
        extensions: list[dict[str, Any]] | None = None,
        policies: list[dict[str, Any]] | None = None,
        functions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.columns = columns
        self.primary_keys = primary_keys
        self.indexes = indexes or []
        self.constraints = constraints or []
        self.extensions = extensions or []
        self.policies = policies or []
        self.functions = functions or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        sql_lower = sql.lower()
        if "information_schema.columns" in sql_lower:
            return self.columns
        if "constraint_type = 'primary key'" in sql_lower:
            return self.primary_keys
        if "pg_index" in sql_lower:
            return self.indexes
        if "con.contype in ('u', 'f', 'c')" in sql_lower:
            return self.constraints
        # pg_proc must be checked before pg_extension because the function
        # query has a NOT EXISTS subquery against pg_extension/pg_depend.
        if "pg_proc" in sql_lower:
            return self.functions
        if "pg_extension" in sql_lower:
            return self.extensions
        if "pg_policies" in sql_lower:
            return self.policies
        return []


class _SchemaConnection:
    dialect = "postgres"

    def __init__(self, driver: _SchemaDriver) -> None:
        self.driver = driver

    def _require_driver(self) -> _SchemaDriver:
        return self.driver


class DriftTicket(ferrum.Model):
    model_config = ferrum.ModelConfig(table="drift_tickets")

    id: UUID = ferrum.Field(primary_key=True)
    labels: list[str] = ferrum.Field(default_factory=list, jsonb_list=True)
    watcher_ids: list[UUID] = ferrum.Field(default_factory=list)
    title: str
    note: str | None = None


_DEFAULT_TABLE = "drift_tickets"


def _column(
    name: str,
    data_type: str,
    *,
    udt_name: str,
    nullable: bool = False,
    max_length: int | None = None,
    default: str | None = None,
    table: str = _DEFAULT_TABLE,
) -> dict[str, Any]:
    return {
        "table_name": table,
        "column_name": name,
        "data_type": data_type,
        "udt_name": udt_name,
        "is_nullable": "YES" if nullable else "NO",
        "column_default": default,
        "character_maximum_length": max_length,
        "numeric_precision": None,
        "numeric_scale": None,
    }


def _matching_columns() -> list[dict[str, Any]]:
    return [
        _column("id", "uuid", udt_name="uuid", default="gen_random_uuid()"),
        _column("labels", "jsonb", udt_name="jsonb"),
        _column("watcher_ids", "ARRAY", udt_name="_uuid"),
        _column("title", "text", udt_name="text"),
        _column("note", "text", udt_name="text", nullable=True),
    ]


def _primary_key() -> list[dict[str, Any]]:
    return _primary_key_for(_DEFAULT_TABLE)


def _primary_key_for(table: str) -> list[dict[str, Any]]:
    return [{"table_name": table, "column_name": "id", "ordinal_position": 1}]


@pytest.mark.asyncio
async def test_detect_drift_accepts_matching_jsonb_list_and_uuid_array() -> None:
    driver = _SchemaDriver(_matching_columns(), _primary_key())
    report = await detect_drift(_SchemaConnection(driver), models=[DriftTicket])

    assert report.has_drift is False
    assert report.compared_tables == ("drift_tickets",)
    # Every schema-scoped query binds the schema name positionally; the
    # extension query is catalog-wide and carries no parameter.
    assert all(call[1] == ("public",) or call[1] == () for call in driver.calls)


@pytest.mark.asyncio
async def test_detect_drift_reports_actionable_type_nullability_and_pk_differences() -> None:
    columns = _matching_columns()
    columns[1] = _column("labels", "ARRAY", udt_name="_text")
    columns[4] = _column("note", "text", udt_name="text", nullable=False)
    driver = _SchemaDriver(columns, [])

    report = await detect_drift(_SchemaConnection(driver), models=["DriftTicket"])

    assert report.has_drift is True
    assert [(item.column, item.kind) for item in report.field_differences["drift_tickets"]] == [
        ("labels", "type"),
        ("note", "nullability"),
    ]
    assert report.field_differences["drift_tickets"][0].expected == "JSONB"
    assert report.field_differences["drift_tickets"][0].actual == "TEXT[]"
    assert report.primary_key_differences["drift_tickets"].expected == ("id",)
    assert report.primary_key_differences["drift_tickets"].actual == ()
    assert "numbered SQL remains authoritative" in report.format_summary()


@pytest.mark.asyncio
async def test_detect_drift_reports_missing_and_extra_columns() -> None:
    columns = _matching_columns()
    columns = [column for column in columns if column["column_name"] != "title"]
    columns.append(_column("legacy", "text", udt_name="text"))
    driver = _SchemaDriver(columns, _primary_key())

    report = await detect_drift(_SchemaConnection(driver), models=[DriftTicket])

    assert report.missing_columns == {"drift_tickets": ("title",)}
    assert report.extra_columns == {"drift_tickets": ("legacy",)}
    assert report.column_diffs == {
        "drift_tickets": {
            "missing_columns": ["title"],
            "extra_columns": ["legacy"],
        }
    }


@pytest.mark.asyncio
async def test_detect_drift_supports_explicit_auth_and_langgraph_carve_outs() -> None:
    columns = _matching_columns()
    columns.extend(
        [
            {
                **_column("id", "text", udt_name="text"),
                "table_name": "auth_sessions",
            },
            {
                **_column("thread_id", "text", udt_name="text"),
                "table_name": "langgraph_checkpoints",
            },
            {
                **_column("id", "text", udt_name="text"),
                "table_name": "unowned_table",
            },
        ]
    )
    driver = _SchemaDriver(columns, _primary_key())

    report = await detect_drift(
        _SchemaConnection(driver),
        models=[DriftTicket],
        auth_tables={"auth_sessions"},
        langgraph_tables={"langgraph_checkpoints"},
        include_unmapped_tables=True,
    )

    assert report.extra_tables == ("unowned_table",)
    assert "auth_sessions" in report.excluded_tables
    assert "langgraph_checkpoints" in report.excluded_tables


@pytest.mark.asyncio
async def test_detect_drift_with_empty_selection_ignores_unselected_tables() -> None:
    driver = _SchemaDriver(_matching_columns(), _primary_key())

    report = await detect_drift(_SchemaConnection(driver), models=[])

    assert report.has_drift is False
    assert report.compared_tables == ()


# ---------------------------------------------------------------------------
# New W2-F tests: defaults, indexes, constraints, extensions, RLS, functions,
# vector dimensions, Alembic carve-out, JSON output, schema exclusions.
# ---------------------------------------------------------------------------


class DriftDefaultModel(ferrum.Model):
    model_config = ferrum.ModelConfig(table="drift_default_models")

    id: UUID = ferrum.Field(primary_key=True)
    created_at: datetime = ferrum.Field(db_default="NOW()")
    label: str = ferrum.Field(default="")


@pytest.mark.asyncio
async def test_detect_drift_reports_default_mismatch() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_default_models"
        ),
        _column(
            "created_at",
            "timestamp with time zone",
            udt_name="timestamptz",
            default="2020-01-01 00:00:00+00",
            table="drift_default_models",
        ),
        _column("label", "text", udt_name="text", table="drift_default_models"),
    ]
    driver = _SchemaDriver(columns, _primary_key_for("drift_default_models"))

    report = await detect_drift(_SchemaConnection(driver), models=[DriftDefaultModel])

    assert report.has_drift is True
    default_diffs = [
        d for d in report.field_differences.get("drift_default_models", ()) if d.kind == "default"
    ]
    assert len(default_diffs) == 1
    assert default_diffs[0].column == "created_at"


@pytest.mark.asyncio
async def test_detect_drift_matches_default_with_type_cast_stripped() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_default_models"
        ),
        _column(
            "created_at",
            "timestamp with time zone",
            udt_name="timestamptz",
            default="now()::timestamptz",
            table="drift_default_models",
        ),
        _column("label", "text", udt_name="text", table="drift_default_models"),
    ]
    driver = _SchemaDriver(columns, _primary_key_for("drift_default_models"))

    report = await detect_drift(_SchemaConnection(driver), models=[DriftDefaultModel])

    assert report.has_drift is False, report.format_summary()


class DriftUniqueModel(ferrum.Model):
    model_config = ferrum.ModelConfig(table="drift_unique_models")

    id: UUID = ferrum.Field(primary_key=True)
    email: str = ferrum.Field(unique=True)


@pytest.mark.asyncio
async def test_detect_drift_reports_missing_unique_constraint() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_unique_models"
        ),
        _column("email", "text", udt_name="text", table="drift_unique_models"),
    ]
    driver = _SchemaDriver(
        columns,
        _primary_key_for("drift_unique_models"),
        constraints=[],  # No live unique constraint → drift.
    )

    report = await detect_drift(_SchemaConnection(driver), models=[DriftUniqueModel])

    assert report.has_drift is True
    cons = report.constraint_differences.get("drift_unique_models", ())
    assert any(c.kind == "missing_unique_constraint" for c in cons)


@pytest.mark.asyncio
async def test_detect_drift_clean_when_unique_constraint_present() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_unique_models"
        ),
        _column("email", "text", udt_name="text", table="drift_unique_models"),
    ]
    constraints = [
        {
            "table_name": "drift_unique_models",
            "constraint_name": "drift_unique_models_email_key",
            "constraint_type": "UNIQUE",
            "column_name": "email",
            "foreign_table": None,
            "foreign_column": None,
            "delete_rule": None,
            "definition": "UNIQUE (email)",
        }
    ]
    driver = _SchemaDriver(
        columns,
        _primary_key_for("drift_unique_models"),
        constraints=constraints,
    )

    report = await detect_drift(_SchemaConnection(driver), models=[DriftUniqueModel])

    assert report.has_drift is False, report.format_summary()


class DriftIndexModel(ferrum.Model):
    model_config = ferrum.ModelConfig(table="drift_index_models")

    id: UUID = ferrum.Field(primary_key=True)
    status: str
    priority: int

    class Meta:
        indexes: ClassVar[list[ferrum.Index]] = [ferrum.Index(fields=("status", "priority"))]


@pytest.mark.asyncio
async def test_detect_drift_reports_missing_index() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_index_models"
        ),
        _column("status", "text", udt_name="text", table="drift_index_models"),
        _column("priority", "integer", udt_name="int4", table="drift_index_models"),
    ]
    driver = _SchemaDriver(columns, _primary_key_for("drift_index_models"))

    report = await detect_drift(_SchemaConnection(driver), models=[DriftIndexModel])

    assert report.has_drift is True
    idx_diffs = report.index_differences.get("drift_index_models", ())
    assert any(d.kind == "missing_index" for d in idx_diffs)


@pytest.mark.asyncio
async def test_detect_drift_clean_when_index_present() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_index_models"
        ),
        _column("status", "text", udt_name="text", table="drift_index_models"),
        _column("priority", "integer", udt_name="int4", table="drift_index_models"),
    ]
    indexes = [
        {
            "table_name": "drift_index_models",
            "index_name": "idx_drift_index_models_status_priority",
            "is_unique": False,
            "using_method": "btree",
            "indexdef": (
                "CREATE INDEX idx_drift_index_models_status_priority "
                "ON drift_index_models USING btree (status, priority)"
            ),
            "predicate": None,
            "is_ready": True,
        }
    ]
    driver = _SchemaDriver(
        columns,
        _primary_key_for("drift_index_models"),
        indexes=indexes,
    )

    report = await detect_drift(_SchemaConnection(driver), models=[DriftIndexModel])

    assert report.has_drift is False, report.format_summary()


@pytest.mark.asyncio
async def test_detect_drift_reports_extra_index() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_index_models"
        ),
        _column("status", "text", udt_name="text", table="drift_index_models"),
        _column("priority", "integer", udt_name="int4", table="drift_index_models"),
    ]
    indexes = [
        {
            "table_name": "drift_index_models",
            "index_name": "idx_drift_index_models_status_priority",
            "is_unique": False,
            "using_method": "btree",
            "indexdef": (
                "CREATE INDEX idx_drift_index_models_status_priority "
                "ON drift_index_models USING btree (status, priority)"
            ),
            "predicate": None,
            "is_ready": True,
        },
        {
            "table_name": "drift_index_models",
            "index_name": "idx_drift_index_models_extra",
            "is_unique": False,
            "using_method": "btree",
            "indexdef": (
                "CREATE INDEX idx_drift_index_models_extra "
                "ON drift_index_models USING btree (legacy)"
            ),
            "predicate": None,
            "is_ready": True,
        },
    ]
    driver = _SchemaDriver(
        columns,
        _primary_key_for("drift_index_models"),
        indexes=indexes,
    )

    report = await detect_drift(_SchemaConnection(driver), models=[DriftIndexModel])

    idx_diffs = report.index_differences.get("drift_index_models", ())
    assert any(
        d.kind == "extra_index" and d.name == "idx_drift_index_models_extra" for d in idx_diffs
    )


@pytest.mark.asyncio
async def test_detect_drift_reports_reversed_composite_index_order() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_index_models"
        ),
        _column("status", "text", udt_name="text", table="drift_index_models"),
        _column("priority", "integer", udt_name="int4", table="drift_index_models"),
    ]
    indexes = [
        {
            "table_name": "drift_index_models",
            "index_name": "idx_drift_index_models_priority_status",
            "is_unique": False,
            "using_method": "btree",
            "indexdef": (
                "CREATE INDEX idx_drift_index_models_priority_status "
                "ON drift_index_models USING btree (priority, status)"
            ),
            "predicate": None,
            "is_ready": True,
        }
    ]
    driver = _SchemaDriver(
        columns,
        _primary_key_for("drift_index_models"),
        indexes=indexes,
    )

    report = await detect_drift(_SchemaConnection(driver), models=[DriftIndexModel])

    assert report.has_drift is True
    idx_diffs = report.index_differences.get("drift_index_models", ())
    kinds = {d.kind for d in idx_diffs}
    assert "missing_index" in kinds
    assert "extra_index" in kinds


@pytest.mark.asyncio
async def test_detect_drift_reports_missing_extension_when_expected() -> None:
    driver = _SchemaDriver(
        _matching_columns(),
        _primary_key(),
        extensions=[{"name": "plpgsql", "version": "1.0"}],
    )

    report = await detect_drift(
        _SchemaConnection(driver),
        models=[DriftTicket],
        expected_extensions=["pgvector"],
    )

    assert report.has_drift is True
    assert any(
        d.kind == "missing_extension" and d.name == "PGVECTOR" for d in report.extension_differences
    )


@pytest.mark.asyncio
async def test_detect_drift_inventories_live_extensions_without_expected() -> None:
    driver = _SchemaDriver(
        _matching_columns(),
        _primary_key(),
        extensions=[{"name": "pgvector", "version": "0.7.0"}],
    )

    report = await detect_drift(_SchemaConnection(driver), models=[DriftTicket])

    assert report.has_drift is False
    assert {"name": "pgvector", "version": "0.7.0"} in [dict(e) for e in report.live_extensions]


@pytest.mark.asyncio
async def test_detect_drift_inventories_live_policies_and_functions() -> None:
    policies = [
        {
            "schema": "public",
            "table_name": "drift_tickets",
            "name": "tenant_isolation",
            "permissive": "PERMISSIVE",
            "roles": "{ferrum_app}",
            "command": "ALL",
            "using_expr": "tenant_id = current_setting('app.tenant_id')",
            "check_expr": None,
        }
    ]
    functions = [
        {
            "name": "uuidv7",
            "arguments": "",
            "definition": "CREATE FUNCTION uuidv7() RETURNS uuid ...",
            "language": "sql",
        }
    ]
    driver = _SchemaDriver(
        _matching_columns(),
        _primary_key(),
        policies=policies,
        functions=functions,
    )

    report = await detect_drift(_SchemaConnection(driver), models=[DriftTicket])

    assert report.has_drift is False
    assert len(report.live_policies) == 1
    assert len(report.live_functions) == 1


@pytest.mark.asyncio
async def test_detect_drift_supports_alembic_carve_out() -> None:
    columns = _matching_columns()
    columns.append(
        {
            **_column("version_num", "character varying", udt_name="varchar"),
            "table_name": "alembic_version",
        }
    )
    driver = _SchemaDriver(columns, _primary_key())

    report = await detect_drift(
        _SchemaConnection(driver),
        models=[DriftTicket],
        alembic_tables={"alembic_version"},
        include_unmapped_tables=True,
    )

    assert "alembic_version" in report.excluded_tables
    assert "alembic_version" not in report.extra_tables


@pytest.mark.asyncio
async def test_detect_drift_rejects_schema_in_exclude_schemas() -> None:
    driver = _SchemaDriver(_matching_columns(), _primary_key())

    with pytest.raises(ValueError, match="exclude_schemas"):
        await detect_drift(
            _SchemaConnection(driver),
            models=[DriftTicket],
            schema="tenant_a",
            exclude_schemas={"tenant_a"},
        )


@pytest.mark.asyncio
async def test_detect_drift_to_json_round_trips() -> None:
    driver = _SchemaDriver(_matching_columns(), _primary_key())
    report = await detect_drift(_SchemaConnection(driver), models=[DriftTicket])

    payload = report.to_json()
    parsed = json.loads(payload)
    assert parsed["has_drift"] is False
    assert "compared_tables" in parsed
    assert "live_extensions" in parsed
    assert "live_policies" in parsed
    assert "live_functions" in parsed


@pytest.mark.asyncio
async def test_detect_drift_to_dict_includes_new_difference_kinds() -> None:
    columns = [
        _column(
            "id", "uuid", udt_name="uuid", default="gen_random_uuid()", table="drift_unique_models"
        ),
        _column("email", "text", udt_name="text", table="drift_unique_models"),
    ]
    driver = _SchemaDriver(columns, _primary_key_for("drift_unique_models"))

    report = await detect_drift(_SchemaConnection(driver), models=[DriftUniqueModel])

    payload = report.to_dict()
    assert "index_differences" in payload
    assert "constraint_differences" in payload
    assert "extension_differences" in payload
    assert "policy_differences" in payload
    assert "function_differences" in payload
    assert payload["has_drift"] is True


def test_default_unmanaged_tables_includes_better_auth_langgraph_alembic() -> None:
    tables = default_unmanaged_tables()
    assert "user" in tables
    assert "session" in tables
    assert "checkpoints" in tables
    assert "alembic_version" in tables
