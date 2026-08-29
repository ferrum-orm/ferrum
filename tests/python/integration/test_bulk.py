"""Integration tests for QuerySet bulk_create / bulk_update / bulk_delete."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferrum

from .backends import Backend, Capability
from .helpers import transient_table as pg_transient_table
from .schema import Column, transient_table


@pytest.mark.integration
async def test_bulk_create_delete_round_trip(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
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

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("label", "text", null=False),
            Column("qty", "int", null=False, default="0"),
        ],
    ) as conn:
        created = await Item.objects.bulk_create(
            conn,
            [{"label": "a", "qty": 1}, {"label": "b", "qty": 2}],
            batch_size=2,
        )
        assert len(created) == 2
        assert all(isinstance(row, Item) for row in created)
        assert created[0].id > 0

        ids = [row.id for row in created]
        deleted = await Item.objects.bulk_delete(conn, ids, batch_size=2)
        assert deleted == 2
        assert await Item.objects.count(conn) == 0


@pytest.mark.integration
async def test_bulk_create_update_delete_round_trip(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.BULK_UPDATE)

    table_name = f"ferrum_int_bulk_upd_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        label: str = ""
        qty: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("label", "text", null=False),
            Column("qty", "int", null=False, default="0"),
        ],
    ) as conn:
        created = await Item.objects.bulk_create(
            conn,
            [{"label": "a", "qty": 1}, {"label": "b", "qty": 2}],
            batch_size=2,
        )
        assert len(created) == 2

        for row in created:
            row.label = row.label.upper()
        updated = await Item.objects.bulk_update(conn, created, ("label",), batch_size=2)
        assert updated == 2

        ids = [row.id for row in created]
        deleted = await Item.objects.bulk_delete(conn, ids, batch_size=2)
        assert deleted == 2
        assert await Item.objects.count(conn) == 0


@pytest.mark.integration
async def test_bulk_create_count_mode(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_bulk_cnt_{unique_suffix}"

    class Row(ferrum.Model):
        id: int = 0
        val: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("val", "int", null=False),
        ],
    ) as conn:
        count = await Row.objects.bulk_create(
            conn,
            [{"val": i} for i in range(5)],
            returning=False,
            batch_size=2,
        )
        assert count == 5
        assert await Row.objects.count(conn) == 5


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

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
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


# ---------------------------------------------------------------------------
# W1-A: Cast matrix live round-trip — INTEGER[] / REAL / INTEGER casts
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_bulk_update_integer_array_cast_matches_ddl(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """bulk_update on an INTEGER[] column must not fail with SQLSTATE 42883.

    Previously ``postgres_value_cast`` emitted ``bigint[]`` for ``ArrayInt``,
    but the DDL produces ``INTEGER[]``. The ``t.col = v.col`` join predicate
    has no implicit cast between ``int4[]`` and ``int8[]``, so this would
    fail with ``UndefinedFunction``.
    """
    table_name = f"ferrum_int_bulk_arr_int_{unique_suffix}"

    class Slot(ferrum.Model):
        id: int = 0
        counts: list[int] = ferrum.Field(default_factory=list)

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            counts INTEGER[] NOT NULL DEFAULT '{{}}'
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        row = await Slot.objects.create(pg_conn, counts=[1, 2, 3])
        row.counts = [10, 20]
        updated = await Slot.objects.bulk_update(pg_conn, [row], ("counts",))
        assert updated == 1

        stored = await Slot.objects.get(pg_conn, id=row.id)
        assert stored.counts == [10, 20]


@pytest.mark.integration
async def test_bulk_update_real_cast_matches_ddl(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """bulk_update on a REAL (float4) column with a float field type.

    Previously ``postgres_value_cast`` emitted ``double precision`` for
    ``Float``, but the DDL produces ``REAL`` (float4). While PostgreSQL has
    an implicit cast, the cast should match the DDL exactly.
    """
    table_name = f"ferrum_int_bulk_real_{unique_suffix}"

    class Meter(ferrum.Model):
        id: int = 0
        reading: float = 0.0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            reading REAL NOT NULL DEFAULT 0.0
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        row = await Meter.objects.create(pg_conn, reading=1.5)
        row.reading = 2.5
        updated = await Meter.objects.bulk_update(pg_conn, [row], ("reading",))
        assert updated == 1

        stored = await Meter.objects.get(pg_conn, id=row.id)
        assert abs(stored.reading - 2.5) < 0.001


@pytest.mark.integration
async def test_bulk_update_integer_pk_cast_matches_ddl(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """bulk_update on a non-PK INTEGER column with an int PK.

    The PK is ``big_int`` (BIGSERIAL), but the update column ``count`` is
    ``int`` (INTEGER). The VALUES cast for ``count`` must be ``integer``,
    not ``bigint``.
    """
    table_name = f"ferrum_int_bulk_int_{unique_suffix}"

    class Counter(ferrum.Model):
        id: int = 0
        count: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with pg_transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        row = await Counter.objects.create(pg_conn, count=5)
        row.count = 42
        updated = await Counter.objects.bulk_update(pg_conn, [row], ("count",))
        assert updated == 1

        stored = await Counter.objects.get(pg_conn, id=row.id)
        assert stored.count == 42
