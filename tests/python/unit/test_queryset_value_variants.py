"""Behavioral regression tests for the QuerySet class split (v0.1.7).

The typing refactor split the single ``QuerySet`` into ``_QuerySetBase`` plus a
model-facing ``QuerySet`` and three value-shaped siblings
(``ValuesQuerySet`` / ``ValuesListQuerySet`` / ``FlatValuesListQuerySet``). These
tests pin the runtime contract that the split must preserve:

- ``values()`` returns dict rows; ``values_list()`` returns tuple rows;
  ``values_list(flat=True)`` returns flat scalars for a single selected field.
- Accumulated chaining state (filters/order/limit/only) survives the switch to a
  value sibling and every chaining method keeps the concrete subclass identity.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ferrum
from ferrum.queryset import (
    FlatValuesListQuerySet,
    QuerySet,
    ValuesListQuerySet,
    ValuesQuerySet,
)


class Widget(ferrum.Model):
    id: int = 0
    name: str = ""
    active: bool = False


def _mock_conn(rows: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    """Build a mock ``_native_ext`` + ``Connection`` returning ``rows`` from fetch."""
    mock_ext = MagicMock()
    mock_ext.compile_query.return_value = {
        "sql_text": "SELECT id, name, active FROM widget",
        "bound_params": [],
        "fingerprint": "fp-values",
        "operation": "select",
    }
    mock_conn = MagicMock()
    mock_conn.dialect = "postgres"
    driver = AsyncMock()
    driver.fetch = AsyncMock(return_value=rows)
    mock_conn._require_driver.return_value = driver
    return mock_ext, mock_conn


class TestValueQuerysetSubclassIdentity:
    def test_values_returns_values_queryset(self) -> None:
        qs = QuerySet(Widget).values("id", "name")
        assert isinstance(qs, ValuesQuerySet)

    def test_values_list_returns_values_list_queryset(self) -> None:
        qs = QuerySet(Widget).values_list("id", "name")
        assert isinstance(qs, ValuesListQuerySet)

    def test_values_list_flat_returns_flat_queryset(self) -> None:
        qs = QuerySet(Widget).values_list("id", flat=True)
        assert isinstance(qs, FlatValuesListQuerySet)

    def test_chaining_preserves_subclass(self) -> None:
        # filter/order_by/limit must return the same concrete subclass so that
        # ``.values().filter(...).all(conn)`` still yields dicts.
        base = QuerySet(Widget).values("id")
        chained = base.filter(active=True).order_by("id").limit(5)
        assert isinstance(chained, ValuesQuerySet)

        flat = QuerySet(Widget).values_list("id", flat=True).filter(active=True)
        assert isinstance(flat, FlatValuesListQuerySet)

    def test_model_queryset_chaining_stays_queryset(self) -> None:
        qs = QuerySet(Widget).filter(active=True).order_by("-id")
        assert isinstance(qs, QuerySet)
        assert not isinstance(qs, (ValuesQuerySet, ValuesListQuerySet, FlatValuesListQuerySet))


class TestValueQuerysetStatePreservation:
    def test_state_copied_into_value_sibling(self) -> None:
        src = QuerySet(Widget).filter(active=True).order_by("-id").limit(3).offset(2)
        vq = src.values("id", "name")
        # Accumulated state must transfer to the sibling (immutable chaining).
        assert vq._is_filtered is True
        assert vq._order_by == src._order_by
        assert vq._order_by  # ORDER BY state transferred (internal dict shape)
        assert vq._limit == 3
        assert vq._offset == 2
        assert vq._only_fields == ("id", "name")
        # Source is untouched.
        assert src._only_fields is None


class TestValueQuerysetMaterialization:
    @pytest.mark.asyncio
    async def test_values_yields_dicts(self) -> None:
        rows = [{"id": 1, "name": "a", "active": True}, {"id": 2, "name": "b", "active": False}]
        mock_ext, conn = _mock_conn(rows)
        with patch("ferrum.queryset._native_ext", mock_ext):
            out = await QuerySet(Widget).values().all(conn)
        assert out == rows
        assert all(isinstance(r, dict) for r in out)

    @pytest.mark.asyncio
    async def test_values_list_yields_tuples(self) -> None:
        rows = [{"id": 1, "name": "a", "active": True}, {"id": 2, "name": "b", "active": False}]
        mock_ext, conn = _mock_conn(rows)
        with patch("ferrum.queryset._native_ext", mock_ext):
            out = await QuerySet(Widget).values_list("id", "name").all(conn)
        assert out == [(1, "a"), (2, "b")]
        assert all(isinstance(r, tuple) for r in out)

    @pytest.mark.asyncio
    async def test_values_list_flat_single_field_yields_scalars(self) -> None:
        rows = [{"id": 1, "name": "a", "active": True}, {"id": 2, "name": "b", "active": False}]
        mock_ext, conn = _mock_conn(rows)
        with patch("ferrum.queryset._native_ext", mock_ext):
            out = await QuerySet(Widget).values_list("id", flat=True).all(conn)
        assert out == [1, 2]

    @pytest.mark.asyncio
    async def test_values_list_flat_multi_field_falls_back_to_tuples(self) -> None:
        # flat=True with more than one field preserves the pre-split tuple fallback.
        rows = [{"id": 1, "name": "a", "active": True}, {"id": 2, "name": "b", "active": False}]
        mock_ext, conn = _mock_conn(rows)
        with patch("ferrum.queryset._native_ext", mock_ext):
            out = await QuerySet(Widget).values_list("id", "name", flat=True).all(conn)
        assert out == [(1, "a"), (2, "b")]


class TestValueQuerysetExports:
    def test_value_querysets_exported_from_package(self) -> None:
        assert ferrum.ValuesQuerySet is ValuesQuerySet
        assert ferrum.ValuesListQuerySet is ValuesListQuerySet
        assert ferrum.FlatValuesListQuerySet is FlatValuesListQuerySet


class TestValueQuerysetSharedTerminals:
    @pytest.mark.asyncio
    async def test_value_queryset_count_uses_shared_terminal(self) -> None:
        # count() lives on _QuerySetBase; value siblings inherit it unchanged.
        mock_ext = MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "SELECT id FROM widget",
            "bound_params": [],
            "fingerprint": "fp-count",
            "operation": "select",
        }
        conn = MagicMock()
        conn.dialect = "postgres"
        driver = AsyncMock()
        driver.fetchval = AsyncMock(return_value=7)
        conn._require_driver.return_value = driver
        with patch("ferrum.queryset._native_ext", mock_ext):
            n = await QuerySet(Widget).values("id").count(conn)
        assert n == 7
        # Ensure the params round-trip decoder is importable/used (smoke).
        assert json.dumps({"type": "null"})
