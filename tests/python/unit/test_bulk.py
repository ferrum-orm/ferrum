"""Unit tests for QuerySet bulk_create / bulk_update / bulk_delete IR builders."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumConfigError
from ferrum.queryset import QuerySet


class Widget(ferrum.Model):
    id: int = 0
    name: str = ""
    active: bool = True


class TestBulkIrBuilders:
    def test_build_bulk_insert_ir_rejects_unknown_field(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            qs._build_bulk_insert_ir([{"ghost": "x"}], returning=True)

    def test_build_bulk_insert_ir_requires_consistent_columns(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="same field set"):
            qs._build_bulk_insert_ir(
                [{"name": "a"}, {"name": "b", "active": False}],
                returning=True,
            )

    def test_build_bulk_insert_ir_shape(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_insert_ir(
            [{"name": "a", "active": True}, {"name": "b", "active": False}],
            returning=True,
        )
        assert ir["operation"]["kind"] == "bulk_insert"
        assert len(ir["operation"]["rows"]) == 2
        assert ir["operation"]["returning"] is True

    def test_build_bulk_update_ir_shape(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_update_ir(
            [(1, {"name": "x"}), (2, {"name": "y"})],
            ["name"],
        )
        assert ir["operation"]["kind"] == "bulk_update"
        assert ir["operation"]["pk_field"]["name"] == "id"
        assert len(ir["operation"]["rows"]) == 2

    def test_build_bulk_delete_ir_shape(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_delete_ir([1, 2, 3])
        assert ir["operation"]["kind"] == "bulk_delete"
        assert len(ir["operation"]["ids"]) == 3


class TestBulkWithoutExtension:
    @pytest.mark.asyncio
    async def test_bulk_create_raises_without_native(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.bulk_create(None, [{"name": "x"}])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_bulk_create_compiles_via_native(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext = MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "INSERT INTO widget ...",
            "bound_params": [],
            "fingerprint": "fp",
            "operation": "bulk_insert",
        }
        mock_conn = MagicMock()
        mock_conn.dialect = "postgres"
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_conn._require_driver.return_value = mock_pool

        with patch("ferrum.queryset._native_ext", mock_ext):
            result = await qs.bulk_create(mock_conn, [{"name": "a"}, {"name": "b"}])

        assert result == []
        called_ir = json.loads(mock_ext.compile_query.call_args[0][1])
        assert called_ir["operation"]["kind"] == "bulk_insert"
