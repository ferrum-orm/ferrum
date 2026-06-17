"""Fixtures for performance benchmarks.

DB-backed benchmarks require ``FERRUM_TEST_DSN`` (or ``DATABASE_URL`` as fallback).
Hook and native compile micro-benchmarks run without a live database.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

import ferrum


def _resolve_test_dsn() -> str | None:
    return os.environ.get("FERRUM_TEST_DSN") or os.environ.get("DATABASE_URL")


@pytest.fixture
def benchmark_dsn() -> str:
    dsn = _resolve_test_dsn()
    if not dsn:
        pytest.skip("FERRUM_TEST_DSN or DATABASE_URL not set")
    return dsn


@pytest_asyncio.fixture
async def bench_conn(benchmark_dsn: str) -> ferrum.connection.Connection:
    """Open a connection pool sized for concurrency benchmarks."""
    async with ferrum.connect(
        benchmark_dsn,
        min_size=2,
        max_size=50,
    ) as conn:
        yield conn
