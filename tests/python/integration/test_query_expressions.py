"""Integration tests for Phase 1 query expressiveness."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.expressions import Q

from .helpers import transient_table


def _item_model(table_name: str) -> type[ferrum.Model]:
    class Item(ferrum.Model):
        id: int = 0
        name: str = ""
        active: bool = False

        class Meta:
            table = table_name

    return Item


_CREATE = """
    CREATE TABLE "{t}" (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT false
    )
"""


@pytest.mark.integration
async def test_exists_returns_bool(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_exists_{unique_suffix}"
    model = _item_model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        await model.objects.create(pg_conn, name="a", active=True)
        assert await model.objects.filter(active=True).exists(pg_conn) is True
        assert await model.objects.filter(active=False).exists(pg_conn) is False


@pytest.mark.integration
async def test_values_returns_dicts(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_values_{unique_suffix}"
    model = _item_model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        row = await model.objects.create(pg_conn, name="alpha")
        rows = await model.objects.filter(id=row.id).values("id", "name").all(pg_conn)
        assert rows == [{"id": row.id, "name": "alpha"}]


@pytest.mark.integration
async def test_q_or_filter(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_q_or_{unique_suffix}"
    model = _item_model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        a = await model.objects.create(pg_conn, name="a", active=True)
        b = await model.objects.create(pg_conn, name="b", active=False)
        results = await model.objects.filter(Q(active=True) | Q(name="b")).all(pg_conn)
        assert sorted(r.id for r in results) == sorted([a.id, b.id])
