"""Integration tests for database constraint violations mapped to FerrumIntegrityError."""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import FerrumIntegrityError

from .helpers import transient_table


@pytest.mark.integration
async def test_unique_violation_maps_to_integrity_error(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_unique_{unique_suffix}"

    class User(ferrum.Model):
        id: int = 0
        email: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        await User.objects.create(pg_conn, email="a@example.com")

        with pytest.raises(FerrumIntegrityError) as exc_info:
            await User.objects.create(pg_conn, email="a@example.com")

        assert "FERR-D201" in str(exc_info.value)
        assert "postgresql://" not in str(exc_info.value)
        assert "a@example.com" not in str(exc_info.value)


@pytest.mark.integration
async def test_not_null_violation_maps_to_integrity_error(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_notnull_{unique_suffix}"

    class Note(ferrum.Model):
        id: int = 0
        body: str | None = None

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            body TEXT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        with pytest.raises(FerrumIntegrityError):
            await Note.objects.create(pg_conn, body=None)
