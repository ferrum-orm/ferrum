"""Production runtime helpers: timeouts, retries, and query execution guards.

All behavior lives at the Python async I/O boundary (ADR-001). Rust is not involved.

Retry scope contract (ratified AGENTS.md §5a — binding):

- Statement-level ``RetryPolicy`` may apply **only** to discrete autocommit reads
  issued through a ``Connection``: ``fetch`` / ``fetchrow`` / ``fetchval`` and
  QuerySet read terminals that use them. Default remains ``retry=None``.
- If autocommit read retry is enabled, allowed categories are deadlock (``40P01``)
  and serialization failure (``40001``) only. ``connection`` and timeout are NOT
  valid statement-retry categories.
- Autocommit writes (``execute``, QuerySet ``create`` / ``update`` / ``delete`` /
  ``upsert``, DDL) must NOT statement-retry.
- Statements issued through a ``Transaction`` or savepoint never retry
  (object-scoped: ``TimedQueryExecutor.is_transaction=True``).
- Streams and cursors stay out of statement-retry scope (they bypass
  ``_execute_with_policy``).
- The only write-retry story is ``Connection.run_transaction(fn, retry=...)``:
  open a fresh transaction per attempt, replay the entire callback, restrict
  retries to allowlisted SQLSTATE (``40001`` / ``40P01``), use capped exponential
  backoff with jitter, honor cancellation/deadline, and require the callback to
  document its own idempotency.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ferrum.drivers.protocol import ChunkStreamProtocol
from ferrum.errors import (
    FerrumConnectionError,
    FerrumError,
    FerrumTimeoutError,
    map_db_error,
)

_T = TypeVar("_T")

# Retry categories matched against driver exceptions before Ferrum mapping.
# Per ratified §5a: only deadlock (40P01) and serialization (40001) are valid
# statement-retry categories for discrete autocommit reads. ``connection`` was
# removed because a client timeout or connection drop after the server committed
# can duplicate writes, and statement retry on DML is forbidden.
_RETRY_CATEGORIES: frozenset[str] = frozenset({"deadlock", "serialization"})


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
    return None


@dataclass(frozen=True)
class RetryPolicy:
    """Explicit, opt-in retry policy for discrete autocommit reads.

    Default Ferrum behavior is **no retries** (``retry=None``). Pass
    ``retry=RetryPolicy(...)`` to ``ferrum.connect()`` to enable retries for
    discrete autocommit reads only (``fetch`` / ``fetchrow`` / ``fetchval`` and
    QuerySet read terminals that use them).

    Per ratified §5a:

    - Allowed categories are ``deadlock`` (SQLSTATE 40P01) and ``serialization``
      (SQLSTATE 40001) only. ``connection`` is rejected — a client timeout or
      connection drop after the server committed can duplicate writes, and
      statement retry on DML is forbidden.
    - This policy never applies to writes (``execute``, ``create``, ``update``,
      ``delete``, ``upsert``, DDL) or to statements issued through a
      ``Transaction`` — those are disabled at the ``TimedQueryExecutor`` level.
    - The only write-retry story is ``Connection.run_transaction(fn, retry=...)``
      via :class:`TransactionRetryPolicy`.
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


# SQLSTATE codes that ``TransactionRetryPolicy`` treats as retriable.
# Per §5a: only serialization failure (40001) and deadlock (40P01) are valid
# write-retry SQLSTATEs. These are checked against the W1-D ``sqlstate``
# attribute on mapped Ferrum exceptions — no second taxonomy is introduced.
_RETRIABLE_XACT_SQLSTATES: frozenset[str] = frozenset({"40001", "40P01"})


@dataclass(frozen=True)
class TransactionRetryPolicy:
    """Retry policy for ``Connection.run_transaction(fn, retry=...)``.

    This is the **only** write-retry story (ratified §5a). It opens a fresh
    transaction per attempt, replays the entire callback, and retries only on
    allowlisted SQLSTATE (``40001`` serialization failure, ``40P01`` deadlock).

    Uses capped exponential backoff with jitter. Honors cancellation and
    deadline (via the transaction's ``deadline`` parameter) — a cancelled or
    timed-out attempt rolls back and re-raises without retry.

    The callback ``fn`` MUST document its own idempotency. Replay re-executes
    the entire callback from scratch on each attempt; non-idempotent side
    effects (e.g. sending an email, incrementing an external counter) must
    not be inside ``fn`` unless the caller can tolerate replay.
    """

    max_attempts: int = 3
    backoff_base: float = 0.05
    backoff_max: float = 1.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if self.backoff_base < 0:
            raise ValueError("backoff_base must be non-negative.")
        if self.backoff_max < self.backoff_base:
            raise ValueError("backoff_max must be >= backoff_base.")
        if not (0.0 <= self.jitter <= 1.0):
            raise ValueError("jitter must be in [0.0, 1.0].")

    def should_retry(self, exc: Exception, attempt: int) -> bool:
        """Return True if ``exc`` is a retriable SQLSTATE and attempts remain.

        Uses the W1-D ``sqlstate`` attribute on mapped ``FerrumError`` exceptions.
        Non-Ferrum exceptions (including ``CancelledError``) are never retried.
        """
        if attempt >= self.max_attempts:
            return False
        if not isinstance(exc, FerrumError):
            return False
        return exc.sqlstate in _RETRIABLE_XACT_SQLSTATES

    def backoff_seconds(self, attempt: int) -> float:
        """Capped exponential backoff with jitter for the given attempt (1-based).

        ``backoff_base * 2**(attempt-1)`` capped at ``backoff_max``, plus uniform
        jitter in ``[-jitter * delay, +jitter * delay]``.
        """
        base = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
        if self.jitter > 0 and base > 0:
            spread = base * self.jitter
            base += random.uniform(-spread, spread)  # noqa: S311 — non-crypto jitter
        return max(0.0, base)


@dataclass(frozen=True)
class AdvisoryLockKey:
    """Validated PostgreSQL advisory lock key.

    PostgreSQL advisory locks accept either a single 64-bit integer or two 32-bit
    integers (a high word and a low word). This class validates the input and
    provides the SQL arguments in the correct form.

    Accepts an ``int``, a ``(int, int)`` tuple, or another ``AdvisoryLockKey``.
    """

    _key: int | tuple[int, int]

    def __init__(self, key: int | tuple[int, int] | AdvisoryLockKey) -> None:
        if isinstance(key, AdvisoryLockKey):
            object.__setattr__(self, "_key", key._key)
            return
        if isinstance(key, bool):  # bool is a subclass of int — reject explicitly.
            raise TypeError(
                "Advisory lock key must be int or (int, int), got bool. Pass an explicit int."
            )
        if isinstance(key, int):
            if not (-(2**63) <= key < 2**63):
                raise ValueError(f"Advisory lock key {key} out of range (signed 64-bit integer).")
            object.__setattr__(self, "_key", key)
            return
        if (
            isinstance(key, tuple)
            and len(key) == 2
            and all(isinstance(k, int) and not isinstance(k, bool) for k in key)
        ):
            for k in key:
                if not (-(2**31) <= k < 2**31):
                    raise ValueError(
                        f"Advisory lock key half {k} out of range (signed 32-bit integer)."
                    )
            object.__setattr__(self, "_key", (int(key[0]), int(key[1])))
            return
        raise TypeError(f"Advisory lock key must be int or (int, int), got {type(key).__name__}.")

    def as_args(self) -> tuple[int, ...]:
        """Return the key as a tuple of ints for SQL parameter binding."""
        if isinstance(self._key, int):
            return (self._key,)
        return self._key

    def is_two_part(self) -> bool:
        """Return True if this is a two-part (int, int) key."""
        return isinstance(self._key, tuple)


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
        self._streams: set[ManagedChunkStream] = set()

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

    def register_stream(self, stream: ManagedChunkStream) -> None:
        self._streams.add(stream)

    def unregister_stream(self, stream: ManagedChunkStream) -> None:
        self._streams.discard(stream)

    async def close_streams(self) -> None:
        """Force active producers to close before their driver shuts down."""
        if self._streams:
            await asyncio.gather(
                *(stream.aclose() for stream in tuple(self._streams)),
                return_exceptions=True,
            )


class ManagedChunkStream:
    """Lifecycle-accounted wrapper around a driver chunk stream."""

    def __init__(self, inner: ChunkStreamProtocol, lifecycle: _LifecycleGuard) -> None:
        self._inner = inner
        self._lifecycle = lifecycle
        self._closed = False
        self._lifecycle.begin()
        self._lifecycle.register_stream(self)

    def __aiter__(self) -> AsyncIterator[list[Any]]:
        return self

    async def __anext__(self) -> list[Any]:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._inner.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._inner.aclose()
        finally:
            self._lifecycle.unregister_stream(self)
            self._lifecycle.end()


class TimedQueryExecutor:
    """Wraps a driver executor with query timeouts, retries, and lifecycle guards.

    Per ratified §5a:

    - ``is_transaction=True`` disables retry entirely (object-scoped: statements
      through a ``Transaction`` never retry).
    - ``is_write=True`` on a per-call basis disables retry for autocommit writes
      (``execute`` is always ``is_write=True``; ``fetch``/``fetchrow``/``fetchval``
      accept ``is_write`` as a keyword arg so QuerySet write terminals can opt out).
    - Remaining autocommit read retry, if a ``RetryPolicy`` is configured, is
      limited to deadlock (``40P01``) and serialization (``40001``) only.
    """

    def __init__(
        self,
        inner: Any,  # noqa: ANN401
        *,
        runtime: RuntimeConfig,
        lifecycle: _LifecycleGuard,
        is_transaction: bool = False,
    ) -> None:
        self._inner = inner
        self._runtime = runtime
        self._lifecycle = lifecycle
        self._is_transaction = is_transaction
        self.dialect: str = getattr(inner, "dialect", "postgres")

    async def _run(
        self,
        op: Callable[[], Awaitable[_T]],
        *,
        is_write: bool = False,
    ) -> _T:
        """Run one driver await while accounting for connection shutdown."""
        self._lifecycle.begin()
        try:
            return await self._execute_with_policy(op, is_write=is_write)
        finally:
            self._lifecycle.end()

    async def _execute_with_policy(
        self,
        op: Callable[[], Awaitable[_T]],
        *,
        is_write: bool = False,
    ) -> _T:
        """Apply timeout/retry policy at the Python await boundary.

        Retries are opt-in and category-limited. Exhausted or non-retriable
        driver exceptions are mapped through ``map_db_error`` so raw driver
        details do not escape the runtime layer.

        Per §5a: retry is disabled when ``is_transaction`` (object-scoped) or
        ``is_write`` (autocommit writes never statement-retry). The remaining
        autocommit read retry, if enabled, is deadlock/serialization only.
        """
        retry = self._runtime.retry
        if self._is_transaction or is_write:
            retry = None
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
                    f"Query exceeded their {self._runtime.query_timeout}s deadline. [FERR-E102]"
                ) from None
            except Exception as exc:
                if retry is not None and retry.should_retry(exc, attempt):
                    await asyncio.sleep(retry.backoff_base * attempt)
                    continue
                raise map_db_error(exc) from None

    async def fetch(self, sql: str, *params: object, is_write: bool = False) -> list[Any]:
        return await self._run(lambda: self._inner.fetch(sql, *params), is_write=is_write)

    async def fetchrow(self, sql: str, *params: object, is_write: bool = False) -> object | None:
        return await self._run(lambda: self._inner.fetchrow(sql, *params), is_write=is_write)

    async def fetchval(self, sql: str, *params: object, is_write: bool = False) -> object:
        return await self._run(lambda: self._inner.fetchval(sql, *params), is_write=is_write)

    async def execute(self, sql: str, *params: object) -> str:
        # ``execute`` is always treated as a write (INSERT/UPDATE/DELETE/DDL and
        # non-returning SELECTs). Per §5a autocommit writes never statement-retry.
        return await self._run(lambda: self._inner.execute(sql, *params), is_write=True)


async def drain_inflight(lifecycle: _LifecycleGuard, *, timeout: float) -> None:
    """Wait until in-flight operations complete or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while lifecycle.inflight > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
