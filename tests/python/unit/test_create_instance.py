"""Unit tests for the instance/dict input form of ``QuerySet.create()``.

Invariants:
- ``create(conn, obj)`` mirrors ``bulk_create()`` semantics: values from
  ``model_dump()``, auto-PK sentinel (``0``/``None``/``""``) dropped.
- The kwargs form is unchanged: exactly the given values, no sentinel drop.
- Mixed instance + kwargs input is rejected with a structured error.
- Instances with deferred fields are rejected (silent-clobber hazard).
- An insert that would be empty after sentinel-drop fails before SQL emission.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ferrum
from ferrum.errors import FerrumCompileError
from ferrum.queryset import QuerySet


class Widget(ferrum.Model):
    id: int = 0
    name: str = ""
    active: bool = True


class OnlyPk(ferrum.Model):
    id: int = 0


def _mock_native_and_conn(*, fetchrow_result: dict | None = None) -> tuple[MagicMock, MagicMock]:
    mock_ext = MagicMock()
    mock_ext.compile_query.return_value = {
        "sql_text": "INSERT INTO widget ... RETURNING *",
        "bound_params": [],
        "fingerprint": "fp",
        "operation": "insert",
    }
    mock_conn = MagicMock()
    mock_conn.dialect = "postgres"
    mock_driver = MagicMock()
    mock_driver.fetchrow = AsyncMock(
        return_value=fetchrow_result or {"id": 1, "name": "a", "active": True}
    )
    mock_conn._require_driver.return_value = mock_driver
    return mock_ext, mock_conn


def _compiled_insert_fields(mock_ext: MagicMock) -> list[str]:
    ir = json.loads(mock_ext.compile_query.call_args[0][1])
    assert ir["operation"]["kind"] == "insert"
    return [field_ref["name"] for field_ref, _value in ir["operation"]["values"]]


class TestCreateInstanceForm:
    @pytest.mark.asyncio
    async def test_create_with_instance_drops_pk_sentinel(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()
        widget = Widget(name="a", active=False)

        with patch("ferrum.queryset._native_ext", mock_ext):
            created = await qs.create(mock_conn, widget)

        fields = _compiled_insert_fields(mock_ext)
        assert "id" not in fields, "sentinel PK must be dropped so the DB default runs"
        assert set(fields) == {"name", "active"}
        assert isinstance(created, Widget)

    @pytest.mark.asyncio
    async def test_create_with_instance_keeps_real_pk(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()

        with patch("ferrum.queryset._native_ext", mock_ext):
            await qs.create(mock_conn, Widget(id=7, name="a"))

        assert "id" in _compiled_insert_fields(mock_ext)

    @pytest.mark.asyncio
    async def test_create_with_dict_form(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()

        with patch("ferrum.queryset._native_ext", mock_ext):
            await qs.create(mock_conn, {"id": 0, "name": "a"})

        fields = _compiled_insert_fields(mock_ext)
        assert fields == ["name"], "dict form shares instance-form sentinel semantics"

    @pytest.mark.asyncio
    async def test_create_does_not_mutate_passed_instance(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn(
            fetchrow_result={"id": 42, "name": "a", "active": True}
        )
        widget = Widget(name="a")

        with patch("ferrum.queryset._native_ext", mock_ext):
            created = await qs.create(mock_conn, widget)

        assert widget.id == 0, "the passed instance must never be mutated"
        assert created.id == 42
        assert created is not widget

    @pytest.mark.asyncio
    async def test_create_coerces_sqlite_integer_boolean_without_revalidation(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn(
            fetchrow_result={"id": 42, "name": "a", "active": 0}
        )

        with patch("ferrum.queryset._native_ext", mock_ext):
            created = await qs.create(mock_conn, name="a", active=False)

        assert created.active is False

    @pytest.mark.asyncio
    async def test_create_instance_and_kwargs_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()

        with (
            patch("ferrum.queryset._native_ext", mock_ext),
            pytest.raises(FerrumCompileError, match="not both"),
        ):
            await qs.create(mock_conn, Widget(name="a"), name="b")
        mock_ext.compile_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_all_sentinel_instance_raises_before_sql(self) -> None:
        qs: QuerySet[OnlyPk] = QuerySet(OnlyPk)
        mock_ext, mock_conn = _mock_native_and_conn()

        with (
            patch("ferrum.queryset._native_ext", mock_ext),
            pytest.raises(FerrumCompileError, match="at least one field"),
        ):
            await qs.create(mock_conn, OnlyPk())
        mock_ext.compile_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_deferred_instance_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()
        widget = Widget.model_construct(id=1, name="a")
        object.__setattr__(widget, "__ferrum_deferred__", frozenset({"active"}))

        with (
            patch("ferrum.queryset._native_ext", mock_ext),
            pytest.raises(FerrumCompileError, match="deferred"),
        ):
            await qs.create(mock_conn, widget)
        mock_ext.compile_query.assert_not_called()


class TestCreateKwargsFormUnchanged:
    @pytest.mark.asyncio
    async def test_kwargs_path_does_no_sentinel_drop(self) -> None:
        """``create(conn, id=0)`` still inserts a literal 0 (backward compat)."""
        qs: QuerySet[Widget] = QuerySet(Widget)
        mock_ext, mock_conn = _mock_native_and_conn()

        with patch("ferrum.queryset._native_ext", mock_ext):
            await qs.create(mock_conn, id=0, name="a")

        assert "id" in _compiled_insert_fields(mock_ext)

    @pytest.mark.asyncio
    async def test_model_field_named_obj_works_via_kwargs(self) -> None:
        """The positional param is ``_obj`` so a field named ``obj`` never collides."""

        class Odd(ferrum.Model):
            id: int = 0
            obj: str = ""

        qs: QuerySet[Odd] = QuerySet(Odd)
        mock_ext, mock_conn = _mock_native_and_conn(fetchrow_result={"id": 1, "obj": "x"})

        with patch("ferrum.queryset._native_ext", mock_ext):
            await qs.create(mock_conn, obj="x")

        assert _compiled_insert_fields(mock_ext) == ["obj"]


class TestBulkWriteDeferredRejection:
    @pytest.mark.asyncio
    async def test_bulk_create_deferred_instance_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        widget = Widget.model_construct(id=1, name="a")
        object.__setattr__(widget, "__ferrum_deferred__", frozenset({"active"}))
        mock_ext, mock_conn = _mock_native_and_conn()

        with (
            patch("ferrum.queryset._native_ext", mock_ext),
            pytest.raises(FerrumCompileError, match="deferred"),
        ):
            await qs.bulk_create(mock_conn, [widget])
        mock_ext.compile_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_update_deferred_instance_raises(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        widget = Widget.model_construct(id=1, name="a")
        object.__setattr__(widget, "__ferrum_deferred__", frozenset({"active"}))
        mock_ext, mock_conn = _mock_native_and_conn()

        with (
            patch("ferrum.queryset._native_ext", mock_ext),
            pytest.raises(FerrumCompileError, match="deferred"),
        ):
            await qs.bulk_update(mock_conn, [widget], ["name"])
        mock_ext.compile_query.assert_not_called()


class TestBulkCreateSentinelParity:
    def test_bulk_insert_ir_empty_after_sentinel_drop_raises(self) -> None:
        """Regression: an all-sentinel row must fail structurally, not emit ``INSERT ... ()``."""
        qs: QuerySet[OnlyPk] = QuerySet(OnlyPk)
        with pytest.raises(FerrumCompileError, match="at least one field"):
            qs._build_bulk_insert_ir([{"id": 0}], returning=True)

    def test_bulk_insert_ir_still_drops_sentinel_pk(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        ir = qs._build_bulk_insert_ir([{"id": 0, "name": "a"}], returning=True)
        row_fields = [field_ref["name"] for field_ref, _v in ir["operation"]["rows"][0]]
        assert row_fields == ["name"]
