"""Unit tests for production runtime: timeouts, retries, lifecycle.

Covers the ratified §5a retry contract:

- ``RetryPolicy`` rejects ``connection`` and unknown categories.
- ``TimedQueryExecutor`` with ``is_transaction=True`` never retries (object-scoped).
- Autocommit writes (``execute``) never retry.
- Autocommit read retry (``fetch``/``fetchrow``/``fetchval``), when enabled, is
  deadlock (40P01) and serialization (40001) only.
- ``TransactionRetryPolicy`` retries only 40001/40P01, uses backoff with jitter,
  and rejects non-Ferrum exceptions (including cancellation).
- ``AdvisoryLockKey`` validates int and (int, int) ranges.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ferrum.errors import FerrumDatabaseError, FerrumError, FerrumIntegrityError
from ferrum.runtime import (
    AdvisoryLockKey,
    RetryPolicy,
    RuntimeConfig,
    TimedQueryExecutor,
    TransactionRetryPolicy,
    _LifecycleGuard,
)


class _FakeInner:
    """Stand-in for a driver executor that records calls and can fail N times."""

    dialect = "postgres"

    def __init__(
        self,
        *,
        fail_times: int = 0,
        exc: Exception | None = None,
        op: str = "fetchval",
    ) -> None:
        self.calls = 0
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("boom")
        self._op = op

    async def fetchval(self, _sql: str, *_params: object) -> int:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return 1

    async def fetchrow(self, _sql: str, *_params: object) -> Any | None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return None

    async def fetch(self, _sql: str, *_params: object) -> list[Any]:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return []

    async def execute(self, _sql: str, *_params: object) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return "OK"


def _pg_exc(name: str) -> Exception:
    """Construct an asyncpg exception by class name (skips if asyncpg missing)."""
    pytest.importorskip("asyncpg.exceptions")
    import asyncpg.exceptions as pg_exc  # type: ignore[import-untyped]

    cls = getattr(pg_exc, name)
    return cls("test")


# ---------------------------------------------------------------------------
# RetryPolicy — category validation (§5a: only deadlock/serialization)
# ---------------------------------------------------------------------------


def test_retry_policy_rejects_connection_category() -> None:
    """``connection`` is no longer a valid statement-retry category (§5a)."""
    with pytest.raises(ValueError, match="Unknown retry categories"):
        RetryPolicy(on=frozenset({"connection"}))


def test_retry_policy_rejects_unknown_category() -> None:
    with pytest.raises(ValueError, match="Unknown retry categories"):
        RetryPolicy(on=frozenset({"not_a_category"}))


def test_retry_policy_accepts_deadlock_and_serialization() -> None:
    RetryPolicy(on=frozenset({"deadlock"}))
    RetryPolicy(on=frozenset({"serialization"}))
    RetryPolicy(on=frozenset({"deadlock", "serialization"}))


def test_retry_policy_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)


# ---------------------------------------------------------------------------
# TimedQueryExecutor — object-scoped and write-scoped no-retry (§5a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_timeout_raises_ferrum_timeout() -> None:
    inner = _FakeInner()

    async def slow(_sql: str, *_params: object) -> int:
        await asyncio.sleep(0.05)
        return 1

    inner.fetchval = slow  # type: ignore[method-assign]

    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(
        inner, runtime=RuntimeConfig(query_timeout=0.001), lifecycle=guard
    )
    with pytest.raises(FerrumError, match="FERR-E102"):
        await executor.fetchval("SELECT 1")


@pytest.mark.asyncio
async def test_autocommit_read_retries_on_deadlock() -> None:
    """Autocommit read (fetchval) retries on deadlock when a policy is set."""
    exc = _pg_exc("DeadlockDetectedError")
    inner = _FakeInner(fail_times=1, exc=exc)
    policy = RetryPolicy(max_attempts=2, on=frozenset({"deadlock"}))
    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(retry=policy), lifecycle=guard)
    assert await executor.fetchval("SELECT 1") == 1
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_autocommit_read_retries_on_serialization() -> None:
    """Autocommit read retries on serialization failure when a policy is set."""
    exc = _pg_exc("SerializationError")
    inner = _FakeInner(fail_times=1, exc=exc)
    policy = RetryPolicy(max_attempts=2, on=frozenset({"serialization"}))
    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(retry=policy), lifecycle=guard)
    assert await executor.fetchval("SELECT 1") == 1
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_autocommit_write_never_retries() -> None:
    """Autocommit writes (execute) never statement-retry even with a policy."""
    exc = _pg_exc("DeadlockDetectedError")
    inner = _FakeInner(fail_times=1, exc=exc)
    policy = RetryPolicy(max_attempts=3, on=frozenset({"deadlock"}))
    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(retry=policy), lifecycle=guard)
    with pytest.raises(FerrumError):
        await executor.execute("UPDATE t SET x = 1")
    assert inner.calls == 1  # no retry


@pytest.mark.asyncio
async def test_transaction_never_retries_reads() -> None:
    """Object-scoped: a Transaction-pinned executor never retries, even on reads."""
    exc = _pg_exc("DeadlockDetectedError")
    inner = _FakeInner(fail_times=1, exc=exc)
    policy = RetryPolicy(max_attempts=3, on=frozenset({"deadlock"}))
    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(
        inner,
        runtime=RuntimeConfig(retry=policy),
        lifecycle=guard,
        is_transaction=True,
    )
    with pytest.raises(FerrumError):
        await executor.fetchval("SELECT 1")
    assert inner.calls == 1  # no retry — object-scoped


@pytest.mark.asyncio
async def test_fetch_with_is_write_true_never_retries() -> None:
    """``fetch(is_write=True)`` disables retry (e.g. bulk_create with returning)."""
    exc = _pg_exc("DeadlockDetectedError")
    inner = _FakeInner(fail_times=1, exc=exc, op="fetch")
    policy = RetryPolicy(max_attempts=3, on=frozenset({"deadlock"}))
    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(retry=policy), lifecycle=guard)
    with pytest.raises(FerrumError):
        await executor.fetch("INSERT ... RETURNING *", is_write=True)
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_fetch_with_is_write_false_retries_on_deadlock() -> None:
    """``fetch(is_write=False)`` (default) retries on deadlock for reads."""
    exc = _pg_exc("DeadlockDetectedError")
    inner = _FakeInner(fail_times=1, exc=exc, op="fetch")
    policy = RetryPolicy(max_attempts=2, on=frozenset({"deadlock"}))
    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(retry=policy), lifecycle=guard)
    assert await executor.fetch("SELECT * FROM t") == []
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_lifecycle_guard_rejects_when_closing() -> None:
    guard = _LifecycleGuard()
    guard.stop_accepting()
    with pytest.raises(FerrumError, match="shutting down"):
        guard.reject_if_closing()


@pytest.mark.asyncio
async def test_timed_executor_tracks_inflight() -> None:
    inner = _FakeInner()
    guard = _LifecycleGuard()
    executor = TimedQueryExecutor(inner, runtime=RuntimeConfig(), lifecycle=guard)
    assert guard.inflight == 0
    await executor.fetchval("SELECT 1")
    assert guard.inflight == 0


# ---------------------------------------------------------------------------
# TransactionRetryPolicy — write-retry (§5a: only 40001/40P01)
# ---------------------------------------------------------------------------


def test_transaction_retry_policy_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        TransactionRetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="backoff_max"):
        TransactionRetryPolicy(backoff_base=2.0, backoff_max=1.0)
    with pytest.raises(ValueError, match="jitter"):
        TransactionRetryPolicy(jitter=-0.1)
    with pytest.raises(ValueError, match="jitter"):
        TransactionRetryPolicy(jitter=1.1)


def test_transaction_retry_policy_should_retry_on_serialization() -> None:
    policy = TransactionRetryPolicy(max_attempts=3)
    exc = FerrumDatabaseError("test", sqlstate="40001", category="serialization")
    assert policy.should_retry(exc, 1)


def test_transaction_retry_policy_should_retry_on_deadlock() -> None:
    policy = TransactionRetryPolicy(max_attempts=3)
    exc = FerrumDatabaseError("test", sqlstate="40P01", category="deadlock")
    assert policy.should_retry(exc, 1)


def test_transaction_retry_policy_no_retry_on_non_retriable_sqlstate() -> None:
    policy = TransactionRetryPolicy(max_attempts=3)
    exc = FerrumIntegrityError("test", sqlstate="23505")
    assert not policy.should_retry(exc, 1)


def test_transaction_retry_policy_no_retry_on_non_ferrum_exception() -> None:
    policy = TransactionRetryPolicy(max_attempts=3)
    assert not policy.should_retry(ValueError("not ferrum"), 1)
    assert not policy.should_retry(asyncio.CancelledError(), 1)


def test_transaction_retry_policy_no_retry_when_attempts_exhausted() -> None:
    policy = TransactionRetryPolicy(max_attempts=2)
    exc = FerrumDatabaseError("test", sqlstate="40001")
    assert policy.should_retry(exc, 1)
    assert not policy.should_retry(exc, 2)


def test_transaction_retry_policy_backoff_is_capped() -> None:
    policy = TransactionRetryPolicy(max_attempts=5, backoff_base=0.1, backoff_max=1.0, jitter=0.0)
    assert policy.backoff_seconds(1) == 0.1
    assert policy.backoff_seconds(2) == 0.2
    assert policy.backoff_seconds(3) == 0.4
    # Capped at backoff_max.
    assert policy.backoff_seconds(10) == 1.0


def test_transaction_retry_policy_backoff_jitter_in_range() -> None:
    policy = TransactionRetryPolicy(backoff_base=1.0, backoff_max=10.0, jitter=0.5)
    for attempt in range(1, 10):
        delay = policy.backoff_seconds(attempt)
        base = min(1.0 * (2 ** (attempt - 1)), 10.0)
        assert max(0.0, base * 0.5) <= delay <= base * 1.5


# ---------------------------------------------------------------------------
# AdvisoryLockKey — validation
# ---------------------------------------------------------------------------


def test_advisory_lock_key_accepts_int() -> None:
    key = AdvisoryLockKey(42)
    assert key.as_args() == (42,)
    assert not key.is_two_part()


def test_advisory_lock_key_accepts_tuple() -> None:
    key = AdvisoryLockKey((1, 2))
    assert key.as_args() == (1, 2)
    assert key.is_two_part()


def test_advisory_lock_key_accepts_negative_int() -> None:
    key = AdvisoryLockKey(-(2**63))
    assert key.as_args() == (-(2**63),)


def test_advisory_lock_key_accepts_negative_tuple() -> None:
    key = AdvisoryLockKey((-(2**31), 0))
    assert key.as_args() == (-(2**31), 0)


def test_advisory_lock_key_rejects_int_overflow() -> None:
    with pytest.raises(ValueError, match="out of range"):
        AdvisoryLockKey(2**63)
    with pytest.raises(ValueError, match="out of range"):
        AdvisoryLockKey(-(2**63) - 1)


def test_advisory_lock_key_rejects_tuple_overflow() -> None:
    with pytest.raises(ValueError, match="out of range"):
        AdvisoryLockKey((2**31, 0))
    with pytest.raises(ValueError, match="out of range"):
        AdvisoryLockKey((0, -(2**31) - 1))


def test_advisory_lock_key_rejects_bad_tuple_length() -> None:
    with pytest.raises(TypeError, match="int or \\(int, int\\)"):
        AdvisoryLockKey((1, 2, 3))  # type: ignore[arg-type]


def test_advisory_lock_key_rejects_non_int() -> None:
    with pytest.raises(TypeError, match="int or \\(int, int\\)"):
        AdvisoryLockKey("not a key")  # type: ignore[arg-type]


def test_advisory_lock_key_rejects_bool() -> None:
    with pytest.raises(TypeError, match="bool"):
        AdvisoryLockKey(True)  # type: ignore[arg-type]


def test_advisory_lock_key_accepts_another_key() -> None:
    original = AdvisoryLockKey(42)
    copy = AdvisoryLockKey(original)
    assert copy.as_args() == (42,)
