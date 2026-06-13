"""Unit tests for the hook dispatcher and Tier A payload enforcement.

LOG-1: default hook payloads must contain only Tier A keys.
LOG-2: Tier B/C must not activate from generic DEBUG=1.
"""

from __future__ import annotations

import os

import pytest

import ferrum.hooks as hooks
from ferrum.hooks import _TIER_A_KEYS, HookPayload, dispatch, register_hook, unregister_hook


class TestTierAEnforcement:
    def test_dispatch_strips_non_tier_a_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERRUM_OBS", "A")
        received: list[HookPayload] = []
        fn = received.append
        register_hook(fn)
        try:
            dispatch({
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
            })
            assert len(received) == 1
            payload = received[0]
            assert "sql_text" not in payload
            assert "bound_params" not in payload
            assert "secret" not in payload
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
        finally:
            unregister_hook(fn)

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

        register_hook(bad_hook)
        try:
            dispatch({"event": "query", "status": "ok"})
        finally:
            unregister_hook(bad_hook)
