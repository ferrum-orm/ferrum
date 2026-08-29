"""Live MySQL FULLTEXT integration tests (requires FERRUM_TEST_MYSQL_DSN).

DDL is set up via ``ferrum.migrations.fts.mysql``.  Ferrum Models with
``FullTextIndex`` metadata are defined dynamically per test so the table name
matches the unique suffix.  All search assertions go through
``QuerySet.search()`` / ``rank_by()``, ensuring the IR compile-and-execute path
is exercised end-to-end against a live MySQL instance.

Supported modes:
  - ``plain``  → ``MATCH(cols) AGAINST (? IN NATURAL LANGUAGE MODE)``
  - ``boolean`` → ``MATCH(cols) AGAINST (? IN BOOLEAN MODE)``
  - ``phrase`` and ``websearch`` map to NATURAL LANGUAGE MODE in the MySQL
    emitter; they are not independently tested here to avoid false positives.
"""

# ruff: noqa: S608 — table identifiers are test-controlled uuid suffixes, not user input.

from __future__ import annotations

import contextlib
import os

import pytest

import ferrum
from ferrum.migrations.fts.mysql import create_full_text_index, drop_full_text_index
from ferrum.models import Field, FullTextIndex, Model
from ferrum.queryset import QuerySet

pytestmark = pytest.mark.integration


@pytest.fixture
def mysql_dsn() -> str:
    dsn = os.environ.get("FERRUM_TEST_MYSQL_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_MYSQL_DSN not set")
    pytest.importorskip("asyncmy", reason="asyncmy not available")
    return dsn


def _make_fts_model(table_name: str, fts_idx: str) -> type[Model]:
    """Build a Ferrum model whose FTS metadata points at the given table/index."""

    class FTSArticle(Model):
        class Meta:
            table = table_name
            full_text_indexes = (FullTextIndex(fields=("body",), name=fts_idx),)

        id: int = Field(primary_key=True)
        body: str

    return FTSArticle


@pytest.mark.asyncio
async def test_mysql_fts_plain_mode_search(
    mysql_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search(mode='plain') returns matching rows via MATCH … NATURAL LANGUAGE."""
    table = f"ferrum_fts_{unique_suffix}"
    idx_name = f"ferrum_fts_idx_{unique_suffix}"
    FTSModel = _make_fts_model(table, idx_name)

    async with ferrum.connect(mysql_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f"CREATE TABLE `{table}` ("
            f"  `id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
            f"  `body` TEXT NOT NULL"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        try:
            fts_ddl = create_full_text_index(
                {"table": table, "name": idx_name, "columns": ["body"]}
            )
            await driver.execute(fts_ddl)

            # InnoDB FTS requires words longer than ft_min_token_size (3).
            await driver.execute(
                f"INSERT INTO `{table}` (body) VALUES "
                f"('Ferrum async ORM library'), "
                f"('Python asyncio frameworks'), "
                f"('Rust performance crate')"
            )

            results = (
                await QuerySet(FTSModel).search("Ferrum", field="body", mode="plain").all(conn)
            )
            assert len(results) >= 1, f"Expected >=1 MATCH result for 'Ferrum', got {results!r}"
            assert any("Ferrum" in r.body for r in results), (
                f"'Ferrum' not found in result bodies: {[r.body for r in results]}"
            )
        finally:
            drop_ddl = drop_full_text_index({"table": table, "name": idx_name})
            with contextlib.suppress(Exception):
                await driver.execute(drop_ddl)
            await driver.execute(f"DROP TABLE IF EXISTS `{table}`")


@pytest.mark.asyncio
async def test_mysql_fts_boolean_mode_search(
    mysql_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search(mode='boolean') uses MATCH … BOOLEAN MODE."""
    table = f"ferrum_fts_bool_{unique_suffix}"
    idx_name = f"ferrum_fts_bool_idx_{unique_suffix}"
    FTSModel = _make_fts_model(table, idx_name)

    async with ferrum.connect(mysql_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f"CREATE TABLE `{table}` ("
            f"  `id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
            f"  `body` TEXT NOT NULL"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        try:
            fts_ddl = create_full_text_index(
                {"table": table, "name": idx_name, "columns": ["body"]}
            )
            await driver.execute(fts_ddl)

            await driver.execute(
                f"INSERT INTO `{table}` (body) VALUES "
                f"('async database library'), "
                f"('Ferrum performance ORM')"
            )

            # Boolean mode: +term requires the term to be present.
            results = (
                await QuerySet(FTSModel).search("+Ferrum", field="body", mode="boolean").all(conn)
            )
            assert len(results) >= 1, (
                f"Expected >=1 BOOLEAN MODE result for '+Ferrum', got {results!r}"
            )
            assert any("Ferrum" in r.body for r in results)
        finally:
            drop_ddl = drop_full_text_index({"table": table, "name": idx_name})
            with contextlib.suppress(Exception):
                await driver.execute(drop_ddl)
            await driver.execute(f"DROP TABLE IF EXISTS `{table}`")


@pytest.mark.asyncio
async def test_mysql_fts_no_match_returns_empty(
    mysql_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """QuerySet.search() with an absent term returns an empty list."""
    table = f"ferrum_fts_nm_{unique_suffix}"
    idx_name = f"ferrum_fts_nm_idx_{unique_suffix}"
    FTSModel = _make_fts_model(table, idx_name)

    async with ferrum.connect(mysql_dsn) as conn:
        driver = conn._require_driver()
        await driver.execute(
            f"CREATE TABLE `{table}` ("
            f"  `id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
            f"  `body` TEXT NOT NULL"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        try:
            fts_ddl = create_full_text_index(
                {"table": table, "name": idx_name, "columns": ["body"]}
            )
            await driver.execute(fts_ddl)

            await driver.execute(f"INSERT INTO `{table}` (body) VALUES ('async database library')")

            results = (
                await QuerySet(FTSModel)
                .search("xyzzyunmatchable", field="body", mode="plain")
                .all(conn)
            )
            assert results == [], f"Expected empty list for absent term, got {results!r}"
        finally:
            drop_ddl = drop_full_text_index({"table": table, "name": idx_name})
            with contextlib.suppress(Exception):
                await driver.execute(drop_ddl)
            await driver.execute(f"DROP TABLE IF EXISTS `{table}`")
