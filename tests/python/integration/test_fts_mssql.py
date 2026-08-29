"""Live MSSQL full-text search integration tests (requires FERRUM_TEST_MSSQL_DSN).

DDL is set up via ``ferrum.migrations.fts.mssql``.  A Ferrum Model is defined
dynamically per test with ``FullTextIndex`` metadata so that all search
assertions go through ``QuerySet.search()`` / ``rank_by()``.

The Rust MSSQL emitter translates search modes as follows:
  - ``plain`` / ``websearch``  →  ``FREETEXT(col, ?)``
  - ``phrase`` / ``boolean``   →  ``CONTAINS(col, ?)``

``rank_by()`` adds an ``ORDER BY`` with a correlated ``FREETEXTTABLE`` /
``CONTAINSTABLE`` subquery.  ``QuerySet.search()`` calls ``rank_by()``
internally, so both IR paths are exercised.

MSSQL full-text population is **asynchronous** — the catalog crawl runs after
``CREATE FULLTEXT INDEX``.  We poll ``FULLTEXTCATALOGPROPERTY(...,
'PopulateStatus')`` and **fail** (not skip) if the catalog does not reach idle
state within the configured timeout.  A required, configured backend must never
silently pass without evidence that the search path was actually exercised.
"""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

import ferrum
from ferrum.migrations.fts.mssql import (
    create_full_text_catalog,
    create_full_text_index,
    drop_full_text_index,
)
from ferrum.models import Field, FullTextIndex, Model
from ferrum.queryset import QuerySet

pytestmark = pytest.mark.integration

_POLL_INTERVAL_S: float = 1.0
_CATALOG_IDLE_TIMEOUT_S: float = 60.0


@pytest.fixture
def mssql_dsn() -> str:
    dsn = os.environ.get("FERRUM_TEST_MSSQL_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_MSSQL_DSN not set")
    pytest.importorskip("aioodbc", reason="aioodbc/pyodbc not available")
    return dsn


async def _wait_for_catalog_idle(driver: object, catalog_name: str) -> None:
    """Poll until the full-text catalog finishes populating.

    Calls ``pytest.fail`` — not ``pytest.skip`` — on timeout.  A configured
    required backend that cannot index within ``_CATALOG_IDLE_TIMEOUT_S`` is a
    hard failure; silently going green is not acceptable.
    """
    elapsed = 0.0
    while elapsed < _CATALOG_IDLE_TIMEOUT_S:
        rows = await driver.fetch(  # type: ignore[union-attr]
            f"SELECT FULLTEXTCATALOGPROPERTY('{catalog_name}', 'PopulateStatus') AS ps"
        )
        status = rows[0]["ps"] if rows else None
        if status == 0:
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
        elapsed += _POLL_INTERVAL_S
    pytest.fail(
        f"MSSQL FTS catalog '{catalog_name}' did not become idle within "
        f"{_CATALOG_IDLE_TIMEOUT_S}s — population timed out.  "
        f"A required backend must not silently pass without exercising the search path."
    )


def _make_fts_model(table_name: str, fts_idx_name: str) -> type[Model]:
    """Build a Ferrum model for the MSSQL FTS table.

    For MSSQL, ``FullTextIndex.name`` is a logical index identifier; the Rust
    emitter does not use the virtual-table name (as SQLite does) — it emits
    ``FREETEXT``/``CONTAINS`` directly against the column.  The name must still
    be provided so the IR ``full_text_indexes`` payload is populated and
    ``fts_index_for_field`` can resolve the index for ``rank_by()``.
    """

    class FTSArticle(Model):
        class Meta:
            table = table_name
            full_text_indexes = (FullTextIndex(fields=("title",), name=fts_idx_name),)

        id: int = Field(primary_key=True)
        title: str

    return FTSArticle


@pytest.mark.asyncio
async def test_mssql_fts_plain_mode_search(
    mssql_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search(mode='plain') emits FREETEXT and returns matching rows."""
    table = f"ferrum_fts_{unique_suffix}"
    catalog_name = f"ferrum_cat_{unique_suffix}"
    pk_constraint = f"ferrum_pk_{unique_suffix}"
    fts_idx_name = f"ferrum_fts_idx_{unique_suffix}"
    FTSModel = _make_fts_model(table, fts_idx_name)

    async with ferrum.connect(mssql_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f"CREATE TABLE [{table}] ("
            f"  [id] INT NOT NULL,"
            f"  [title] NVARCHAR(MAX) NOT NULL,"
            f"  CONSTRAINT [{pk_constraint}] PRIMARY KEY ([id])"
            f")"
        )
        try:
            catalog_ddl = create_full_text_catalog({"name": catalog_name})
            await driver.execute(catalog_ddl)

            try:
                fts_ddl = create_full_text_index(
                    {
                        "table": table,
                        "columns": ["title"],
                        "catalog": catalog_name,
                        "pk_column": pk_constraint,
                    }
                )
                await driver.execute(fts_ddl)

                await driver.execute(
                    f"INSERT INTO [{table}] VALUES "
                    f"(1, N'Ferrum async ORM library'), "
                    f"(2, N'Python asyncio framework'), "
                    f"(3, N'Rust performance crate')"
                )

                await _wait_for_catalog_idle(driver, catalog_name)

                results = (
                    await QuerySet(FTSModel).search("Ferrum", field="title", mode="plain").all(conn)
                )
                assert len(results) >= 1, (
                    f"Expected >=1 FREETEXT match for 'Ferrum', got: {results!r}"
                )
                assert any("Ferrum" in r.title for r in results), (
                    f"'Ferrum' not found in titles: {[r.title for r in results]}"
                )
            finally:
                drop_ddl = drop_full_text_index({"table": table})
                with contextlib.suppress(Exception):
                    await driver.execute(drop_ddl)
                with contextlib.suppress(Exception):
                    await driver.execute(f"DROP FULLTEXT CATALOG [{catalog_name}]")
        finally:
            await driver.execute(f"IF OBJECT_ID(N'[{table}]') IS NOT NULL DROP TABLE [{table}]")


@pytest.mark.asyncio
async def test_mssql_fts_phrase_mode_search(
    mssql_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search(mode='phrase') emits CONTAINS and returns matching rows."""
    table = f"ferrum_fts_ph_{unique_suffix}"
    catalog_name = f"ferrum_cat_ph_{unique_suffix}"
    pk_constraint = f"ferrum_pk_ph_{unique_suffix}"
    fts_idx_name = f"ferrum_fts_ph_idx_{unique_suffix}"
    FTSModel = _make_fts_model(table, fts_idx_name)

    async with ferrum.connect(mssql_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f"CREATE TABLE [{table}] ("
            f"  [id] INT NOT NULL,"
            f"  [title] NVARCHAR(MAX) NOT NULL,"
            f"  CONSTRAINT [{pk_constraint}] PRIMARY KEY ([id])"
            f")"
        )
        try:
            catalog_ddl = create_full_text_catalog({"name": catalog_name})
            await driver.execute(catalog_ddl)

            try:
                fts_ddl = create_full_text_index(
                    {
                        "table": table,
                        "columns": ["title"],
                        "catalog": catalog_name,
                        "pk_column": pk_constraint,
                    }
                )
                await driver.execute(fts_ddl)

                await driver.execute(
                    f"INSERT INTO [{table}] VALUES "
                    f"(1, N'Ferrum async ORM library'), "
                    f"(2, N'Python asyncio framework')"
                )

                await _wait_for_catalog_idle(driver, catalog_name)

                # phrase mode → CONTAINS(col, ?) with FORMSOF(THESAURUS, ...) or exact phrase.
                results = (
                    await QuerySet(FTSModel)
                    .search('"Ferrum async"', field="title", mode="phrase")
                    .all(conn)
                )
                assert len(results) >= 1, (
                    f"Expected >=1 CONTAINS match for phrase 'Ferrum async', got: {results!r}"
                )
                assert any("Ferrum" in r.title for r in results), (
                    f"Expected 'Ferrum' in titles: {[r.title for r in results]}"
                )
            finally:
                drop_ddl = drop_full_text_index({"table": table})
                with contextlib.suppress(Exception):
                    await driver.execute(drop_ddl)
                with contextlib.suppress(Exception):
                    await driver.execute(f"DROP FULLTEXT CATALOG [{catalog_name}]")
        finally:
            await driver.execute(f"IF OBJECT_ID(N'[{table}]') IS NOT NULL DROP TABLE [{table}]")


@pytest.mark.asyncio
async def test_mssql_fts_no_match_returns_empty(
    mssql_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search() returns an empty list when no rows match the term."""
    table = f"ferrum_fts_nm_{unique_suffix}"
    catalog_name = f"ferrum_cat_nm_{unique_suffix}"
    pk_constraint = f"ferrum_pk_nm_{unique_suffix}"
    fts_idx_name = f"ferrum_fts_nm_idx_{unique_suffix}"
    FTSModel = _make_fts_model(table, fts_idx_name)

    async with ferrum.connect(mssql_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f"CREATE TABLE [{table}] ("
            f"  [id] INT NOT NULL,"
            f"  [title] NVARCHAR(MAX) NOT NULL,"
            f"  CONSTRAINT [{pk_constraint}] PRIMARY KEY ([id])"
            f")"
        )
        try:
            catalog_ddl = create_full_text_catalog({"name": catalog_name})
            await driver.execute(catalog_ddl)

            try:
                fts_ddl = create_full_text_index(
                    {
                        "table": table,
                        "columns": ["title"],
                        "catalog": catalog_name,
                        "pk_column": pk_constraint,
                    }
                )
                await driver.execute(fts_ddl)
                await driver.execute(f"INSERT INTO [{table}] VALUES (1, N'async database library')")

                await _wait_for_catalog_idle(driver, catalog_name)

                results = (
                    await QuerySet(FTSModel)
                    .search("xyzzyunmatchable", field="title", mode="plain")
                    .all(conn)
                )
                assert results == [], f"Expected empty list for absent term, got: {results!r}"
            finally:
                drop_ddl = drop_full_text_index({"table": table})
                with contextlib.suppress(Exception):
                    await driver.execute(drop_ddl)
                with contextlib.suppress(Exception):
                    await driver.execute(f"DROP FULLTEXT CATALOG [{catalog_name}]")
        finally:
            await driver.execute(f"IF OBJECT_ID(N'[{table}]') IS NOT NULL DROP TABLE [{table}]")
