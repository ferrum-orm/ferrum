"""Live coverage for bounded cursor streaming."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

import pytest

from ferrum.connection import Connection
from ferrum.drivers.protocol import _compiled_query

from .backends import Backend, Capability


@pytest.mark.integration
async def test_streams_compiled_query_in_bounded_chunks(
    backend: Backend,
    requires: Callable[[Capability], None],
) -> None:
    requires(Capability.STREAMING)

    dsn = os.environ[backend.dsn_env]
    conn = Connection(dsn, min_size=1, max_size=1)
    await conn.open()
    try:
        compiled = _compiled_query(
            "SELECT value FROM generate_series($1::int, $2::int) AS value",
            [1, 7],
        )
        seen: list[int] = []
        async with conn.stream_compiled(compiled, chunk_size=3) as chunks:
            async for chunk in chunks:
                assert len(chunk) <= 3
                seen.extend(row["value"] for row in chunk)
        assert seen == list(range(1, 8))
        assert await conn.health_check() is True
    finally:
        await conn.close()


@pytest.mark.integration
async def test_early_break_releases_single_pool_connection(
    backend: Backend,
    requires: Callable[[Capability], None],
) -> None:
    requires(Capability.STREAMING)

    dsn = os.environ[backend.dsn_env]
    conn = Connection(dsn, min_size=1, max_size=1, acquire_timeout=0.5)
    await conn.open()
    try:
        compiled = _compiled_query("SELECT generate_series(1, 1000) AS value", [])
        async with conn.stream_compiled(compiled, chunk_size=10) as chunks:
            async for chunk in chunks:
                assert len(chunk) == 10
                break
        assert await conn.health_check() is True
    finally:
        await conn.close()


@pytest.mark.integration
async def test_stream_cancellation_closes_cursor_and_releases_connection(
    backend: Backend,
    requires: Callable[[Capability], None],
) -> None:
    requires(Capability.STREAMING)

    dsn = os.environ[backend.dsn_env]
    conn = Connection(dsn, min_size=1, max_size=1, acquire_timeout=1.0)
    await conn.open()
    started = asyncio.Event()

    async def consume() -> None:
        compiled = _compiled_query(
            "SELECT value, pg_sleep(0.05) FROM generate_series(1, 1000) AS value",
            [],
        )
        async with conn.stream_compiled(compiled, chunk_size=10) as chunks:
            started.set()
            await anext(chunks)

    try:
        task = asyncio.create_task(consume())
        await started.wait()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await conn.health_check() is True
    finally:
        await conn.close()
