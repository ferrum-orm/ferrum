"""Unit tests for inspectdb scaffolding."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import ferrum.cli.inspectdb_cmd as inspectdb_cmd


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeDbConn:
    def __init__(
        self,
        *,
        column_rows: list[dict],
        fk_rows: list[dict] | None = None,
        pk_rows: list[dict] | None = None,
    ) -> None:
        self._results = [
            column_rows,
            fk_rows or [],
            pk_rows or [],
        ]
        self.fetch_calls: list[tuple[str, str]] = []

    async def fetch(self, query: str, schema: str) -> list[dict]:
        self.fetch_calls.append((query, schema))
        return self._results.pop(0)


class _FakePool:
    def __init__(self, db_conn: _FakeDbConn) -> None:
        self.db_conn = db_conn

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.db_conn)


class _FakeConn:
    def __init__(self, db_conn: _FakeDbConn) -> None:
        self.db_conn = db_conn

    def _require_pool(self) -> _FakePool:
        return _FakePool(self.db_conn)


class _FakeConnect:
    def __init__(self, db_conn: _FakeDbConn) -> None:
        self.db_conn = db_conn

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self.db_conn)

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _column(
    table_name: str,
    column_name: str,
    data_type: str,
    *,
    nullable: bool = False,
    max_len: int | None = None,
    precision: int | None = None,
    scale: int | None = None,
    ordinal_position: int = 1,
) -> dict:
    return {
        "table_name": table_name,
        "column_name": column_name,
        "data_type": data_type,
        "character_maximum_length": max_len,
        "numeric_precision": precision,
        "numeric_scale": scale,
        "is_nullable": "YES" if nullable else "NO",
        "column_default": None,
        "ordinal_position": ordinal_position,
    }


def test_pg_type_to_ferrum_maps_parameterized_and_nullable_types() -> None:
    assert (
        inspectdb_cmd._pg_type_to_ferrum("character varying", 100, None, None, False, False)
        == "Annotated[str, Field(max_length=100)]"
    )
    assert (
        inspectdb_cmd._pg_type_to_ferrum("numeric", None, 12, 2, False, False)
        == "Annotated[Decimal, Field(max_digits=12, decimal_places=2)]"
    )
    assert inspectdb_cmd._pg_type_to_ferrum("uuid", None, None, None, False, False) == "UUID"
    assert inspectdb_cmd._pg_type_to_ferrum("text", None, None, None, False, True) == ("str | None")


def test_render_class_uses_foreign_key_instead_of_raw_id_field() -> None:
    source = inspectdb_cmd._render_class(
        "blog_post",
        [
            _column("blog_post", "id", "bigserial", ordinal_position=1),
            _column("blog_post", "author_id", "integer", ordinal_position=2),
            _column("blog_post", "title", "character varying", max_len=200, ordinal_position=3),
        ],
        {"id"},
        {
            "author_id": {
                "foreign_table_name": "user_account",
                "foreign_column_name": "id",
                "delete_rule": "CASCADE",
            }
        },
    )

    assert 'author: ForeignKey = ForeignKey(to="UserAccount", on_delete="CASCADE")' in source
    assert "author_id:" not in source
    assert "title: Annotated[str, Field(max_length=200)]" in source


@pytest.mark.asyncio
async def test_run_inspectdb_writes_stdout_when_no_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_conn = _FakeDbConn(
        column_rows=[_column("notes", "id", "bigserial")],
        pk_rows=[{"table_name": "notes", "column_name": "id"}],
    )
    monkeypatch.setattr(inspectdb_cmd, "connect", lambda: _FakeConnect(db_conn))

    result = await inspectdb_cmd.run_inspectdb(output=None, schema="public")

    assert result == 0
    captured = capsys.readouterr()
    assert "class Notes(Model):" in captured.out
    assert "id: int = Field(primary_key=True)" in captured.out


@pytest.mark.asyncio
async def test_run_inspectdb_writes_to_output_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "models.py"
    db_conn = _FakeDbConn(
        column_rows=[_column("notes", "id", "bigserial")],
        pk_rows=[{"table_name": "notes", "column_name": "id"}],
    )
    monkeypatch.setattr(inspectdb_cmd, "connect", lambda: _FakeConnect(db_conn))

    result = await inspectdb_cmd.run_inspectdb(output=output, schema="public")

    assert result == 0
    assert "class Notes(Model):" in output.read_text(encoding="utf-8")
    assert str(output) in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_inspectdb_queries_are_parameterized_with_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_conn = _FakeDbConn(column_rows=[])
    connect_mock = AsyncMock(return_value=_FakeConnect(db_conn))
    monkeypatch.setattr(inspectdb_cmd, "connect", lambda: _FakeConnect(db_conn))

    result = await inspectdb_cmd.run_inspectdb(output=None, schema="tenant_schema")

    assert result == 0
    assert len(db_conn.fetch_calls) == 3
    assert all(schema == "tenant_schema" for _, schema in db_conn.fetch_calls)
    assert all("$1" in query for query, _ in db_conn.fetch_calls)
    assert all("tenant_schema" not in query for query, _ in db_conn.fetch_calls)
    connect_mock.assert_not_called()
