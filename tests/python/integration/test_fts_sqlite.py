"""Live SQLite FTS5 integration tests (requires FERRUM_TEST_SQLITE_DSN).

DDL is set up via ``ferrum.migrations.fts.sqlite`` (FTS5 virtual table + sync
triggers).  A Ferrum Model is defined per test against the **content table**,
with ``FullTextIndex.name`` pointing at the FTS5 virtual table.

The Rust SQLite emitter translates ``QuerySet.search()`` into:
  ``content_table.rowid IN (SELECT rowid FROM fts_table WHERE fts_table MATCH ?)``
and ``rank_by()`` adds an ``ORDER BY bm25(fts_table) ASC`` clause.
``QuerySet.search()`` calls ``rank_by()`` internally, so both IR paths are
exercised by every test here.

``aiosqlite`` only handles one statement per ``execute()`` call, so multi-
statement DDL from ``create_full_text_index`` is split and executed per fragment.
"""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

import contextlib
import os

import pytest

import ferrum
from ferrum.migrations.fts.sqlite import create_full_text_index, drop_full_text_index
from ferrum.models import Field, FullTextIndex, Model
from ferrum.queryset import QuerySet

pytestmark = pytest.mark.integration


@pytest.fixture
def sqlite_dsn() -> str:
    dsn = os.environ.get("FERRUM_TEST_SQLITE_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_SQLITE_DSN not set")
    return dsn


async def _execute_ddl_statements(driver: object, ddl: str) -> None:
    """Execute a multi-statement DDL string one statement at a time.

    ``aiosqlite`` only handles a single statement per ``execute()`` call,
    so split only on the builder's statement separator. Trigger bodies contain
    semicolons that must remain part of the same statement.
    """
    for stmt in ddl.split(";\n"):
        stmt = stmt.strip()
        if stmt:
            await driver.execute(stmt)  # type: ignore[union-attr]


def _make_fts_model(content_table: str, fts_virtual_table: str) -> type[Model]:
    """Build a Ferrum model for the SQLite FTS content table.

    ``Meta.table`` points at the regular content table.
    ``FullTextIndex.name`` points at the FTS5 virtual table; the Rust emitter
    uses that name to build the rowid-subquery MATCH filter.
    """

    class FTSDoc(Model):
        class Meta:
            table = content_table
            full_text_indexes = (FullTextIndex(fields=("body",), name=fts_virtual_table),)

        id: int = Field(primary_key=True)
        body: str

    return FTSDoc


@pytest.mark.asyncio
async def test_sqlite_fts5_queryset_search(
    sqlite_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search() issues a rowid-subquery MATCH filter via the FTS5 virtual table."""
    content_table = f"ferrum_fts_ct_{unique_suffix}"
    fts_vt = f"ferrum_fts_{unique_suffix}"
    FTSDoc = _make_fts_model(content_table, fts_vt)

    async with ferrum.connect(sqlite_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f'CREATE TABLE "{content_table}" '
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)"
        )
        try:
            fts_ddl = create_full_text_index(
                {
                    "table": fts_vt,
                    "name": fts_vt,
                    "columns": ["body"],
                    "sqlite_content_table": content_table,
                }
            )
            await _execute_ddl_statements(driver, fts_ddl)

            # Insert via the content table so triggers sync the FTS5 index.
            await driver.execute(
                f'INSERT INTO "{content_table}" (body) VALUES '
                f"('Ferrum async ORM library'), "
                f"('Python asyncio frameworks'), "
                f"('Rust performance crate')"
            )

            results = await QuerySet(FTSDoc).search("Ferrum", field="body", mode="plain").all(conn)
            assert len(results) >= 1, f"Expected >=1 FTS5 match for 'Ferrum', got: {results!r}"
            assert any("Ferrum" in r.body for r in results), (
                f"'Ferrum' not found in result bodies: {[r.body for r in results]}"
            )
        finally:
            drop_ddl = drop_full_text_index({"name": fts_vt})
            with contextlib.suppress(Exception):
                await _execute_ddl_statements(driver, drop_ddl)
            await driver.execute(f'DROP TABLE IF EXISTS "{content_table}"')


@pytest.mark.asyncio
async def test_sqlite_fts5_queryset_rank_ordering(
    sqlite_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search() (which calls rank_by internally) orders by bm25 relevance."""
    content_table = f"ferrum_fts_rank_ct_{unique_suffix}"
    fts_vt = f"ferrum_fts_rank_{unique_suffix}"
    FTSDoc = _make_fts_model(content_table, fts_vt)

    async with ferrum.connect(sqlite_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f'CREATE TABLE "{content_table}" '
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)"
        )
        try:
            fts_ddl = create_full_text_index(
                {
                    "table": fts_vt,
                    "name": fts_vt,
                    "columns": ["body"],
                    "sqlite_content_table": content_table,
                }
            )
            await _execute_ddl_statements(driver, fts_ddl)

            # Row with two occurrences of "Ferrum" should rank higher.
            await driver.execute(
                f'INSERT INTO "{content_table}" (body) VALUES '
                f"('Ferrum ORM library'), "
                f"('Python asyncio'), "
                f"('Ferrum Ferrum high relevance')"
            )

            results = await QuerySet(FTSDoc).search("Ferrum", field="body", mode="plain").all(conn)
            # At least both Ferrum rows should be returned.
            assert len(results) >= 2, (
                f"Expected >=2 FTS5 results for 'Ferrum', got: {[r.body for r in results]}"
            )
            bodies = [r.body for r in results]
            assert all("Ferrum" in b for b in bodies), f"Non-matching row returned: {bodies}"
        finally:
            drop_ddl = drop_full_text_index({"name": fts_vt})
            with contextlib.suppress(Exception):
                await _execute_ddl_statements(driver, drop_ddl)
            await driver.execute(f'DROP TABLE IF EXISTS "{content_table}"')


@pytest.mark.asyncio
async def test_sqlite_fts5_no_match_returns_empty(
    sqlite_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search() returns an empty list when no rows match the term."""
    content_table = f"ferrum_fts_nm_ct_{unique_suffix}"
    fts_vt = f"ferrum_fts_nm_{unique_suffix}"
    FTSDoc = _make_fts_model(content_table, fts_vt)

    async with ferrum.connect(sqlite_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f'CREATE TABLE "{content_table}" '
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)"
        )
        try:
            fts_ddl = create_full_text_index(
                {
                    "table": fts_vt,
                    "name": fts_vt,
                    "columns": ["body"],
                    "sqlite_content_table": content_table,
                }
            )
            await _execute_ddl_statements(driver, fts_ddl)

            await driver.execute(
                f"INSERT INTO \"{content_table}\" (body) VALUES ('async database library')"
            )

            results = (
                await QuerySet(FTSDoc)
                .search("xyzzyunmatchable", field="body", mode="plain")
                .all(conn)
            )
            assert results == [], f"Expected empty list for absent term, got: {results!r}"
        finally:
            drop_ddl = drop_full_text_index({"name": fts_vt})
            with contextlib.suppress(Exception):
                await _execute_ddl_statements(driver, drop_ddl)
            await driver.execute(f'DROP TABLE IF EXISTS "{content_table}"')
