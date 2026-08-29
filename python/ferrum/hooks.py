"""Ferrum observability hook dispatcher.

Implements the three-tier observability model (ARCHITECTURE.md §10 / SECURITY.md):

- **Tier A (default):** query fingerprint, operation, model, table, duration, status,
  failure category. Never includes bound values, DSN, or row data.
- **Tier B (opt-in):** normalized SQL text (no values). Requires ``FERRUM_OBS=B``.
- **Tier C (opt-in, local-dev only):** full SQL + bound values. Requires
  ``FERRUM_OBS=C``. MUST NOT be enabled in production or APM pipelines.

Registered hooks receive a ``HookPayload`` dict. Hook functions run synchronously
in the query dispatch path and must be fast. Async hooks are not supported in v0.1.

Security invariants:
- Bound parameter values never appear in Tier A or Tier B payloads (LOG-1).
- The ``_obs_level`` check is hardened against ``DEBUG=1`` environment leakage
  (LOG-2): activation requires a Ferrum-specific env variable.
- The redaction function is non-bypassable: it runs before any hook receives data.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from typing import Any, TypeAlias

HookPayload: TypeAlias = dict[str, Any]
HookFn: TypeAlias = Callable[[HookPayload], None]

_HOOKS: list[HookFn] = []

# Event-keyed hooks registered via ``register_hook(event, fn)``.
_EVENT_HOOKS: dict[str, list[HookFn]] = {}

# Tier A keys — the only keys allowed in default payloads (LOG-1).
# Extended by W4-A with safe enum/count/duration keys for pool, transaction,
# migration, retry, and timeout events. Every added key holds an enum string,
# integer count, float duration, or boolean — never a bound value, DSN,
# credential, row datum, or free-form user input. SecurityEngineer review
# required for any further additions.
_TIER_A_KEYS = frozenset(
    {
        # Original W1-D query-path keys (do not remove — W1-D contract).
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
        # W4-A pool-lifecycle keys (integer pool snapshot counts).
        "pool_size",
        "pool_idle",
        "pool_acquired_count",
        "pool_waiters",
        # W4-A transaction keys (enum isolation level + booleans).
        "isolation",
        "readonly",
        "deferrable",
        # W4-A migration keys (enum direction: "up" | "down").
        "direction",
        # W4-A retry keys (integer attempt number).
        "attempt",
    }
)


def _obs_level() -> str:
    """Return the active observability tier ('A', 'B', or 'C').

    Tier B/C require Ferrum-specific opt-in; ``DEBUG=1`` alone never elevates the tier.
    Tier C is only permitted when ``FERRUM_OBS_ALLOW_TIER_C=1`` is also set, as an
    additional guard against accidental production enablement.
    """
    raw = os.environ.get("FERRUM_OBS", "A").strip().upper()
    if raw == "C" and os.environ.get("FERRUM_OBS_ALLOW_TIER_C") != "1":
        return "B"
    return raw if raw in ("A", "B", "C") else "A"


def _redact(payload: HookPayload) -> HookPayload:
    """Return a copy of ``payload`` safe to emit at the current tier level.

    - Tier A: only keys in ``_TIER_A_KEYS`` are kept.
    - Tier B: adds ``sql_normalized`` (no values).
    - Tier C: also adds ``sql_text`` and ``bound_params`` (local-dev only).

    The redaction step cannot be bypassed: it runs before dispatching to any hook.
    """
    level = _obs_level()
    safe: HookPayload = {k: v for k, v in payload.items() if k in _TIER_A_KEYS}

    if level in ("B", "C") and "sql_normalized" in payload:
        safe["sql_normalized"] = payload["sql_normalized"]

    if level == "C":
        if "sql_text" in payload:
            safe["sql_text"] = payload["sql_text"]
        if "bound_params" in payload:
            safe["bound_params"] = payload["bound_params"]

    return safe


def register_hook(event: str, fn: HookFn) -> None:
    """Register a hook function for a specific query event.

    ``event`` must be one of ``"query_start"``, ``"query_success"``, or
    ``"query_failure"``. Use ``"*"`` to receive all events (catch-all).

    Hook functions are called synchronously; keep them fast. A crashing hook
    never propagates to the query path (errors are silently suppressed).

    For test use, call ``clear_hooks()`` in teardown to avoid cross-test leakage.
    """
    _EVENT_HOOKS.setdefault(event, []).append(fn)


def unregister_hook(fn: HookFn) -> None:
    """Unregister a previously registered hook function from all event slots."""
    with contextlib.suppress(ValueError):
        _HOOKS.remove(fn)
    for slot in _EVENT_HOOKS.values():
        with contextlib.suppress(ValueError):
            slot.remove(fn)


def clear_hooks() -> None:
    """Remove all registered hooks (catch-all and event-specific).

    Intended for test teardown. Do not call in production code.
    """
    _HOOKS.clear()
    _EVENT_HOOKS.clear()


def dispatch(payload: HookPayload) -> None:
    """Dispatch a hook payload to all registered hooks after redaction.

    The payload is redacted before any hook sees it. Hooks cannot receive
    more data than the current tier allows. Dispatches to:
    - Catch-all hooks in ``_HOOKS`` (legacy ``_HOOKS.append`` usage).
    - Event-specific hooks in ``_EVENT_HOOKS[event]`` where ``event`` matches
      ``payload["event"]``.
    - ``_EVENT_HOOKS["*"]`` catch-all slot registered via ``register_hook("*", fn)``.
    """
    safe = _redact(payload)
    event: str = payload.get("event", "")  # type: ignore[assignment]
    for hook in list(_HOOKS):
        with contextlib.suppress(Exception):
            # A crashing hook must never break the query path.
            hook(safe)
    for hook in list(_EVENT_HOOKS.get(event, [])):
        with contextlib.suppress(Exception):
            hook(safe)
    if event:
        for hook in list(_EVENT_HOOKS.get("*", [])):
            with contextlib.suppress(Exception):
                hook(safe)


def query_start(
    *,
    fingerprint: str,
    model: str,
    operation: str,
    table: str,
) -> None:
    """Dispatch a Tier A ``query_start`` hook payload.

    Fires before SQL execution. Contains only identifiers — never bound values
    or row data (LOG-1).
    """
    dispatch(
        {
            "event": "query_start",
            "fingerprint": fingerprint,
            "model": model,
            "operation": operation,
            "table": table,
        }
    )


def query_success(
    *,
    fingerprint: str,
    duration_ms: float,
    row_count: int,
) -> None:
    """Dispatch a Tier A ``query_success`` hook payload.

    Fires after successful SQL execution. ``row_count`` is the number of rows
    returned or affected — never the row data itself (LOG-1).
    """
    dispatch(
        {
            "event": "query_success",
            "fingerprint": fingerprint,
            "duration_ms": round(duration_ms, 3),
            "rows_affected": row_count,
            "status": "ok",
        }
    )


def query_failure(
    *,
    fingerprint: str,
    duration_ms: float,
    failure_category: str,
    category: str | None = None,
) -> None:
    """Dispatch a Tier A ``query_failure`` hook payload.

    Fires when SQL execution raises an exception. ``failure_category`` MUST be
    a Ferrum error class name (e.g. ``"FerrumIntegrityError"``), never a raw
    SQLSTATE code, exception message, or bound value (LOG-1).

    ``category`` is the closed-enum error category from ``ERROR_CATEGORIES``
    (e.g. ``"unique_violation"``, ``"deadlock"``, ``"connection"``). It is a
    Tier A key and survives default redaction. When ``category`` is ``None``
    (e.g. the caller has not yet been updated to pass it), the key is simply
    omitted from the payload.
    """
    payload: HookPayload = {
        "event": "query_failure",
        "fingerprint": fingerprint,
        "duration_ms": round(duration_ms, 3),
        "failure_category": failure_category,
        "status": "error",
    }
    if category is not None:
        payload["category"] = category
    dispatch(payload)


def hydration_failure(
    *,
    fingerprint: str,
    failure_category: str,
    model: str,
    category: str | None = None,
) -> None:
    """Dispatch a Tier A ``hydration_failure`` hook payload.

    Fires when ``_native.hydrate_rows()`` raises during the live read path.
    The payload contains only model/column metadata — never row values or
    bound parameters (LOG-1, ERR-1).

    Args:
        fingerprint: Query fingerprint (operation + model, no values).
        failure_category: Ferrum error class name (e.g. ``"FerrumHydrationError"``).
        model: Model class name — safe metadata, never user input.
        category: Closed-enum error category from ``ERROR_CATEGORIES``
            (e.g. ``"hydration"``). Tier A key; omitted when ``None``.
    """
    payload: HookPayload = {
        "event": "hydration_failure",
        "fingerprint": fingerprint,
        "failure_category": failure_category,
        "model": model,
        "status": "error",
    }
    if category is not None:
        payload["category"] = category
    dispatch(payload)


class QueryTimer:
    """Context manager that times a query and dispatches a Tier A hook payload."""

    def __init__(self, *, model: str, table: str, operation: str) -> None:
        self._model = model
        self._table = table
        self._operation = operation
        self._start: float = 0.0

    def __enter__(self) -> QueryTimer:
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, _: object) -> None:
        duration_ms = (time.monotonic() - self._start) * 1000
        status = "error" if exc_val is not None else "ok"
        failure_category = type(exc_val).__name__ if exc_val is not None else None
        # Extract the closed-enum category from FerrumError exceptions (§5a).
        category = getattr(exc_val, "category", None) if exc_val is not None else None
        payload: HookPayload = {
            "event": "query",
            "model": self._model,
            "table": self._table,
            "operation": self._operation,
            "duration_ms": round(duration_ms, 3),
            "status": status,
        }
        if failure_category:
            payload["failure_category"] = failure_category
        if category:
            payload["category"] = category
        dispatch(payload)


# ---------------------------------------------------------------------------
# W4-A: Tier-A-safe helpers for pool, transaction, migration, retry, timeout.
#
# These helpers dispatch payloads that carry ONLY Tier-A-safe fields (enums,
# integer counts, durations, booleans). They never accept or carry bound
# parameter values, DSNs, credentials, row data, or free-form user input
# (LOG-1, §3). Dispatch sites in connection.py / runtime.py / migrations /
# drivers are owned by other workstreams; the helpers here are the contract
# those dispatch sites will call when instrumented.
# ---------------------------------------------------------------------------


def pool_acquire(
    *,
    pool_size: int = -1,
    pool_idle: int = -1,
    pool_acquired_count: int = -1,
    pool_waiters: int = -1,
) -> None:
    """Dispatch a Tier A ``pool_acquire`` hook payload.

    Fires when a pooled connection is acquired. All fields are integer pool
    snapshots (``-1`` sentinel when unavailable) — never DSNs, credentials, or
    bound values (LOG-1, §3).
    """
    dispatch(
        {
            "event": "pool_acquire",
            "pool_size": pool_size,
            "pool_idle": pool_idle,
            "pool_acquired_count": pool_acquired_count,
            "pool_waiters": pool_waiters,
        }
    )


def pool_release(
    *,
    pool_size: int = -1,
    pool_idle: int = -1,
    pool_acquired_count: int = -1,
    pool_waiters: int = -1,
) -> None:
    """Dispatch a Tier A ``pool_release`` hook payload.

    Fires when a pooled connection is returned to the pool. Same integer-only
    fields as :func:`pool_acquire` (LOG-1, §3).
    """
    dispatch(
        {
            "event": "pool_release",
            "pool_size": pool_size,
            "pool_idle": pool_idle,
            "pool_acquired_count": pool_acquired_count,
            "pool_waiters": pool_waiters,
        }
    )


def pool_wait(*, pool_waiters: int = -1) -> None:
    """Dispatch a Tier A ``pool_wait`` hook payload.

    Fires when an acquire call begins waiting for a free connection. Only the
    integer waiter count is carried (LOG-1, §3).
    """
    dispatch({"event": "pool_wait", "pool_waiters": pool_waiters})


def pool_timeout(*, pool_waiters: int = -1) -> None:
    """Dispatch a Tier A ``pool_timeout`` hook payload.

    Fires when an acquire call times out waiting for a connection. Carries
    only the integer waiter count — never the DSN or password (LOG-1, §3).
    """
    dispatch({"event": "pool_timeout", "pool_waiters": pool_waiters})


def pool_shutdown(*, pool_size: int = -1, pool_acquired_count: int = -1) -> None:
    """Dispatch a Tier A ``pool_shutdown`` hook payload.

    Fires when the pool begins graceful shutdown (stop_accepting). Integer
    snapshot fields only (LOG-1, §3).
    """
    dispatch(
        {
            "event": "pool_shutdown",
            "pool_size": pool_size,
            "pool_acquired_count": pool_acquired_count,
        }
    )


def transaction_start(
    *,
    isolation: str | None = None,
    readonly: bool = False,
    deferrable: bool = False,
) -> None:
    """Dispatch a Tier A ``transaction_start`` hook payload.

    Fires when a transaction begins. ``isolation`` is one of the allowlisted
    enum values (``serializable`` / ``repeatable_read`` / ``read_committed`` /
    ``read_unmitted``) or ``None`` for the server default. ``readonly`` and
    ``deferrable`` are booleans. No bound values or row data (LOG-1, §3).
    """
    dispatch(
        {
            "event": "transaction_start",
            "isolation": isolation if isolation is not None else "default",
            "readonly": readonly,
            "deferrable": deferrable,
        }
    )


def transaction_end(
    *,
    duration_ms: float,
    status: str,
    isolation: str | None = None,
    readonly: bool = False,
    deferrable: bool = False,
) -> None:
    """Dispatch a Tier A ``transaction_end`` hook payload.

    Fires when a transaction commits or rolls back. ``status`` is ``"ok"`` or
    ``"error"``. ``duration_ms`` is the wall-clock duration in milliseconds.
    Enum/boolean/duration fields only (LOG-1, §3).
    """
    dispatch(
        {
            "event": "transaction_end",
            "duration_ms": round(duration_ms, 3),
            "status": status,
            "isolation": isolation if isolation is not None else "default",
            "readonly": readonly,
            "deferrable": deferrable,
        }
    )


def migration_event(
    *,
    event: str,
    direction: str,
    status: str,
    duration_ms: float = 0.0,
) -> None:
    """Dispatch a Tier A ``migration_event`` hook payload.

    Fires during migration apply/revert. ``event`` is one of ``"migration_start"``
    / ``"migration_end"``. ``direction`` is ``"up"`` or ``"down"``. ``status`` is
    ``"ok"`` or ``"error"``. The migration *id* / filename is intentionally NOT
    carried — it is project-relative free-form text and a cardinality risk for
    metric labels. Enum/duration fields only (LOG-1, §3).
    """
    dispatch(
        {
            "event": event,
            "direction": direction,
            "status": status,
            "duration_ms": round(duration_ms, 3),
        }
    )


def retry_attempt(*, attempt: int, category: str) -> None:
    """Dispatch a Tier A ``retry_attempt`` hook payload.

    Fires when a retriable operation is retried. ``attempt`` is the 1-based
    attempt number. ``category`` is the closed-enum error category from
    ``ERROR_CATEGORIES`` (e.g. ``"deadlock"``, ``"serialization"``). Integer +
    closed-enum only — never the exception message or bound values (LOG-1, §3).
    """
    dispatch({"event": "retry_attempt", "attempt": attempt, "category": category})


def timeout_event(*, category: str, duration_ms: float = 0.0) -> None:
    """Dispatch a Tier A ``timeout_event`` hook payload.

    Fires when an operation times out (pool acquire, query, transaction
    deadline). ``category`` is the closed-enum error category (typically
    ``"timeout"``). Closed-enum + duration only (LOG-1, §3).
    """
    dispatch(
        {
            "event": "timeout_event",
            "category": category,
            "duration_ms": round(duration_ms, 3),
        }
    )
