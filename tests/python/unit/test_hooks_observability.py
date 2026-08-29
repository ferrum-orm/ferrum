"""Unit tests for W4-A hook helpers (pool/transaction/migration/retry/timeout).

Verifies that the new helper functions in hooks.py:
- Dispatch Tier-A payloads only (no bound values, DSNs, row data).
- Carry the correct event names and Tier-A-safe fields.
- Preserve the W1-D Tier-A hook contract (existing keys unchanged).
"""

from __future__ import annotations

import pytest

from ferrum.hooks import (
    _TIER_A_KEYS,
    HookPayload,
    clear_hooks,
    dispatch,
    hydration_failure,
    migration_event,
    pool_acquire,
    pool_release,
    pool_shutdown,
    pool_timeout,
    pool_wait,
    query_failure,
    query_start,
    query_success,
    register_hook,
    retry_attempt,
    timeout_event,
    transaction_end,
    transaction_start,
)


@pytest.fixture(autouse=True)
def _clean_hooks() -> None:
    clear_hooks()
    yield
    clear_hooks()


# ---------------------------------------------------------------------------
# W1-D preservation — existing helpers still dispatch Tier-A payloads.
# ---------------------------------------------------------------------------


class TestW1DContractPreserved:
    def test_tier_a_keys_include_w1d_set(self) -> None:
        """The original W1-D keys MUST remain in _TIER_A_KEYS."""
        for key in (
            "event",
            "model",
            "table",
            "operation",
            "fingerprint",
            "duration_ms",
            "status",
            "failure_category",
            "category",
            "rows_affected",
        ):
            assert key in _TIER_A_KEYS, f"W1-D key {key!r} must remain in _TIER_A_KEYS"

    def test_query_start_dispatches_tier_a_only(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            query_start(fingerprint="fp", model="User", operation="select", table="users")
            assert len(received) == 1
            payload = received[0]
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
            assert payload["event"] == "query_start"
        finally:
            clear_hooks()

    def test_query_failure_dispatches_tier_a_only(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            query_failure(
                fingerprint="fp",
                duration_ms=1.0,
                failure_category="FerrumIntegrityError",
                category="unique_violation",
            )
            assert len(received) == 1
            payload = received[0]
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
            assert payload["category"] == "unique_violation"
        finally:
            clear_hooks()

    def test_query_success_dispatches_tier_a_only(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            query_success(fingerprint="fp", duration_ms=1.0, row_count=3)
            assert len(received) == 1
            payload = received[0]
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
            assert payload["rows_affected"] == 3
        finally:
            clear_hooks()

    def test_hydration_failure_dispatches_tier_a_only(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            hydration_failure(
                fingerprint="fp",
                failure_category="FerrumHydrationError",
                model="Post",
                category="hydration",
            )
            assert len(received) == 1
            payload = received[0]
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
            assert payload["category"] == "hydration"
        finally:
            clear_hooks()


# ---------------------------------------------------------------------------
# W4-A pool helpers.
# ---------------------------------------------------------------------------


class TestPoolHelpers:
    @pytest.mark.parametrize(
        "fn,kwargs,event",
        [
            (pool_acquire, {"pool_size": 10, "pool_idle": 5}, "pool_acquire"),
            (pool_release, {"pool_size": 10, "pool_idle": 6}, "pool_release"),
            (pool_wait, {"pool_waiters": 2}, "pool_wait"),
            (pool_timeout, {"pool_waiters": 3}, "pool_timeout"),
            (pool_shutdown, {"pool_size": 2, "pool_acquired_count": 1}, "pool_shutdown"),
        ],
    )
    def test_pool_helpers_dispatch_tier_a_only(self, fn, kwargs: dict, event: str) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            fn(**kwargs)
            assert len(received) == 1
            payload = received[0]
            assert payload["event"] == event
            assert set(payload.keys()).issubset(_TIER_A_KEYS), (
                f"Pool payload has non-Tier-A keys: {set(payload.keys()) - _TIER_A_KEYS}"
            )
        finally:
            clear_hooks()

    def test_pool_helpers_never_carry_dsn_or_credentials(self) -> None:
        """Pool payloads must never carry DSN/password/user input (LOG-1, §3)."""
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            pool_acquire(pool_size=10, pool_idle=5, pool_acquired_count=5, pool_waiters=0)
            assert len(received) == 1
            payload = received[0]
            payload_str = str(payload)
            assert "dsn" not in payload
            assert "password" not in payload
            assert "://" not in payload_str
        finally:
            clear_hooks()

    def test_pool_helpers_strip_non_tier_a_keys(self) -> None:
        """If a caller tries to inject bound values via dispatch, redaction strips them."""
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            dispatch(
                {
                    "event": "pool_acquire",
                    "pool_size": 10,
                    "dsn": "postgresql://user:secret@host/db",
                    "password": "secret",
                    "bound_params": ["secret"],
                }
            )
            assert len(received) == 1
            payload = received[0]
            assert "dsn" not in payload
            assert "password" not in payload
            assert "bound_params" not in payload
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
            assert "secret" not in str(payload)
        finally:
            clear_hooks()


# ---------------------------------------------------------------------------
# W4-A transaction helpers.
# ---------------------------------------------------------------------------


class TestTransactionHelpers:
    def test_transaction_start_dispatches_tier_a(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            transaction_start(isolation="serializable", readonly=True, deferrable=False)
            assert len(received) == 1
            payload = received[0]
            assert payload["event"] == "transaction_start"
            assert payload["isolation"] == "serializable"
            assert payload["readonly"] is True
            assert payload["deferrable"] is False
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
        finally:
            clear_hooks()

    def test_transaction_start_default_isolation(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            transaction_start()
            assert len(received) == 1
            assert received[0]["isolation"] == "default"
        finally:
            clear_hooks()

    def test_transaction_end_dispatches_tier_a(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            transaction_end(duration_ms=25.0, status="ok", isolation="read_committed")
            assert len(received) == 1
            payload = received[0]
            assert payload["event"] == "transaction_end"
            assert payload["duration_ms"] == 25.0
            assert payload["status"] == "ok"
            assert payload["isolation"] == "read_committed"
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
        finally:
            clear_hooks()


# ---------------------------------------------------------------------------
# W4-A migration / retry / timeout helpers.
# ---------------------------------------------------------------------------


class TestMigrationRetryTimeoutHelpers:
    def test_migration_event_dispatches_tier_a(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            migration_event(event="migration_end", direction="up", status="ok", duration_ms=120.0)
            assert len(received) == 1
            payload = received[0]
            assert payload["event"] == "migration_end"
            assert payload["direction"] == "up"
            assert payload["status"] == "ok"
            assert payload["duration_ms"] == 120.0
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
        finally:
            clear_hooks()

    def test_migration_event_does_not_carry_migration_id(self) -> None:
        """Migration id/filename is a cardinality risk and is NOT a Tier-A key."""
        assert "migration_id" not in _TIER_A_KEYS
        assert "migration_name" not in _TIER_A_KEYS
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            dispatch(
                {
                    "event": "migration_end",
                    "direction": "up",
                    "status": "ok",
                    "duration_ms": 1.0,
                    "migration_id": "0001_initial",
                }
            )
            assert len(received) == 1
            payload = received[0]
            assert "migration_id" not in payload, (
                "migration_id must be stripped (cardinality + free-form text)"
            )
        finally:
            clear_hooks()

    def test_retry_attempt_dispatches_tier_a(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            retry_attempt(attempt=2, category="deadlock")
            assert len(received) == 1
            payload = received[0]
            assert payload["event"] == "retry_attempt"
            assert payload["attempt"] == 2
            assert payload["category"] == "deadlock"
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
        finally:
            clear_hooks()

    def test_timeout_event_dispatches_tier_a(self) -> None:
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            timeout_event(category="timeout", duration_ms=30.0)
            assert len(received) == 1
            payload = received[0]
            assert payload["event"] == "timeout_event"
            assert payload["category"] == "timeout"
            assert payload["duration_ms"] == 30.0
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
        finally:
            clear_hooks()

    def test_retry_attempt_does_not_carry_exception_message(self) -> None:
        """Retry payload must never carry the raw exception message (LOG-1)."""
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            dispatch(
                {
                    "event": "retry_attempt",
                    "attempt": 1,
                    "category": "deadlock",
                    "exception_message": "process 42 detected deadlock over tuple (a, b)",
                }
            )
            assert len(received) == 1
            payload = received[0]
            assert "exception_message" not in payload
            assert "deadlock over tuple" not in str(payload)
        finally:
            clear_hooks()
