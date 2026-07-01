"""Gated SQLite FTS5 integration harness (requires FERRUM_TEST_SQLITE_PATH)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def sqlite_path() -> str:
    path = os.environ.get("FERRUM_TEST_SQLITE_PATH")
    if not path:
        pytest.skip("FERRUM_TEST_SQLITE_PATH not set")
    return path


@pytest.mark.asyncio
async def test_sqlite_fts_harness_placeholder(sqlite_path: str) -> None:
    del sqlite_path
    pytest.skip("SQLite FTS5 integration not wired in CI yet")
