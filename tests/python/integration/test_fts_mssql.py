"""Gated MSSQL FTS integration harness (requires FERRUM_TEST_MSSQL_DSN).

Full-text indexes populate asynchronously; tests may be flaky when FTS is enabled.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def mssql_dsn() -> str:
    dsn = os.environ.get("FERRUM_TEST_MSSQL_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_MSSQL_DSN not set")
    return dsn


@pytest.mark.asyncio
async def test_mssql_fts_harness_placeholder(mssql_dsn: str) -> None:
    del mssql_dsn
    pytest.skip("MSSQL FTS integration not wired in CI yet")
