"""Integration tests for get/first/count terminal semantics on live backends."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import FerrumMultipleObjectsError, FerrumNotFoundError

from .backends import Backend
from .schema import Column, transient_table


@pytest.mark.integration
async def test_get_returns_single_row(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_get_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        sku: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("sku", "text", null=False, extra="UNIQUE"),
        ],
    ) as conn:
        created = await Item.objects.create(conn, sku="ABC-1")
        fetched = await Item.objects.filter(sku="ABC-1").get(conn)
        assert fetched.id == created.id
        assert fetched.sku == "ABC-1"


@pytest.mark.integration
async def test_get_raises_multiple_objects(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_get_multi_{unique_suffix}"

    class Pair(ferrum.Model):
        id: int = 0
        group_id: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("group_id", "int", null=False),
        ],
    ) as conn:
        await Pair.objects.create(conn, group_id=7)
        await Pair.objects.create(conn, group_id=7)

        with pytest.raises(FerrumMultipleObjectsError):
            await Pair.objects.filter(group_id=7).get(conn)


@pytest.mark.integration
async def test_first_returns_none_when_empty(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_first_{unique_suffix}"

    class Empty(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
        ],
    ) as conn:
        assert await Empty.objects.first(conn) is None

        await Empty.objects.create(conn, name="only")
        first = await Empty.objects.order_by("id").first(conn)
        assert first is not None
        assert first.name == "only"


@pytest.mark.integration
async def test_count_respects_filters(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_count_{unique_suffix}"

    class Score(ferrum.Model):
        id: int = 0
        points: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("points", "int", null=False),
        ],
    ) as conn:
        for pts in (1, 5, 5, 10):
            await Score.objects.create(conn, points=pts)

        assert await Score.objects.count(conn) == 4
        assert await Score.objects.filter(points=5).count(conn) == 2


@pytest.mark.integration
async def test_get_raises_not_found(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_not_found_{unique_suffix}"

    class Ghost(ferrum.Model):
        id: int = 0
        code: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("code", "text", null=False),
        ],
    ) as conn:
        with pytest.raises(FerrumNotFoundError):
            await Ghost.objects.filter(code="missing").get(conn)
