"""Bounded asyncpg server-cursor streaming.

The producer owns the cursor lifecycle. A one-slot queue bounds read-ahead to
one chunk and applies backpressure when the consumer is slower than PostgreSQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, cast

from ferrum.drivers.protocol import CompiledQuery
from ferrum.errors import FerrumTimeoutError, map_db_error

_END = object()


@dataclass(frozen=True)
class _Failure:
    error: BaseException


class AsyncpgChunkStream:
    """Background cursor producer with one-chunk bounded buffering."""

    def __init__(
        self,
        source: Callable[[], contextlib.AbstractAsyncContextManager[Any]],
        compiled: CompiledQuery,
        *,
        chunk_size: int,
        query_timeout: float | None,
    ) -> None:
        self._source = source
        self._compiled = compiled
        self._chunk_size = chunk_size
        self._query_timeout = query_timeout
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
        self._closed = False
        self._terminal_enqueued = False
        self._task = asyncio.create_task(self._produce())

    def __aiter__(self) -> AsyncIterator[list[Any]]:
        return self

    async def __anext__(self) -> list[Any]:
        if self._closed:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _END:
            await self.aclose()
            raise StopAsyncIteration
        if isinstance(item, _Failure):
            await self.aclose()
            raise item.error
        return cast(list[Any], item)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._task.done() and not self._terminal_enqueued:
            self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(_END)

    async def _await_driver(self, awaitable: Any) -> Any:
        if self._query_timeout is None:
            return await awaitable
        try:
            async with asyncio.timeout(self._query_timeout):
                return await awaitable
        except TimeoutError:
            raise FerrumTimeoutError(
                f"Streaming cursor operation exceeded its {self._query_timeout}s "
                "deadline. [FERR-E102]"
            ) from None

    async def _produce(self) -> None:
        cursor_name = f"ferrum_stream_{uuid.uuid4().hex}"
        quoted_name = f'"{cursor_name}"'
        declared = False
        raw: Any = None
        try:
            async with self._source() as raw:
                declare_sql = (
                    f"DECLARE {quoted_name} NO SCROLL CURSOR FOR {self._compiled.sql_text}"
                )
                await self._await_driver(raw.execute(declare_sql, *self._compiled.bound_params))
                declared = True
                fetch_sql = f"FETCH FORWARD {self._chunk_size} FROM {quoted_name}"
                while True:
                    rows = list(await self._await_driver(raw.fetch(fetch_sql)))
                    if not rows:
                        break
                    await self._queue.put(rows)
                await self._queue.put(_END)
                self._terminal_enqueued = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mapped = exc if isinstance(exc, FerrumTimeoutError) else map_db_error(exc)
            await self._queue.put(_Failure(mapped))
            self._terminal_enqueued = True
        finally:
            if declared and raw is not None:
                with contextlib.suppress(BaseException):
                    await raw.execute(f"CLOSE {quoted_name}")
