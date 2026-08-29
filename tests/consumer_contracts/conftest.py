"""Fixtures for the consumer-parity contract suite.

Deliberately self-contained (does not import ``tests/python/integration``,
an unowned path for this workstream) but mirrors that suite's ``pg_conn`` /
``pg_dsn`` / ``unique_suffix`` / ``require_native`` fixtures exactly, so the
same ``FERRUM_TEST_DSN`` env var and skip behavior apply here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

import ferrum


@pytest.fixture
def require_native() -> None:
    """Skip when the maturin-built Rust extension is not importable."""
    pytest.importorskip(
        "ferrum._native",
        reason="Rust extension not built — run `maturin develop`",
    )


@pytest.fixture
def unique_suffix() -> str:
    """Unique hex suffix for transient table/function names (parallel-safe)."""
    return uuid.uuid4().hex[:12]


@pytest.fixture
def pg_dsn() -> str:
    """Raw DSN string for tests that manage their own connection lifecycle.

    Skips when ``FERRUM_TEST_DSN`` is not set.
    """
    dsn = os.environ.get("FERRUM_TEST_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_DSN not set")
    return dsn


@pytest_asyncio.fixture
async def pg_conn(pg_dsn: str) -> AsyncIterator[ferrum.connection.Connection]:
    """Open Ferrum connection backed by a live PostgreSQL instance."""
    async with ferrum.connect(pg_dsn) as conn:
        yield conn
