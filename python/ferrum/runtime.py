"""Production runtime helpers: timeouts, retries, and query execution guards.

All behavior lives at the Python async I/O boundary (ADR-001). Rust is not involved.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ferrum.errors import (
    FerrumConnectionError,
    FerrumTimeoutError,
    map_db_error,
)

_T = TypeVar("_T")

# Retry categories matched against driver exceptions before Ferrum mapping.
_RETRY_CATEGORIES: frozenset[str] = frozenset({"deadlock", "connection", "serialization"})


def _exception_category(exc: Exception) -> str | None:
    """Map a raw driver exception to a retry category, or None if not retriable."""
    try:
        import asyncpg.exceptions as pg_exc  # type: ignore[import-untyped]
    except ImportError:
        return None

    deadlock = getattr(pg_exc, "DeadlockDetectedError", None)
    if deadlock is not None and isinstance(exc, deadlock):
        return "deadlock"
    serialization = getattr(pg_exc, "SerializationError", None)
    if serialization is not None and isinstance(exc, serialization):
        return "serialization"
    conn_err = getattr(pg_exc, "PostgresConnectionError", None)
    if conn_err is not None and isinstance(exc, conn_err):
        return "connection"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)):
        return "connection"
    return None


@dataclass(frozen=True)
class RetryPolicy:
    """Explicit, opt-in retry policy for database awaits.

    Default Ferrum behavior is **no retries**. Pass ``retry=RetryPolicy(...)`` to
    ``ferrum.connect()`` to enable retries for idempotent-safe categories only.
    """

    max_attempts: int = 3
    on: frozenset[str] = field(default_factory=lambda: frozenset({"deadlock"}))
    backoff_base: float = 0.05

    def __post_init__(self) -> None:
        unknown = self.on - _RETRY_CATEGORIES
        if unknown:
            msg = (
                f"Unknown retry categories: {sorted(unknown)}. "
                f"Allowed: {sorted(_RETRY_CATEGORIES)}."
            )
            raise ValueError(msg)
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")

    def should_retry(self, exc: Exception, attempt: int) -> bool:
        """Return whether ``exc`` should be retried before mapping to Ferrum errors."""
        if attempt >= self.max_attempts:
            return False
        category = _exception_category(exc)
        return category is not None and category in self.on


@dataclass(frozen=True)
class RuntimeConfig:
    """Connection-scoped runtime options applied at the Python await point."""

    acquire_timeout: float | None = None
    query_timeout: float | None = None
    statement_timeout_ms: int | None = None
    max_lifetime: float | None = None
    retry: RetryPolicy | None = None
    drain_timeout: float = 30.0


class _LifecycleGuard:
    """Tracks in-flight operations and whether the pool accepts new work."""

    def __init__(self) -> None:
        self._accepting = True
        self._inflight = 0

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def inflight(self) -> int:
        return self._inflight

    def reject_if_closing(self) -> None:
        if not self._accepting:
            raise FerrumConnectionError(
                "Connection pool is shutting down and cannot accept new work. [FERR-E101]"
            )

    def begin(self) -> None:
        self.reject_if_closing()
        self._inflight += 1

    def end(self) -> None:
        self._inflight -= 1

    def stop_accepting(self) -> None:
        self._accepting = False


class TimedQueryExecutor:
    """Wraps a driver executor with query timeouts, retries, and lifecycle guards."""

    def __init__(
        self,
        inner: Any,  # noqa: ANN401
        *,
        runtime: RuntimeConfig,
        lifecycle: _LifecycleGuard,
    ) -> None:
        self._inner = inner
        self._runtime = runtime
        self._lifecycle = lifecycle
        self.dialect: str = getattr(inner, "dialect", "postgres")

    async def _run(self, op: Callable[[], Awaitable[_T]]) -> _T:
        """Run one driver await while accounting for connection shutdown."""
        self._lifecycle.begin()
        try:
            return await self._execute_with_policy(op)
        finally:
            self._lifecycle.end()

    async def _execute_with_policy(self, op: Callable[[], Awaitable[_T]]) -> _T:
        """Apply timeout/retry policy at the Python await boundary.

        Retries are opt-in and category-limited. Exhausted or non-retriable
        driver exceptions are mapped through ``map_db_error`` so raw driver
        details do not escape the runtime layer.
        """
        retry = self._runtime.retry
        attempt = 0
        while True:
            attempt += 1
            try:
                if self._runtime.query_timeout is not None:
                    async with asyncio.timeout(self._runtime.query_timeout):
                        return await op()
                return await op()
            except TimeoutError:
                raise FerrumTimeoutError(
                    f"Query exceeded its {self._runtime.query_timeout}s deadline. [FERR-E102]"
                ) from None
            except Exception as exc:
                if retry is not None and retry.should_retry(exc, attempt):
                    await asyncio.sleep(retry.backoff_base * attempt)
                    continue
                raise map_db_error(exc) from None

    async def fetch(self, sql: str, *params: object) -> list[Any]:
        return await self._run(lambda: self._inner.fetch(sql, *params))

    async def fetchrow(self, sql: str, *params: object) -> object | None:
        return await self._run(lambda: self._inner.fetchrow(sql, *params))

    async def fetchval(self, sql: str, *params: object) -> object:
        return await self._run(lambda: self._inner.fetchval(sql, *params))

    async def execute(self, sql: str, *params: object) -> str:
        return await self._run(lambda: self._inner.execute(sql, *params))


async def drain_inflight(lifecycle: _LifecycleGuard, *, timeout: float) -> None:
    """Wait until in-flight operations complete or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while lifecycle.inflight > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
