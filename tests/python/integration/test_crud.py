"""Integration tests for QuerySet CRUD terminals against live PostgreSQL.

Invariants:
- create() inserts via compiled INSERT … RETURNING and hydrates a model instance.
- update()/delete() require filters; scoped mutations return affected row counts.
- danger_delete_all() is the explicit unscoped delete escape hatch.
"""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import FerrumDangerApiError

from .helpers import raw_pool, seed_int_rows, transient_table


@pytest.mark.integration
async def test_create_returns_hydrated_instance(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_crud_create_{unique_suffix}"

    class Article(ferrum.Model):
        id: int = 0
        title: str = ""
        published: bool = False

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            published BOOLEAN NOT NULL DEFAULT FALSE
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        row = await Article.objects.create(pg_conn, title="hello", published=True)

        assert isinstance(row, Article)
        assert row.title == "hello"
        assert row.published is True
        assert row.id > 0


@pytest.mark.integration
async def test_update_and_delete_scoped_mutations(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_crud_mut_{unique_suffix}"

    class Tag(ferrum.Model):
        id: int = 0
        label: str = ""
        active: bool = True

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            label TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        await Tag.objects.create(pg_conn, label="keep", active=True)
        await Tag.objects.create(pg_conn, label="drop-me", active=False)

        updated = await Tag.objects.filter(active=False).update(pg_conn, label="archived")
        assert updated == 1

        deleted = await Tag.objects.filter(label="archived").delete(pg_conn)
        assert deleted == 1

        remaining = await Tag.objects.count(pg_conn)
        assert remaining == 1


@pytest.mark.integration
async def test_unscoped_delete_requires_danger_api(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    class Ephemeral(ferrum.Model):
        id: int = 0

    with pytest.raises(FerrumDangerApiError, match="danger_delete_all"):
        await Ephemeral.objects.delete(pg_conn)


@pytest.mark.integration
@pytest.mark.xfail(
    reason="danger_delete_all compiles to trailing WHERE (invalid SQL) — compiler fix pending",
    strict=False,
)
async def test_danger_delete_all_clears_table(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_crud_danger_{unique_suffix}"

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
        pool = raw_pool(pg_conn)
        await seed_int_rows(pool, table_name, 1, 2)

        deleted = await Row.objects.danger_delete_all(pg_conn)
        assert deleted == 2
        assert await Row.objects.count(pg_conn) == 0
