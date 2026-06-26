"""Unit tests for the MSSQL (T-SQL) thin-parity backend.

Covers identifier quoting, type mapping, ``_op_to_sql`` DDL shape, the upsert
guard, and the transaction-not-supported guard. These are pure SQL-string /
mock tests; live SQL Server behaviour (requiring ``msodbcsql18``) lives behind
the ``integration`` marker and is not exercised here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.errors import FerrumConfigError, FerrumMigrationError
from ferrum.migrations.orchestrator import (
    _map_sql_type,
    _op_to_sql,
    _quote_ident,
)
from ferrum.queryset import QuerySet


class _UpsertModel(ferrum.Model):
    id: int = 0
    email: str = ""
    name: str = ""


# ---------------------------------------------------------------------------
# Identifier quoting
# ---------------------------------------------------------------------------


def test_quote_ident_uses_brackets() -> None:
    assert _quote_ident("users", "mssql") == "[users]"


def test_quote_ident_escapes_closing_bracket() -> None:
    assert _quote_ident("we]ird", "mssql") == "[we]]ird]"


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        ("SERIAL", "INT IDENTITY(1,1)"),
        ("BIGSERIAL", "BIGINT IDENTITY(1,1)"),
        ("BOOLEAN", "BIT"),
        ("UUID", "UNIQUEIDENTIFIER"),
        ("TIMESTAMPTZ", "DATETIMEOFFSET"),
        ("TEXT", "NVARCHAR(MAX)"),
        ("JSONB", "NVARCHAR(MAX)"),
        ("BYTEA", "VARBINARY(MAX)"),
        ("VARCHAR(120)", "NVARCHAR(120)"),
        ("NUMERIC(10,2)", "NUMERIC(10,2)"),
    ],
)
def test_map_sql_type_mssql(canonical: str, expected: str) -> None:
    assert _map_sql_type(canonical, "mssql") == expected


def test_map_sql_type_mssql_rejects_unknown() -> None:
    with pytest.raises(FerrumMigrationError):
        _map_sql_type("CIDR", "mssql")


# ---------------------------------------------------------------------------
# DDL generation (_op_to_sql)
# ---------------------------------------------------------------------------


def test_create_table_guards_on_object_id() -> None:
    op = {
        "kind": "create_table",
        "table": "widget",
        "columns": [
            {"name": "id", "sql_type": "SERIAL", "primary_key": True, "nullable": False},
            {"name": "label", "sql_type": "TEXT", "nullable": True},
        ],
    }
    sql = _op_to_sql(op, dialect="mssql")
    assert sql.startswith("IF OBJECT_ID(N'widget', N'U') IS NULL CREATE TABLE [widget] (")
    assert "[id] INT IDENTITY(1,1)" in sql
    assert "[label] NVARCHAR(MAX)" in sql
    assert "IF NOT EXISTS" not in sql


def test_add_column_omits_column_keyword() -> None:
    op = {"kind": "add_column", "table": "widget", "name": "qty", "sql_type": "INT"}
    sql = _op_to_sql(op, dialect="mssql")
    assert sql == "ALTER TABLE [widget] ADD [qty] INT"
    assert "ADD COLUMN" not in sql


def test_add_index_omits_if_not_exists_and_using() -> None:
    op = {
        "kind": "add_index",
        "name": "ix_widget_label",
        "table": "widget",
        "columns": ["label"],
        "unique": True,
    }
    sql = _op_to_sql(op, dialect="mssql")
    assert sql == "CREATE UNIQUE INDEX [ix_widget_label] ON [widget] ([label])"
    assert "IF NOT EXISTS" not in sql
    assert "USING" not in sql


def test_drop_index_uses_on_table() -> None:
    op = {"kind": "drop_index", "name": "ix_widget_label", "table": "widget"}
    sql = _op_to_sql(op, dialect="mssql")
    assert sql == "DROP INDEX [ix_widget_label] ON [widget]"


@pytest.mark.parametrize(
    "kind",
    [
        "alter_column",
        "rename_column",
        "enable_rls",
        "create_policy",
        "create_extension",
        "create_function",
    ],
)
def test_unsupported_ops_raise(kind: str) -> None:
    op = {"kind": kind, "table": "widget", "name": "x", "column": "y"}
    with pytest.raises(FerrumMigrationError):
        _op_to_sql(op, dialect="mssql")


# ---------------------------------------------------------------------------
# Upsert guard
# ---------------------------------------------------------------------------


def test_upsert_sql_raises_on_mssql() -> None:
    meta = _UpsertModel.get_metadata()
    qs: QuerySet[_UpsertModel] = QuerySet(_UpsertModel)
    with pytest.raises(FerrumConfigError):
        qs._build_upsert_sql(
            meta,
            {"id": 1, "email": "a@b.com"},
            conflict_fields=["id"],
            update_fields=None,
            returning=False,
            dialect="mssql",
        )


def test_upsert_sql_still_works_on_postgres() -> None:
    meta = _UpsertModel.get_metadata()
    qs: QuerySet[_UpsertModel] = QuerySet(_UpsertModel)
    sql, bound = qs._build_upsert_sql(
        meta,
        {"id": 1, "email": "a@b.com"},
        conflict_fields=["id"],
        update_fields=["email"],
        returning=False,
        dialect="postgres",
    )
    assert "ON CONFLICT" in sql
    assert bound == [1, "a@b.com"]


# ---------------------------------------------------------------------------
# Transaction guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_raises_for_mssql_driver() -> None:
    conn = Connection("mssql://sa:pw@localhost/db")
    driver = MagicMock(spec=["dialect", "fetch", "execute"])
    driver.dialect = "mssql"
    conn._driver = driver  # type: ignore[assignment]
    with pytest.raises(FerrumConfigError):
        async with conn.transaction():
            pass


def test_connection_dialect_from_scheme() -> None:
    assert Connection("mssql://sa:pw@localhost/db").dialect == "mssql"
    assert Connection("sqlserver://sa:pw@localhost/db").dialect == "mssql"
