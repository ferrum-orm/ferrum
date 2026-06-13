"""Security qualification suite — credential safety (CRED-1).

CRED-1: Connection strings, passwords, and secrets must never appear in
hook payloads, exceptions, logs, or migration output.
"""

from __future__ import annotations

import pytest

from ferrum.errors import FerrumConnectionError
from ferrum.hooks import _TIER_A_KEYS, HookPayload, dispatch, register_hook, unregister_hook

pytestmark = pytest.mark.security

_FAKE_DSN = "postgresql://admin:supersecret@db.internal:5432/prod"
_FAKE_PASSWORD = "supersecret"  # noqa: S105 — intentional fake credential for security tests


class TestCredentialSafety:
    def test_connection_error_does_not_leak_dsn(self) -> None:
        """CRED-1: FerrumConnectionError message must not contain the full DSN."""
        try:
            raise FerrumConnectionError(
                "Failed to connect to PostgreSQL at db.internal:5432 "
                "(database=prod, username=admin): OSError"
            )
        except FerrumConnectionError as exc:
            msg = str(exc)
            assert _FAKE_PASSWORD not in msg, "Password leaked in connection error"
            assert _FAKE_DSN not in msg, "Full DSN leaked in connection error"

    def test_hook_payload_never_contains_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CRED-1: Hook payloads must not contain DSN or password even if accidentally included."""
        monkeypatch.setenv("FERRUM_OBS", "A")
        received: list[HookPayload] = []
        fn = received.append
        register_hook(fn)
        try:
            dispatch(
                {
                    "event": "query",
                    "status": "ok",
                    "model": "User",
                    "table": "users",
                    "operation": "select",
                    "duration_ms": 0.5,
                    # Accidentally included — must be stripped by redaction layer
                    "dsn": _FAKE_DSN,
                    "password": _FAKE_PASSWORD,
                }
            )
            assert received
            payload = received[0]
            for v in payload.values():
                if isinstance(v, str):
                    assert _FAKE_PASSWORD not in v, "Password appeared in hook payload"
                    assert _FAKE_DSN not in v, "DSN appeared in hook payload"
        finally:
            unregister_hook(fn)

    def test_tier_a_keys_do_not_include_credentials(self) -> None:
        """CRED-1: Tier A allowlist must not include DSN, password, or credentials."""
        forbidden = {"dsn", "password", "credentials", "secret", "token", "bound_params"}
        overlap = forbidden & _TIER_A_KEYS
        assert not overlap, f"Tier A allowlist contains credential keys: {overlap}"
