"""Unit tests for production runtime: timeouts, retries, lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from ferrum.errors import FerrumConnectionError, FerrumTimeoutError
from ferrum.runtime import RetryPolicy, TimedQueryExecutor, _LifecycleGuard


class _FakeInner:
    dialect = "postgres"

    def __init__(self, *, fail_times: int = 0, exc: Exception | None = None) -> None:
        self.calls = 0
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("boom")

    async def fetchval(self, _sql: str, *_params: object) -> int:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return 1


@pytest.mark.asyncio
async def test_query_timeout_raises_ferrum_timeout() -> None:
    inner = _FakeInner()

    async def slow(_sql: str, *_params: object) -> int:
        await asyncio.sleep(0.05)
        return 1

    inner.fetchval = slow  # type: ignore[method-assign]

    from ferrum.runtime import RuntimeConfig

    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(
        inner, runtime=RuntimeConfig(query_timeout=0.001), lifecycle=guard
    )
    with pytest.raises(FerrumTimeoutError, match="FERR-E102"):
        await executor.fetchval("SELECT 1")


@pytest.mark.asyncio
async def test_retry_policy_retries_deadlock() -> None:
    try:
        import asyncpg.exceptions as pg_exc  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("asyncpg not installed")

    inner = _FakeInner(fail_times=1, exc=pg_exc.DeadlockDetectedError("deadlock"))
    policy = RetryPolicy(max_attempts=2, on=frozenset({"deadlock"}))
    from ferrum.runtime import RuntimeConfig

    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(retry=policy), lifecycle=guard)
    assert await executor.fetchval("SELECT 1") == 1
    assert inner.calls == 2


def test_retry_policy_rejects_unknown_category() -> None:
    with pytest.raises(ValueError, match="Unknown retry categories"):
        RetryPolicy(on=frozenset({"not_a_category"}))


@pytest.mark.asyncio
async def test_lifecycle_guard_rejects_when_closing() -> None:
    guard = _LifecycleGuard()
    guard.stop_accepting()
    with pytest.raises(FerrumConnectionError, match="shutting down"):
        guard.reject_if_closing()


@pytest.mark.asyncio
async def test_timed_executor_tracks_inflight() -> None:
    inner = _FakeInner()
    from ferrum.runtime import RuntimeConfig

    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(), lifecycle=guard)
    assert guard.inflight == 0
    await executor.fetchval("SELECT 1")
    assert guard.inflight == 0
