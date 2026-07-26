"""Unit tests for read-only PostgreSQL schema-fidelity comparison."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

import ferrum
from ferrum.migrations.drift import detect_drift


class _SchemaDriver:
    def __init__(
        self,
        columns: list[dict[str, Any]],
        primary_keys: list[dict[str, Any]],
    ) -> None:
        self.columns = columns
        self.primary_keys = primary_keys
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if "table_constraints" in sql:
            return self.primary_keys
        return self.columns


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


def _column(
    name: str,
    data_type: str,
    *,
    udt_name: str,
    nullable: bool = False,
    max_length: int | None = None,
) -> dict[str, Any]:
    return {
        "table_name": "drift_tickets",
        "column_name": name,
        "data_type": data_type,
        "udt_name": udt_name,
        "is_nullable": "YES" if nullable else "NO",
        "character_maximum_length": max_length,
        "numeric_precision": None,
        "numeric_scale": None,
    }


def _matching_columns() -> list[dict[str, Any]]:
    return [
        _column("id", "uuid", udt_name="uuid"),
        _column("labels", "jsonb", udt_name="jsonb"),
        _column("watcher_ids", "ARRAY", udt_name="_uuid"),
        _column("title", "text", udt_name="text"),
        _column("note", "text", udt_name="text", nullable=True),
    ]


def _primary_key() -> list[dict[str, Any]]:
    return [
        {
            "table_name": "drift_tickets",
            "column_name": "id",
            "ordinal_position": 1,
        }
    ]


@pytest.mark.asyncio
async def test_detect_drift_accepts_matching_jsonb_list_and_uuid_array() -> None:
    driver = _SchemaDriver(_matching_columns(), _primary_key())
    report = await detect_drift(_SchemaConnection(driver), models=[DriftTicket])

    assert report.has_drift is False
    assert report.compared_tables == ("drift_tickets",)
    assert all(call[1] == ("public",) for call in driver.calls)


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
