"""Unit tests for migration operation classes.

Invariants covered:
- Every Operation subclass produces the correct to_op_dict() shape.
- DropTable and DropColumn carry classification == "destructive".
- Column descriptor defaults are applied correctly.
- Round-trip: CreateTable(...).to_op_dict() feeds _op_to_sql and produces
  valid SQL containing the table name and column names.
"""

from __future__ import annotations

from ferrum.migrations.operations import (
    AddColumn,
    AddIndex,
    Column,
    CreateTable,
    DropColumn,
    DropIndex,
    DropTable,
    RawSQL,
    RenameColumn,
)
from ferrum.migrations.orchestrator import _op_to_sql

# ---------------------------------------------------------------------------
# Column descriptor — defaults
# ---------------------------------------------------------------------------


class TestColumn:
    def test_defaults(self) -> None:
        col = Column("email", "TEXT")
        assert col.not_null is False
        assert col.default is None
        assert col.primary_key is False

    def test_to_col_dict_default_values(self) -> None:
        col = Column("body", "TEXT")
        d = col.to_col_dict()
        assert d == {
            "name": "body",
            "sql_type": "TEXT",
            "not_null": False,
            "default": None,
            "primary_key": False,
        }

    def test_to_col_dict_with_flags(self) -> None:
        col = Column("id", "BIGSERIAL", not_null=True, primary_key=True)
        d = col.to_col_dict()
        assert d["not_null"] is True
        assert d["primary_key"] is True

    def test_to_col_dict_with_default(self) -> None:
        col = Column("active", "BOOLEAN", not_null=True, default="TRUE")
        d = col.to_col_dict()
        assert d["default"] == "TRUE"


# ---------------------------------------------------------------------------
# CreateTable
# ---------------------------------------------------------------------------


class TestCreateTable:
    def test_to_op_dict_keys(self) -> None:
        op = CreateTable(
            "user",
            [Column("id", "BIGSERIAL", primary_key=True, not_null=True)],
        )
        d = op.to_op_dict()
        assert d["kind"] == "create_table"
        assert d["table"] == "user"
        assert isinstance(d["columns"], list)
        assert len(d["columns"]) == 1

    def test_to_op_dict_columns_list_shape(self) -> None:
        op = CreateTable(
            "note",
            [
                Column("id", "INTEGER", primary_key=True, not_null=True),
                Column("body", "TEXT", not_null=True),
            ],
        )
        d = op.to_op_dict()
        col_names = [c["name"] for c in d["columns"]]
        assert col_names == ["id", "body"]

    def test_classification_is_safe(self) -> None:
        op = CreateTable("t", [])
        assert op.classification == "safe"

    def test_to_op_dict_empty_columns(self) -> None:
        op = CreateTable("empty", [])
        d = op.to_op_dict()
        assert d["columns"] == []


# ---------------------------------------------------------------------------
# AddColumn — flattened top-level shape
# ---------------------------------------------------------------------------


class TestAddColumn:
    def test_to_op_dict_flattened_keys(self) -> None:
        op = AddColumn("users", Column("score", "FLOAT", not_null=True))
        d = op.to_op_dict()
        assert d["kind"] == "add_column"
        assert d["table"] == "users"
        assert d["name"] == "score"
        assert d["sql_type"] == "FLOAT"
        assert d["not_null"] is True
        assert d["default"] is None
        assert d["primary_key"] is False

    def test_to_op_dict_has_no_nested_columns_key(self) -> None:
        op = AddColumn("t", Column("x", "INT"))
        assert "columns" not in op.to_op_dict()

    def test_classification_is_safe(self) -> None:
        op = AddColumn("t", Column("x", "INT"))
        assert op.classification == "safe"


# ---------------------------------------------------------------------------
# DropTable — destructive
# ---------------------------------------------------------------------------


class TestDropTable:
    def test_to_op_dict_keys(self) -> None:
        op = DropTable("old_users")
        d = op.to_op_dict()
        assert d == {"kind": "drop_table", "table": "old_users"}

    def test_classification_is_destructive(self) -> None:
        op = DropTable("t")
        assert op.classification == "destructive"


# ---------------------------------------------------------------------------
# DropColumn — destructive
# ---------------------------------------------------------------------------


class TestDropColumn:
    def test_to_op_dict_keys(self) -> None:
        op = DropColumn("users", "legacy_field")
        d = op.to_op_dict()
        assert d == {"kind": "drop_column", "table": "users", "column": "legacy_field"}

    def test_classification_is_destructive(self) -> None:
        op = DropColumn("t", "col")
        assert op.classification == "destructive"


# ---------------------------------------------------------------------------
# RenameColumn
# ---------------------------------------------------------------------------


class TestRenameColumn:
    def test_to_op_dict_keys(self) -> None:
        op = RenameColumn("users", "fname", "first_name")
        d = op.to_op_dict()
        assert d["kind"] == "rename_column"
        assert d["table"] == "users"
        assert d["from"] == "fname"
        assert d["to"] == "first_name"

    def test_classification_is_safe(self) -> None:
        op = RenameColumn("t", "a", "b")
        assert op.classification == "safe"


# ---------------------------------------------------------------------------
# AddIndex
# ---------------------------------------------------------------------------


class TestAddIndex:
    def test_to_op_dict_non_unique(self) -> None:
        op = AddIndex("orders", "idx_orders_user_id", ["user_id"])
        d = op.to_op_dict()
        assert d["kind"] == "add_index"
        assert d["table"] == "orders"
        assert d["name"] == "idx_orders_user_id"
        assert d["columns"] == ["user_id"]
        assert d["unique"] is False

    def test_to_op_dict_unique_flag(self) -> None:
        op = AddIndex("users", "idx_users_email", ["email"], unique=True)
        d = op.to_op_dict()
        assert d["unique"] is True

    def test_to_op_dict_multi_column(self) -> None:
        op = AddIndex("events", "idx_events_compound", ["user_id", "created_at"])
        d = op.to_op_dict()
        assert d["columns"] == ["user_id", "created_at"]

    def test_columns_list_is_a_copy(self) -> None:
        cols = ["a", "b"]
        op = AddIndex("t", "idx", cols)
        cols.append("c")
        assert op.to_op_dict()["columns"] == ["a", "b"]

    def test_classification_is_safe(self) -> None:
        op = AddIndex("t", "idx", ["col"])
        assert op.classification == "safe"


# ---------------------------------------------------------------------------
# DropIndex
# ---------------------------------------------------------------------------


class TestDropIndex:
    def test_to_op_dict_keys(self) -> None:
        op = DropIndex("idx_old")
        d = op.to_op_dict()
        assert d == {"kind": "drop_index", "name": "idx_old"}

    def test_classification_is_safe(self) -> None:
        op = DropIndex("idx_old")
        assert op.classification == "safe"


# ---------------------------------------------------------------------------
# RawSQL
# ---------------------------------------------------------------------------


class TestRawSQL:
    def test_to_op_dict_unsafe_by_default(self) -> None:
        op = RawSQL("SELECT 1")
        d = op.to_op_dict()
        assert d["kind"] == "raw_sql"
        assert d["sql"] == "SELECT 1"
        assert d["safe"] is False

    def test_to_op_dict_safe_flag(self) -> None:
        op = RawSQL("CREATE EXTENSION IF NOT EXISTS pgcrypto", safe=True)
        d = op.to_op_dict()
        assert d["safe"] is True

    def test_classification_is_safe(self) -> None:
        op = RawSQL("SELECT 1", safe=True)
        assert op.classification == "safe"


# ---------------------------------------------------------------------------
# Integration: CreateTable → to_op_dict → _op_to_sql
# ---------------------------------------------------------------------------


class TestCreateTableIntegration:
    def test_op_to_sql_contains_table_and_column_names(self) -> None:
        op = CreateTable(
            "article",
            [
                Column("id", "BIGSERIAL", primary_key=True, not_null=True),
                Column("title", "TEXT", not_null=True),
            ],
        )
        sql = _op_to_sql(op.to_op_dict())
        assert "article" in sql
        assert '"article"' in sql
        assert '"id"' in sql
        assert '"title"' in sql
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert "PRIMARY KEY" in sql
        assert "NOT NULL" in sql

    def test_op_to_sql_round_trip_add_column(self) -> None:
        op = AddColumn("users", Column("age", "INT", not_null=True))
        sql = _op_to_sql(op.to_op_dict())
        assert '"users"' in sql
        assert '"age"' in sql
        assert "ALTER TABLE" in sql
        assert "NOT NULL" in sql

    def test_op_to_sql_round_trip_drop_table(self) -> None:
        op = DropTable("old_log")
        sql = _op_to_sql(op.to_op_dict())
        assert '"old_log"' in sql
        assert "DROP TABLE IF EXISTS" in sql

    def test_op_to_sql_round_trip_add_index_unique(self) -> None:
        op = AddIndex("users", "idx_users_email", ["email"], unique=True)
        sql = _op_to_sql(op.to_op_dict())
        assert "UNIQUE" in sql
        assert '"idx_users_email"' in sql

    def test_op_to_sql_round_trip_rename_column(self) -> None:
        op = RenameColumn("users", "fname", "first_name")
        sql = _op_to_sql(op.to_op_dict())
        assert '"fname"' in sql
        assert '"first_name"' in sql
        assert "RENAME COLUMN" in sql

    def test_op_to_sql_round_trip_drop_column(self) -> None:
        op = DropColumn("events", "legacy")
        sql = _op_to_sql(op.to_op_dict())
        assert '"events"' in sql
        assert '"legacy"' in sql
        assert "DROP COLUMN IF EXISTS" in sql

    def test_op_to_sql_round_trip_drop_index(self) -> None:
        op = DropIndex("idx_old")
        sql = _op_to_sql(op.to_op_dict())
        assert '"idx_old"' in sql
        assert "DROP INDEX IF EXISTS" in sql

    def test_op_to_sql_round_trip_raw_sql_passthrough(self) -> None:
        stmt = "CREATE EXTENSION IF NOT EXISTS pgcrypto"
        op = RawSQL(stmt, safe=True)
        assert _op_to_sql(op.to_op_dict()) == stmt
