"""Integration tests for Phase 1 query expressiveness."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.expressions import Q

from .backends import Backend
from .schema import Column, transient_table


def _bool_default(backend: Backend) -> str:
    return "FALSE" if backend.name == "postgres" else "0"


def _item_model(table_name: str) -> type[ferrum.Model]:
    class Item(ferrum.Model):
        id: int = 0
        name: str = ""
        active: bool = False

        class Meta:
            table = table_name

    return Item


def _item_columns(backend: Backend) -> list[Column]:
    return [
        Column("id", "pk_serial"),
        Column("name", "text", null=False),
        Column("active", "bool", null=False, default=_bool_default(backend)),
    ]


@pytest.mark.integration
async def test_exists_returns_bool(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table = f"ferrum_int_exists_{unique_suffix}"
    model = _item_model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_item_columns(backend)):
        await model.objects.create(db_conn, name="a", active=True)
        assert await model.objects.filter(active=True).exists(db_conn) is True
        assert await model.objects.filter(active=False).exists(db_conn) is False


@pytest.mark.integration
async def test_values_returns_dicts(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table = f"ferrum_int_values_{unique_suffix}"
    model = _item_model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_item_columns(backend)):
        row = await model.objects.create(db_conn, name="alpha")
        rows = await model.objects.filter(id=row.id).values("id", "name").all(db_conn)
        assert rows == [{"id": row.id, "name": "alpha"}]


@pytest.mark.integration
async def test_q_or_filter(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table = f"ferrum_int_q_or_{unique_suffix}"
    model = _item_model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_item_columns(backend)):
        a = await model.objects.create(db_conn, name="a", active=True)
        b = await model.objects.create(db_conn, name="b", active=False)
        results = await model.objects.filter(Q(active=True) | Q(name="b")).all(db_conn)
        assert sorted(r.id for r in results) == sorted([a.id, b.id])
