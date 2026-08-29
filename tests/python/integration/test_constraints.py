"""Integration tests for database constraint violations mapped to FerrumIntegrityError.

Verifies the ratified §5a "Safe error fields" contract on live PostgreSQL:
mapped exceptions carry structured ``sqlstate`` and ``category`` attributes.
"""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import ERROR_CATEGORIES, FerrumIntegrityError

from .backends import Backend
from .schema import Column, transient_table


@pytest.mark.integration
async def test_unique_violation_maps_to_integrity_error(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_unique_{unique_suffix}"

    class User(ferrum.Model):
        id: int = 0
        email: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("email", "text", null=False, extra="UNIQUE"),
        ],
    ) as conn:
        await User.objects.create(conn, email="a@example.com")

        with pytest.raises(FerrumIntegrityError) as exc_info:
            await User.objects.create(conn, email="a@example.com")

        err = exc_info.value
        assert "FERR-D201" in str(err)
        assert "://" not in str(err)
        assert "a@example.com" not in str(err)
        # §5a: structured sqlstate and category on live PostgreSQL.
        assert err.category in ERROR_CATEGORIES, (
            f"category={err.category!r} not in closed enum ERROR_CATEGORIES"
        )
        assert err.category in ("unique_violation", "integrity_error"), (
            f"expected unique_violation or integrity_error, got {err.category!r}"
        )
        # sqlstate should be set for PostgreSQL backends (23505 for unique violation).
        if backend.name == "postgres":
            assert err.sqlstate == "23505", (
                f"expected sqlstate=23505 for unique violation, got {err.sqlstate!r}"
            )


@pytest.mark.integration
async def test_not_null_violation_maps_to_integrity_error(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_notnull_{unique_suffix}"

    class Note(ferrum.Model):
        id: int = 0
        body: str | None = None

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("body", "text", null=False),
        ],
    ) as conn:
        with pytest.raises(FerrumIntegrityError) as exc_info:
            await Note.objects.create(conn, body=None)

        err = exc_info.value
        # §5a: structured category on live PostgreSQL.
        assert err.category in ERROR_CATEGORIES, (
            f"category={err.category!r} not in closed enum ERROR_CATEGORIES"
        )
        assert err.category in ("integrity_error", "not_null_violation"), (
            f"expected integrity_error, got {err.category!r}"
        )
