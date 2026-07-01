"""Gated MySQL FTS integration harness (requires FERRUM_TEST_MYSQL_DSN)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def mysql_dsn() -> str:
    dsn = os.environ.get("FERRUM_TEST_MYSQL_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_MYSQL_DSN not set")
    return dsn


@pytest.mark.asyncio
async def test_mysql_fts_harness_placeholder(mysql_dsn: str) -> None:
    """Placeholder — extend when CI MySQL FTS is available."""
    del mysql_dsn
    pytest.skip("MySQL FTS integration not wired in CI yet")
