"""Unit tests for the hook dispatcher and Tier A payload enforcement.

LOG-1: default hook payloads must contain only Tier A keys.
LOG-2: Tier B/C must not activate from generic DEBUG=1.
"""

from __future__ import annotations

import os

import pytest

import ferrum.hooks as hooks
from ferrum.hooks import (
    _TIER_A_KEYS,
    HookPayload,
    clear_hooks,
    dispatch,
    register_hook,
    unregister_hook,
)


class TestTierAEnforcement:
    def test_dispatch_strips_non_tier_a_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERRUM_OBS", "A")
        received: list[HookPayload] = []
        fn = received.append
        register_hook("*", fn)
        try:
            dispatch(
                {
                    "event": "query",
                    "model": "User",
                    "table": "users",
                    "operation": "select",
                    "duration_ms": 1.5,
                    "status": "ok",
                    # Must be stripped:
                    "sql_text": "SELECT * FROM users WHERE id = $1",
                    "bound_params": ["42"],
                    "secret": "should not appear",
                }
            )
            assert len(received) == 1
            payload = received[0]
            assert "sql_text" not in payload
            assert "bound_params" not in payload
            assert "secret" not in payload
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
        finally:
            clear_hooks()

    def test_tier_b_does_not_activate_from_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERRUM_OBS", raising=False)
        monkeypatch.setenv("DEBUG", "1")
        level = hooks._obs_level()
        assert level == "A", "DEBUG=1 must not elevate Ferrum observability tier"

    def test_tier_c_requires_ferrum_specific_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERRUM_OBS", "C")
        monkeypatch.delenv("FERRUM_OBS_ALLOW_TIER_C", raising=False)
        level = hooks._obs_level()
        assert level == "B", "Tier C must require FERRUM_OBS_ALLOW_TIER_C=1"

    def test_tier_c_with_explicit_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERRUM_OBS", "C")
        monkeypatch.setenv("FERRUM_OBS_ALLOW_TIER_C", "1")
        level = hooks._obs_level()
        assert level == "C"

    def test_crashing_hook_does_not_propagate(self) -> None:
        def bad_hook(_: HookPayload) -> None:
            raise RuntimeError("hook crash")

        register_hook("*", bad_hook)
        try:
            dispatch({"event": "query", "status": "ok"})
        finally:
            clear_hooks()

    # ------------------------------------------------------------------
    # LOG-1: bound values and DSN never leak through Tier A redaction
    # ------------------------------------------------------------------

    def test_tier_a_dispatch_never_contains_bound_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LOG-1: Tier A redaction must strip bound_params and any carrying value."""
        monkeypatch.delenv("FERRUM_OBS", raising=False)
        received: list[HookPayload] = []
        fn = received.append
        register_hook("*", fn)
        try:
            dispatch(
                {
                    "event": "query_start",
                    "model": "User",
                    "fingerprint": "abc",
                    "status": "ok",
                    # Carry a recognizable sentinel in the bound_params key.
                    "bound_params": [{"type": "text", "value": "secret_pw_sentinel"}],
                }
            )
            assert len(received) == 1
            payload = received[0]
            payload_str = str(payload)
            assert "secret_pw_sentinel" not in payload_str, (
                "Bound parameter value must not survive Tier A redaction"
            )
            assert "bound_params" not in payload, (
                "bound_params key must not survive Tier A redaction"
            )
        finally:
            unregister_hook(fn)

    def test_tier_a_dispatch_never_contains_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LOG-1: DSN and password keys must be stripped by Tier A redaction."""
        monkeypatch.delenv("FERRUM_OBS", raising=False)
        received: list[HookPayload] = []
        fn = received.append
        register_hook("*", fn)
        try:
            dispatch(
                {
                    "event": "query",
                    "model": "User",
                    "status": "ok",
                    # Inject DSN and password as extra keys — must be stripped.
                    "dsn": "postgresql://user:supersecret@localhost:5432/db",
                    "password": "supersecret",
                }
            )
            assert len(received) == 1
            payload = received[0]
            payload_str = str(payload)
            assert "supersecret" not in payload_str, "Password must not survive Tier A redaction"
            assert "dsn" not in payload, "dsn key must not survive Tier A redaction"
            assert "password" not in payload, "password key must not survive Tier A redaction"
        finally:
            unregister_hook(fn)

    def test_tier_a_allowed_keys_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LOG-1: Every key in a Tier A payload must be in _TIER_A_KEYS — no exceptions."""
        monkeypatch.delenv("FERRUM_OBS", raising=False)
        received: list[HookPayload] = []
        fn = received.append
        register_hook("*", fn)
        try:
            dispatch(
                {
                    "event": "query",
                    "model": "User",
                    "table": "users",
                    "operation": "select",
                    "fingerprint": "abc123",
                    "duration_ms": 2.0,
                    "status": "ok",
                    # Non-Tier-A keys — all must be stripped.
                    "extra_key": "should_not_appear",
                    "bound_params": [42],
                    "sql_text": "SELECT ...",
                    "arbitrary": "noise",
                }
            )
            assert len(received) == 1
            payload_keys = set(received[0].keys())
            assert payload_keys.issubset(_TIER_A_KEYS), (
                f"Payload contains non-Tier-A keys: {payload_keys - _TIER_A_KEYS}"
            )
        finally:
            unregister_hook(fn)

    # ------------------------------------------------------------------
    # LOG-2: validation / compile errors must not echo submitted values
    # ------------------------------------------------------------------

    def test_compile_error_does_not_echo_filter_value(self) -> None:
        """LOG-2: FerrumCompileError raised for an unknown field must not echo the filter value.

        Field names are metadata (safe to surface); submitted *values* are user
        input and must never appear in error messages.
        """
        from ferrum.errors import FerrumCompileError
        from ferrum.models import Model
        from ferrum.queryset import QuerySet

        class _Probe(Model):
            id: int = 0
            name: str = ""

        sentinel = "TOP_SECRET_FILTER_VALUE_67890"
        with pytest.raises(FerrumCompileError) as exc_info:
            # The field name is unknown — the value must not leak into the message.
            qs = QuerySet(_Probe).filter(nonexistent_field=sentinel)
            qs._build_ir()

        error_message = str(exc_info.value)
        assert sentinel not in error_message, (
            f"Filter value {sentinel!r} must not be echoed in the compile error message"
        )

    def test_hook_failure_payload_has_category_not_raw_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ERR-1/LOG-1: query_failure payloads must carry failure_category, not raw DB messages.

        Raw PostgreSQL messages (which may contain row data from DETAIL clauses)
        must never survive Tier A redaction into hook payloads.
        """
        monkeypatch.delenv("FERRUM_OBS", raising=False)
        received: list[HookPayload] = []
        fn = received.append
        register_hook("*", fn)
        try:
            raw_pg_msg = (
                "ERROR: duplicate key value violates unique constraint "
                "DETAIL: Key (email)=(sentinel_row_data@example.com) already exists."
            )
            dispatch(
                {
                    "event": "query",
                    "model": "User",
                    "status": "error",
                    "failure_category": "integrity_error",
                    # Non-Tier-A keys — must be stripped.
                    "raw_db_message": raw_pg_msg,
                    "db_detail": raw_pg_msg,
                }
            )
            assert len(received) == 1
            payload = received[0]
            # Structured taxonomy category survives (it is a Tier A key).
            assert payload.get("failure_category") == "integrity_error", (
                "failure_category (taxonomy string) must be present in Tier A payload"
            )
            # Raw DB message with row data must not survive redaction.
            payload_str = str(payload)
            assert "raw_db_message" not in payload, (
                "raw_db_message key must be stripped by Tier A redaction"
            )
            assert "sentinel_row_data" not in payload_str, (
                "Row-level data from PostgreSQL DETAIL must not appear in Tier A hook payload"
            )
        finally:
            unregister_hook(fn)

    # ------------------------------------------------------------------
    # Tier B/C must not activate from DEBUG=1  (LOG-2 / LOG-3)
    # ------------------------------------------------------------------

    def test_tier_b_not_activated_by_debug_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LOG-2/LOG-3: DEBUG=1 must not auto-set FERRUM_OBS or activate Tier B content.

        Tier B (normalized SQL) requires explicit ``FERRUM_OBS=B``. A generic
        ``DEBUG=1`` must never elevate the observability tier.
        """
        monkeypatch.delenv("FERRUM_OBS", raising=False)
        monkeypatch.setenv("DEBUG", "1")

        # FERRUM_OBS must not be present in the environment (DEBUG=1 must not set it).
        assert "FERRUM_OBS" not in os.environ, "DEBUG=1 must not automatically set FERRUM_OBS"
        # Tier level must remain A.
        assert hooks._obs_level() == "A", "DEBUG=1 must not elevate observability tier above A"

        # Dispatched payloads must not carry Tier B content.
        received: list[HookPayload] = []
        fn = received.append
        register_hook("*", fn)
        try:
            dispatch(
                {
                    "event": "query",
                    "model": "User",
                    "status": "ok",
                    "sql_normalized": "SELECT id FROM users WHERE id = ?",
                    "sql_text": "SELECT id FROM users WHERE id = $1",
                }
            )
            assert len(received) == 1
            payload = received[0]
            assert "sql_normalized" not in payload, (
                "Tier B key 'sql_normalized' must not appear when DEBUG=1 is the only flag"
            )
            assert "sql_text" not in payload, (
                "Tier C key 'sql_text' must not appear when DEBUG=1 is the only flag"
            )
        finally:
            unregister_hook(fn)
