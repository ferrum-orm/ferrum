"""Integration tests for get/first/count terminal semantics on live PostgreSQL."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import FerrumMultipleObjectsError, FerrumNotFoundError

from .helpers import transient_table


@pytest.mark.integration
async def test_get_returns_single_row(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_get_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        sku: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            sku TEXT NOT NULL UNIQUE
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        created = await Item.objects.create(pg_conn, sku="ABC-1")
        fetched = await Item.objects.filter(sku="ABC-1").get(pg_conn)
        assert fetched.id == created.id
        assert fetched.sku == "ABC-1"


@pytest.mark.integration
async def test_get_raises_multiple_objects(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_get_multi_{unique_suffix}"

    class Pair(ferrum.Model):
        id: int = 0
        group_id: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            group_id INT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        await Pair.objects.create(pg_conn, group_id=7)
        await Pair.objects.create(pg_conn, group_id=7)

        with pytest.raises(FerrumMultipleObjectsError):
            await Pair.objects.filter(group_id=7).get(pg_conn)


@pytest.mark.integration
async def test_first_returns_none_when_empty(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_first_{unique_suffix}"

    class Empty(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        assert await Empty.objects.first(pg_conn) is None

        await Empty.objects.create(pg_conn, name="only")
        first = await Empty.objects.order_by("id").first(pg_conn)
        assert first is not None
        assert first.name == "only"


@pytest.mark.integration
async def test_count_respects_filters(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_count_{unique_suffix}"

    class Score(ferrum.Model):
        id: int = 0
        points: int = 0

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            points INT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        for pts in (1, 5, 5, 10):
            await Score.objects.create(pg_conn, points=pts)

        assert await Score.objects.count(pg_conn) == 4
        assert await Score.objects.filter(points=5).count(pg_conn) == 2


@pytest.mark.integration
async def test_get_raises_not_found(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_not_found_{unique_suffix}"

    class Ghost(ferrum.Model):
        id: int = 0
        code: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            code TEXT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        with pytest.raises(FerrumNotFoundError):
            await Ghost.objects.filter(code="missing").get(pg_conn)
