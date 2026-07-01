"""Unit tests for FTS migration ops (Wave 0 stubs)."""

from __future__ import annotations

from ferrum.migrations.operations import (
    CreateFullTextCatalog,
    CreateFullTextIndex,
    DropFullTextIndex,
)
from ferrum.migrations.orchestrator import _op_to_sql


def test_create_full_text_index_postgres_stub() -> None:
    op = CreateFullTextIndex(
        "articles",
        "fts_articles_body",
        ["body"],
        config="english",
    ).to_op_dict()
    sql = _op_to_sql(op, dialect="postgres")
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "USING gin" in sql
    assert "to_tsvector('english'" in sql


def test_create_full_text_index_mysql_stub() -> None:
    op = CreateFullTextIndex("articles", "fts_articles_body", ["body"]).to_op_dict()
    sql = _op_to_sql(op, dialect="mysql")
    assert "FULLTEXT" in sql


def test_drop_full_text_index_sqlite() -> None:
    op = DropFullTextIndex("articles", "articles_fts").to_op_dict()
    sql = _op_to_sql(op, dialect="sqlite")
    assert 'DROP TABLE IF EXISTS "articles_fts"' in sql


def test_create_full_text_catalog_mssql() -> None:
    op = CreateFullTextCatalog("main_catalog").to_op_dict()
    sql = _op_to_sql(op, dialect="mssql")
    assert "FULLTEXT CATALOG" in sql
