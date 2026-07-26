"""Unit coverage for bounded ConnectionLike streaming lifecycle semantics."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import ferrum
from ferrum.connection import Connection, Transaction
from ferrum.drivers.protocol import CompiledQuery, _compiled_query
from ferrum.drivers.streaming import AsyncpgChunkStream
from ferrum.errors import FerrumCompileError, FerrumDeferredFieldError
from ferrum.queryset import QuerySet


class _FakeStream:
    def __init__(
        self,
        chunks: list[list[Any]] | None = None,
        *,
        error: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self._chunks = iter(chunks or [])
        self._error = error
        self._block = block
        self.entered = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> AsyncIterator[list[Any]]:
        return self

    async def __anext__(self) -> list[Any]:
        self.entered.set()
        if self._block:
            await asyncio.Event().wait()
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class _FakeStreamingDriver:
    dialect = "postgres"

    def __init__(self, stream: _FakeStream) -> None:
        self.stream = stream
        self.open_args: tuple[object, int, float | None] | None = None
        self.closed = False

    def open_stream(
        self,
        compiled: object,
        *,
        chunk_size: int,
        query_timeout: float | None,
    ) -> _FakeStream:
        self.open_args = (compiled, chunk_size, query_timeout)
        return self.stream

    async def close(self) -> None:
        self.closed = True


def _connection_with(stream: _FakeStream, *, query_timeout: float | None = None) -> Connection:
    conn = Connection("postgresql://u@localhost/db", query_timeout=query_timeout)
    conn._driver = _FakeStreamingDriver(stream)
    return conn


async def test_stream_exhaustion_closes_and_releases_lifecycle() -> None:
    raw = _FakeStream([[{"id": 1}], [{"id": 2}]])
    conn = _connection_with(raw, query_timeout=2.0)
    compiled = _compiled_query("compiler output", [1])

    async with conn.stream_compiled(compiled, chunk_size=1) as chunks:
        assert [chunk async for chunk in chunks] == [[{"id": 1}], [{"id": 2}]]

    assert raw.closed is True
    assert conn._lifecycle.inflight == 0
    assert conn._driver.open_args == (compiled, 1, 2.0)


async def test_early_break_closes_stream_deterministically() -> None:
    raw = _FakeStream([[1], [2]])
    conn = _connection_with(raw)

    async with conn.stream_compiled(_compiled_query("compiler output", [])) as chunks:
        async for chunk in chunks:
            assert chunk == [1]
            break

    assert raw.closed is True
    assert conn._lifecycle.inflight == 0


async def test_stream_exception_closes_before_propagating() -> None:
    raw = _FakeStream(error=RuntimeError("cursor failed"))
    conn = _connection_with(raw)

    with pytest.raises(RuntimeError, match="cursor failed"):
        async with conn.stream_compiled(_compiled_query("compiler output", [])) as chunks:
            await anext(chunks)

    assert raw.closed is True
    assert conn._lifecycle.inflight == 0


async def test_consumer_cancellation_closes_stream() -> None:
    raw = _FakeStream(block=True)
    conn = _connection_with(raw)

    async def consume() -> None:
        async with conn.stream_compiled(_compiled_query("compiler output", [])) as chunks:
            await anext(chunks)

    task = asyncio.create_task(consume())
    await raw.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert raw.closed is True
    assert conn._lifecycle.inflight == 0


async def test_connection_shutdown_force_closes_active_stream() -> None:
    raw = _FakeStream(block=True)
    conn = _connection_with(raw)
    context = conn.stream_compiled(_compiled_query("compiler output", []))
    chunks = await context.__aenter__()

    await conn.close()
    await context.__aexit__(None, None, None)

    assert raw.closed is True
    assert conn._lifecycle.inflight == 0
    assert conn._driver is None
    with pytest.raises(StopAsyncIteration):
        await anext(chunks)


async def test_transaction_exposes_same_streaming_seam() -> None:
    raw = _FakeStream([[1]])
    driver = _FakeStreamingDriver(raw)
    tx = Transaction(driver, "postgres")

    async with tx.stream_compiled(_compiled_query("compiler output", [])) as chunks:
        assert await anext(chunks) == [1]

    assert raw.closed is True


class _FakeRawConnection:
    def __init__(self, rows: list[dict[str, int]], *, block_fetch: bool = False) -> None:
        self.rows = rows
        self.block_fetch = block_fetch
        self.fetch_started = asyncio.Event()
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *params: object) -> str:
        self.calls.append((sql, params))
        return "OK"

    async def fetch(self, sql: str) -> list[dict[str, int]]:
        self.calls.append((sql, ()))
        self.fetch_started.set()
        if self.block_fetch:
            await asyncio.Event().wait()
        chunk_size = int(sql.split()[2])
        chunk, self.rows = self.rows[:chunk_size], self.rows[chunk_size:]
        return chunk


def _driver_stream(raw: _FakeRawConnection, *, chunk_size: int = 2) -> AsyncpgChunkStream:
    @contextlib.asynccontextmanager
    async def source() -> AsyncGenerator[_FakeRawConnection, None]:
        yield raw

    return AsyncpgChunkStream(
        source,
        _compiled_query("SELECT value FROM compiled_query WHERE id = $1", [7]),
        chunk_size=chunk_size,
        query_timeout=None,
    )


async def test_asyncpg_stream_declares_fetches_and_closes_cursor() -> None:
    raw = _FakeRawConnection([{"id": 1}, {"id": 2}, {"id": 3}])
    stream = _driver_stream(raw)

    assert [chunk async for chunk in stream] == [
        [{"id": 1}, {"id": 2}],
        [{"id": 3}],
    ]
    assert raw.calls[0][0].startswith('DECLARE "ferrum_stream_')
    assert raw.calls[0][0].endswith(
        "NO SCROLL CURSOR FOR SELECT value FROM compiled_query WHERE id = $1"
    )
    assert raw.calls[0][1] == (7,)
    assert raw.calls[-1][0].startswith('CLOSE "ferrum_stream_')


async def test_asyncpg_stream_cancellation_closes_server_cursor() -> None:
    raw = _FakeRawConnection([], block_fetch=True)
    stream = _driver_stream(raw)
    next_chunk = asyncio.create_task(anext(stream))
    await raw.fetch_started.wait()

    await stream.aclose()
    with pytest.raises(StopAsyncIteration):
        await next_chunk

    assert raw.calls[-1][0].startswith('CLOSE "ferrum_stream_')


@pytest.mark.parametrize("chunk_size", [0, -1])
async def test_invalid_chunk_size_is_rejected(chunk_size: int) -> None:
    conn = _connection_with(_FakeStream())
    with pytest.raises(ValueError, match="chunk_size"):
        async with conn.stream_compiled(
            _compiled_query("compiler output", []),
            chunk_size=chunk_size,
        ):
            pass


class _StreamModel(ferrum.Model):
    model_config = ferrum.ModelConfig(table="stream_models")

    id: int = 0
    name: str = ""
    score: int = 0


class _StreamProjection(ferrum.Model):
    model_config = ferrum.ModelConfig(table="stream_models")

    id: int = 0
    name: str = ""


class _QuerySetStreamConnection:
    dialect = "postgres"

    def __init__(self, chunks: list[list[dict[str, Any]]]) -> None:
        self.chunks = chunks
        self.compiled: CompiledQuery | None = None
        self.chunk_size: int | None = None
        self.closed = False

    @contextlib.asynccontextmanager
    async def stream_compiled(
        self,
        compiled: CompiledQuery,
        *,
        chunk_size: int,
    ) -> AsyncGenerator[AsyncIterator[list[Any]], None]:
        self.compiled = compiled
        self.chunk_size = chunk_size
        stream = _FakeStream(self.chunks)
        try:
            yield stream
        finally:
            await stream.aclose()
            self.closed = stream.closed


def _native_stream_compiler() -> MagicMock:
    native = MagicMock()
    native.compile_query.return_value = {
        "sql_text": 'SELECT "id", "name", "score" FROM "stream_models" WHERE "id" > $1',
        "bound_params": ['{"type": "int", "value": 0}'],
        "fingerprint": "stream-fingerprint",
    }
    return native


async def test_queryset_stream_materializes_chunks_and_decodes_params() -> None:
    conn = _QuerySetStreamConnection(
        [[{"id": 1, "name": "one", "score": 10}], [{"id": 2, "name": "two", "score": 20}]]
    )

    with patch("ferrum.queryset._native_ext", _native_stream_compiler()):
        async with (
            QuerySet(_StreamModel).filter(id__gt=0).stream(cast(Any, conn), chunk_size=1) as chunks
        ):
            materialized = [chunk async for chunk in chunks]

    assert [[row.id for row in chunk] for chunk in materialized] == [[1], [2]]
    assert conn.compiled is not None
    assert conn.compiled.bound_params == (0,)
    assert conn.chunk_size == 1
    assert conn.closed is True


async def test_queryset_stream_preserves_projection_values_and_deferred_shapes() -> None:
    projection_conn = _QuerySetStreamConnection([[{"id": 1, "name": "one"}]])
    values_conn = _QuerySetStreamConnection([[{"id": 1, "name": "one"}]])
    deferred_conn = _QuerySetStreamConnection([[{"id": 1, "name": "one"}]])
    native = _native_stream_compiler()

    with patch("ferrum.queryset._native_ext", native):
        async with (
            QuerySet(_StreamModel)
            .project(_StreamProjection)
            .stream(cast(Any, projection_conn)) as chunks
        ):
            projected = await anext(chunks)
        async with (
            QuerySet(_StreamModel).values("id", "name").stream(cast(Any, values_conn)) as chunks
        ):
            values = await anext(chunks)
        async with QuerySet(_StreamModel).defer("score").stream(cast(Any, deferred_conn)) as chunks:
            deferred = await anext(chunks)

    assert isinstance(projected[0], _StreamProjection)
    assert projected[0].name == "one"
    assert values == [{"id": 1, "name": "one"}]
    with pytest.raises(FerrumDeferredFieldError):
        _ = deferred[0].score


async def test_queryset_stream_rejects_prefetch_related_before_compilation() -> None:
    conn = _QuerySetStreamConnection([])
    queryset = QuerySet(_StreamModel)
    queryset._prefetch_related = ("children",)
    with pytest.raises(FerrumCompileError, match="prefetch_related"):
        async with queryset.stream(cast(Any, conn)):
            pass
