"""Integration tests for QuerySet CRUD terminals against live backends.

Invariants:
- create() inserts via compiled INSERT … RETURNING and hydrates a model instance.
- update()/delete() require filters; scoped mutations return affected row counts.
- danger_delete_all() is the explicit unscoped delete escape hatch.
"""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import FerrumDangerApiError

from .backends import Backend
from .schema import Column, transient_table


def _bool_default(backend: Backend) -> str:
    return "FALSE" if backend.name == "postgres" else "0"


@pytest.mark.integration
async def test_create_returns_hydrated_instance(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
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

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("title", "text", null=False),
            Column("published", "bool", null=False, default=_bool_default(backend)),
        ],
    ) as conn:
        row = await Article.objects.create(conn, title="hello", published=True)

        assert isinstance(row, Article)
        assert row.title == "hello"
        assert row.published is True
        assert row.id > 0


@pytest.mark.integration
async def test_update_and_delete_scoped_mutations(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
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

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("label", "text", null=False),
            Column(
                "active",
                "bool",
                null=False,
                default="TRUE" if backend.name == "postgres" else "1",
            ),
        ],
    ) as conn:
        await Tag.objects.create(conn, label="keep", active=True)
        await Tag.objects.create(conn, label="drop-me", active=False)

        updated = await Tag.objects.filter(active=False).update(conn, label="archived")
        assert updated == 1

        deleted = await Tag.objects.filter(label="archived").delete(conn)
        assert deleted == 1

        remaining = await Tag.objects.count(conn)
        assert remaining == 1


@pytest.mark.integration
async def test_unscoped_delete_requires_danger_api(
    db_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    class Ephemeral(ferrum.Model):
        id: int = 0

    with pytest.raises(FerrumDangerApiError, match="danger_delete_all"):
        await Ephemeral.objects.delete(db_conn)


@pytest.mark.integration
@pytest.mark.xfail(
    reason="danger_delete_all compiles to trailing WHERE (invalid SQL) — compiler fix pending",
    strict=False,
)
async def test_danger_delete_all_clears_table(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_crud_danger_{unique_suffix}"

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
        await Row.objects.create(conn, val=1)
        await Row.objects.create(conn, val=2)

        deleted = await Row.objects.danger_delete_all(conn)
        assert deleted == 2
        assert await Row.objects.count(conn) == 0


@pytest.mark.integration
async def test_create_from_instance_round_trip(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_crud_inst_{unique_suffix}"

    class Note(ferrum.Model):
        id: int = 0
        body: str = ""
        pinned: bool = False

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("body", "text", null=False),
            Column("pinned", "bool", null=False, default=_bool_default(backend)),
        ],
    ) as conn:
        note = Note(body="hello", pinned=True)
        created = await Note.objects.create(conn, note)

        assert isinstance(created, Note)
        assert created is not note
        assert note.id == 0, "the passed instance must not be mutated"
        assert created.id > 0, "sentinel PK dropped so SERIAL default runs"
        assert created.body == "hello"
        assert created.pinned is True


@pytest.mark.integration
async def test_update_instance_persists_and_detects_stale(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_crud_updinst_{unique_suffix}"

    class Task(ferrum.Model):
        id: int = 0
        title: str = ""
        done: bool = False

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("title", "text", null=False),
            Column("done", "bool", null=False, default=_bool_default(backend)),
        ],
    ) as conn:
        task = await Task.objects.create(conn, Task(title="todo"))

        edited = task.model_copy(update={"done": True})
        count = await Task.objects.update_instance(conn, edited, fields=["done"])
        assert count == 1

        reloaded = await Task.objects.filter(id=task.id).first(conn)
        assert reloaded is not None
        assert reloaded.done is True
        assert reloaded.title == "todo", "fields=[...] must not touch other columns"

        await Task.objects.filter(id=task.id).delete(conn)
        stale = await Task.objects.update_instance(conn, edited, fields=["title"])
        assert stale == 0, "0 rows signals a missing/stale instance"
