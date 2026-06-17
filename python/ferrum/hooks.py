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
_TIER_A_KEYS = frozenset(
    {
        "event",
        "model",
        "table",
        "operation",
        "fingerprint",
        "duration_ms",
        "status",
        "failure_category",
        "rows_affected",
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
) -> None:
    """Dispatch a Tier A ``query_failure`` hook payload.

    Fires when SQL execution raises an exception. ``failure_category`` MUST be
    a Ferrum error class name (e.g. ``"FerrumIntegrityError"``), never a raw
    SQLSTATE code, exception message, or bound value (LOG-1).
    """
    dispatch(
        {
            "event": "query_failure",
            "fingerprint": fingerprint,
            "duration_ms": round(duration_ms, 3),
            "failure_category": failure_category,
            "status": "error",
        }
    )


def hydration_failure(
    *,
    fingerprint: str,
    failure_category: str,
    model: str,
) -> None:
    """Dispatch a Tier A ``hydration_failure`` hook payload.

    Fires when ``_native.hydrate_rows()`` raises during the live read path.
    The payload contains only model/column metadata — never row values or
    bound parameters (LOG-1, ERR-1).

    Args:
        fingerprint: Query fingerprint (operation + model, no values).
        failure_category: Ferrum error class name (e.g. ``"FerrumHydrationError"``).
        model: Model class name — safe metadata, never user input.
    """
    dispatch(
        {
            "event": "hydration_failure",
            "fingerprint": fingerprint,
            "failure_category": failure_category,
            "model": model,
            "status": "error",
        }
    )


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
        dispatch(payload)
