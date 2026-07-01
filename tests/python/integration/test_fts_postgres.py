"""Live PostgreSQL integration tests for full-text search."""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

from typing import Annotated

import pytest

import ferrum
from ferrum.models import Field

from .helpers import raw_pool, transient_table

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_fts_match_and_rank(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_fts_article_{unique_suffix}"

    class FtsArticle(ferrum.Model):
        id: int = 0
        title: str = ""
        search_vector: Annotated[ferrum.TSVector, Field(fts_config="english")] | None = None

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            search_vector TSVECTOR
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}" CASCADE'

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        pool = raw_pool(pg_conn)
        await pool.execute(
            f'INSERT INTO "{table_name}" (title, search_vector) VALUES '
            f"('Rust ORM guide', to_tsvector('english', 'Rust ORM guide')), "
            f"('Python web apps', to_tsvector('english', 'Python web framework'))"
        )
        await pool.execute(f'CREATE INDEX ON "{table_name}" USING gin (search_vector)')

        hits = await (
            FtsArticle.objects.search("rust orm", field="search_vector", mode="plain")
            .limit(5)
            .all(pg_conn)
        )
        assert len(hits) >= 1
        assert any("Rust" in (a.title or "") for a in hits)
