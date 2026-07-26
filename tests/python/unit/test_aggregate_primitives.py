from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumDangerApiError
from ferrum.expressions import Q
from ferrum.queryset import Aggregate, QuerySet


class Metric(ferrum.Model):
    id: int = 0
    category: str = ""
    amount: float = 0.0
    active: bool = False
    created_at: datetime = datetime(2024, 1, 1)


def test_aggregate_ir_is_typed_and_immutable() -> None:
    base = QuerySet(Metric)
    grouped = (
        base.group_by("category").date_trunc("created_at", "day", alias="day").having(total__gte=10)
    )

    ir, keys = grouped._build_aggregate_ir(
        {
            "rows": Aggregate.count(filter=Q(active=True)),
            "total": Aggregate.sum("amount"),
            "average": Aggregate.avg("amount"),
            "minimum": Aggregate.min("amount"),
            "maximum": Aggregate.max("amount"),
        }
    )

    assert base._aggregate_groups == []
    assert keys == ["category", "day", "rows", "total", "average", "minimum", "maximum"]
    assert ir["version"] == 4
    assert ir["operation"] == {"kind": "select", "fields": []}
    aggregation = ir["aggregation"]
    assert aggregation["groups"][0]["field"]["name"] == "category"
    assert aggregation["groups"][1]["granularity"] == "day"
    assert aggregation["aggregates"][0]["function"] == "count"
    assert aggregation["aggregates"][0]["filter"]["kind"] == "filter"
    assert aggregation["having"] == [
        {
            "aggregate_index": 1,
            "operator": "gte",
            "value": {"type": "int", "value": 10},
        }
    ]


def test_aggregate_rejects_unknown_and_wrong_typed_fields() -> None:
    with pytest.raises(FerrumCompileError):
        QuerySet(Metric)._build_aggregate_ir({"total": Aggregate.sum("missing")})
    with pytest.raises(FerrumCompileError):
        QuerySet(Metric)._build_aggregate_ir({"total": Aggregate.sum("category")})
    with pytest.raises(FerrumCompileError):
        QuerySet(Metric).date_trunc("amount", "day")


def test_having_rejects_unknown_alias_without_leaking_value() -> None:
    sentinel = "secret-value"
    with pytest.raises(FerrumCompileError) as exc_info:
        QuerySet(Metric).having(injected__gt=sentinel)._build_aggregate_ir(
            {"total": Aggregate.sum("amount")}
        )
    assert sentinel not in str(exc_info.value)


def test_result_keys_never_enter_sql_identifier_ir() -> None:
    injected_group_key = 'day"; DROP TABLE metrics;--'
    injected_aggregate_key = 'sum"); DELETE FROM metrics;--'
    ir, keys = (
        QuerySet(Metric)
        .date_trunc("created_at", "day", alias=injected_group_key)
        ._build_aggregate_ir({injected_aggregate_key: Aggregate.sum("amount")})
    )

    encoded_ir = json.dumps(ir)
    assert injected_group_key not in encoded_ir
    assert injected_aggregate_key not in encoded_ir
    assert keys == [injected_group_key, injected_aggregate_key]


@pytest.mark.asyncio
async def test_aggregate_terminal_maps_generated_aliases_to_result_keys() -> None:
    queryset = QuerySet(Metric).group_by("category")
    native = MagicMock()
    native.compile_query.return_value = {
        "sql_text": 'SELECT "category" AS "group_0", SUM("amount") AS "agg_0"',
        "bound_params": [],
        "fingerprint": "aggregate-fingerprint",
    }
    connection = MagicMock()
    connection.dialect = "postgres"
    driver = MagicMock()
    driver.fetch = AsyncMock(return_value=[{"group_0": "a", "agg_0": 12.5}])
    connection._require_driver.return_value = driver

    with patch("ferrum.queryset._native_ext", native):
        rows = await queryset.aggregate(connection, total=Aggregate.sum("amount"))

    assert rows == [{"category": "a", "total": 12.5}]
    driver.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_returning_is_filtered_compare_and_set() -> None:
    queryset = QuerySet(Metric).filter(id=7, active=False)
    native = MagicMock()
    native.compile_query.return_value = {
        "sql_text": 'UPDATE "metric" SET "active" = $1 WHERE "id" = $2 RETURNING *',
        "bound_params": [
            json.dumps({"type": "bool", "value": True}),
            json.dumps({"type": "int", "value": 7}),
        ],
        "fingerprint": "cas-fingerprint",
    }
    connection = MagicMock()
    connection.dialect = "postgres"
    driver = MagicMock()
    driver.fetch = AsyncMock(return_value=[{"id": 7, "active": True}])
    connection._require_driver.return_value = driver

    with patch("ferrum.queryset._native_ext", native):
        rows = await queryset.update_returning(connection, active=True)

    assert rows == [{"id": 7, "active": True}]
    driver.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_returning_preserves_unscoped_guard() -> None:
    with pytest.raises(FerrumDangerApiError):
        await QuerySet(Metric).update_returning(None, active=True)
