"""Unit tests for ``QuerySet.update_instance()``.

Invariants:
- Targets the row by primary key (composite PKs supported) and delegates to
  the filtered ``update()`` path — allowlist, bound params, hooks inherited.
- Requires an unfiltered QuerySet, loaded non-sentinel PK values, and a
  non-empty, non-PK, non-deferred ``fields`` subset when given.
- Returns the driver row count (0 = missing/stale row).
"""

from __future__ import annotations

import json
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.queryset import QuerySet


class Widget(ferrum.Model):
    id: int = 0
    name: str = ""
    active: bool = True


class CompositeRow(ferrum.Model):
    id: Annotated[int, ferrum.Field(primary_key=True)] = 0
    first_seen_at: Annotated[str, ferrum.Field(primary_key=True)] = ""
    value: str = ""


def _mock_native_and_conn(*, status: str = "UPDATE 1") -> tuple[MagicMock, MagicMock]:
    mock_ext = MagicMock()
    mock_ext.compile_query.return_value = {
        "sql_text": "UPDATE widget SET ... WHERE ...",
        "bound_params": [],
        "fingerprint": "fp",
        "operation": "update",
    }
    mock_conn = MagicMock()
    mock_conn.dialect = "postgres"
    mock_driver = MagicMock()
    mock_driver.execute = AsyncMock(return_value=status)
    mock_conn._require_driver.return_value = mock_driver
    return mock_ext, mock_conn


def _compiled_update_ir(mock_ext: MagicMock) -> dict:
    ir = json.loads(mock_ext.compile_query.call_args[0][1])
    assert ir["operation"]["kind"] == "update"
    return ir


class TestUpdateInstanceDelegation:
    @pytest.mark.asyncio
    async def test_updates_selected_fields_filtered_by_pk(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()
        widget = Widget(id=7, name="new-name", active=False)

        with patch("ferrum.queryset._native_ext", mock_ext):
            count = await qs.update_instance(mock_conn, widget, fields=["name"])

        assert count == 1
        ir = _compiled_update_ir(mock_ext)
        assigned = [field_ref["name"] for field_ref, _v in ir["operation"]["assignments"]]
        assert assigned == ["name"]
        assert ir["predicate"]["filter"]["field"]["name"] == "id"

    @pytest.mark.asyncio
    async def test_fields_none_updates_all_non_pk_fields(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()

        with patch("ferrum.queryset._native_ext", mock_ext):
            await qs.update_instance(mock_conn, Widget(id=7, name="n", active=False))

        assigned = {
            field_ref["name"]
            for field_ref, _v in _compiled_update_ir(mock_ext)["operation"]["assignments"]
        }
        assert assigned == {"name", "active"}

    @pytest.mark.asyncio
    async def test_composite_pk_filters_on_all_pk_fields(self) -> None:
        qs: QuerySet[CompositeRow] = QuerySet(CompositeRow)
        mock_ext, mock_conn = _mock_native_and_conn()
        row = CompositeRow(id=1, first_seen_at="2026-01-01", value="v")

        with patch("ferrum.queryset._native_ext", mock_ext):
            await qs.update_instance(mock_conn, row, fields=["value"])

        ir = _compiled_update_ir(mock_ext)
        # Composite PK filter compiles to an AND predicate over both PK fields.
        predicate_str = json.dumps(ir["predicate"])
        assert '"id"' in predicate_str
        assert '"first_seen_at"' in predicate_str

    @pytest.mark.asyncio
    async def test_stale_row_returns_zero(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn(status="UPDATE 0")

        with patch("ferrum.queryset._native_ext", mock_ext):
            count = await qs.update_instance(mock_conn, Widget(id=7), fields=["name"])

        assert count == 0


class TestUpdateInstanceGuards:
    @pytest.mark.asyncio
    async def test_filtered_queryset_raises(self) -> None:
        qs = QuerySet(Widget).filter(active=True)
        with pytest.raises(FerrumCompileError, match="unfiltered"):
            await qs.update_instance(None, Widget(id=7))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_sentinel_pk_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="primary-key value"):
            await qs.update_instance(None, Widget(name="a"))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_empty_fields_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="at least one field"):
            await qs.update_instance(None, Widget(id=7), fields=[])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_unknown_field_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            await qs.update_instance(None, Widget(id=7), fields=["ghost"])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_pk_in_fields_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="primary-key field"):
            await qs.update_instance(None, Widget(id=7), fields=["id"])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_deferred_field_in_fields_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        widget = Widget.model_construct(id=7, name="a")
        object.__setattr__(widget, "__ferrum_deferred__", frozenset({"active"}))
        with pytest.raises(FerrumCompileError, match="deferred field"):
            await qs.update_instance(None, widget, fields=["active"])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_deferred_instance_full_row_form_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        widget = Widget.model_construct(id=7, name="a")
        object.__setattr__(widget, "__ferrum_deferred__", frozenset({"active"}))
        with pytest.raises(FerrumCompileError, match="fields"):
            await qs.update_instance(None, widget)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_deferred_pk_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        widget = Widget.model_construct(name="a")
        object.__setattr__(widget, "__ferrum_deferred__", frozenset({"id"}))
        with pytest.raises(FerrumCompileError, match="primary-key"):
            await qs.update_instance(None, widget, fields=["name"])  # type: ignore[arg-type]
