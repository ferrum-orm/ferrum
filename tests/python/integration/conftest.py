"""Integration-test fixtures for tests that require a live PostgreSQL instance.

Tests using ``pg_conn`` are automatically skipped when ``FERRUM_TEST_DSN``
is not set in the environment.  Run the full integration suite with::

    FERRUM_TEST_DSN="postgresql://user:pass@localhost/ferrum_test" \\
        pytest -m integration tests/python/integration/

The DSN must point to a PostgreSQL instance where the test user has CREATE TABLE
/ DROP TABLE privileges on the target database.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

import ferrum


@pytest_asyncio.fixture
async def pg_conn() -> ferrum.connection.Connection:
    """Yield an open Ferrum connection pool backed by a real PostgreSQL instance.

    Skips the calling test when ``FERRUM_TEST_DSN`` is not set.
    """
    dsn = os.environ.get("FERRUM_TEST_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_DSN not set")
    async with ferrum.connect(dsn) as conn:
        yield conn
