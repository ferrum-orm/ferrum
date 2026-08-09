"""Integration tests for QuerySet bulk_create / bulk_update / bulk_delete."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferrum

from .helpers import transient_table


@pytest.mark.integration
async def test_bulk_create_update_delete_round_trip(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_bulk_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        label: str = ""
        qty: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            label TEXT NOT NULL,
            qty INT NOT NULL DEFAULT 0
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        created = await Item.objects.bulk_create(
            pg_conn,
            [{"label": "a", "qty": 1}, {"label": "b", "qty": 2}],
            batch_size=2,
        )
        assert len(created) == 2
        assert all(isinstance(row, Item) for row in created)
        assert created[0].id > 0

        for row in created:
            row.label = row.label.upper()
        updated = await Item.objects.bulk_update(pg_conn, created, ("label",), batch_size=2)
        assert updated == 2

        ids = [row.id for row in created]
        deleted = await Item.objects.bulk_delete(pg_conn, ids, batch_size=2)
        assert deleted == 2
        assert await Item.objects.count(pg_conn) == 0


@pytest.mark.integration
async def test_bulk_create_count_mode(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_bulk_cnt_{unique_suffix}"

    class Row(ferrum.Model):
        id: int = 0
        val: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            val INT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        count = await Row.objects.bulk_create(
            pg_conn,
            [{"val": i} for i in range(5)],
            returning=False,
            batch_size=2,
        )
        assert count == 5
        assert await Row.objects.count(pg_conn) == 5


@pytest.mark.integration
async def test_bulk_update_non_text_column_types(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """bulk_update casts VALUES placeholders to the DDL column type.

    A UUID primary key previously compiled to ``$1::text``, so the
    ``t.id = v.id`` join raised ``UndefinedFunction``; ``uuid[]``/``tsvector``
    columns failed the SET assignment and ``numeric`` columns failed parameter
    binding against a ``double precision`` cast.
    """
    table_name = f"ferrum_int_bulk_types_{unique_suffix}"

    class Ticket(ferrum.Model):
        id: Annotated[UUID, ferrum.Field(primary_key=True)]
        first_seen_at: Annotated[datetime, ferrum.Field(primary_key=True)]
        related_ids: list[UUID] = ferrum.Field(default_factory=list)
        amount: Decimal | None = None

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id UUID NOT NULL,
            first_seen_at TIMESTAMPTZ NOT NULL,
            related_ids UUID[] NOT NULL DEFAULT '{{}}',
            amount NUMERIC(12, 4),
            PRIMARY KEY (id, first_seen_at)
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        seen_at = datetime.now(UTC)
        row = await Ticket.objects.create(pg_conn, id=uuid4(), first_seen_at=seen_at)

        related = [uuid4()]
        row.related_ids = related
        row.amount = Decimal("1.0050")
        updated = await Ticket.objects.bulk_update(pg_conn, [row], ("related_ids", "amount"))
        assert updated == 1

        stored = await Ticket.objects.get(pg_conn, id=row.id, first_seen_at=seen_at)
        assert stored.related_ids == related
        assert stored.amount == Decimal("1.0050")
