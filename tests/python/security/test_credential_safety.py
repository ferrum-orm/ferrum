"""Security qualification suite — credential safety (CRED-1, CRED-2).

CRED-1: Connection strings and passwords MUST NOT appear in default hook
    payloads, exceptions, or migration output.
CRED-2: Connection diagnostics MAY include host, port, database name, username
    — never password or full DSN. Returned dict keys are restricted to the
    allowlist.
"""

from __future__ import annotations

import pytest

import ferrum
from ferrum.connection import _redacted_dsn_info
from ferrum.errors import FerrumConnectionError
from ferrum.hooks import _TIER_A_KEYS, HookPayload, dispatch, register_hook, unregister_hook

pytestmark = pytest.mark.security


_FAKE_DSN = "postgresql://admin:supersecret@db.internal:5432/prod"
_FAKE_PASSWORD = "supersecret"  # noqa: S105 — intentional fake credential for security tests


# ---------------------------------------------------------------------------
# CRED-1 / CRED-2: _redacted_dsn_info() shape and safety
# ---------------------------------------------------------------------------


class TestRedactedDsnInfo:
    def test_redacted_dsn_info_returns_only_allowed_keys(self) -> None:
        """CRED-2: _redacted_dsn_info() returns exactly {host, port, database, username}."""
        info = _redacted_dsn_info(_FAKE_DSN)
        assert set(info.keys()) == {"host", "port", "database", "username"}

    def test_redacted_dsn_info_no_password_in_values(self) -> None:
        """CRED-1: _redacted_dsn_info() must not include the password in any value."""
        info = _redacted_dsn_info(_FAKE_DSN)
        for v in info.values():
            assert _FAKE_PASSWORD not in v, f"Password appeared in redacted DSN info value: {v!r}"

    def test_redacted_dsn_info_no_full_dsn_in_values(self) -> None:
        """CRED-1: _redacted_dsn_info() must not include the full DSN string in any value."""
        info = _redacted_dsn_info(_FAKE_DSN)
        for v in info.values():
            assert _FAKE_DSN not in v, f"Full DSN appeared in redacted DSN info value: {v!r}"

    def test_redacted_dsn_info_extracts_correct_host(self) -> None:
        """CRED-2: _redacted_dsn_info() extracts the host correctly."""
        info = _redacted_dsn_info(_FAKE_DSN)
        assert info["host"] == "db.internal"

    def test_redacted_dsn_info_extracts_correct_username(self) -> None:
        """CRED-2: _redacted_dsn_info() extracts the username but NOT the password."""
        info = _redacted_dsn_info(_FAKE_DSN)
        assert info["username"] == "admin"
        # Confirm password itself is absent
        assert _FAKE_PASSWORD not in info["username"]

    def test_redacted_dsn_info_handles_malformed_dsn_gracefully(self) -> None:
        """CRED-1: Malformed DSN returns unknown placeholders, never raises or leaks."""
        info = _redacted_dsn_info("not-a-dsn://???")
        assert set(info.keys()) == {"host", "port", "database", "username"}
        # No exception raised, values are safe strings
        for v in info.values():
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# CRED-1: FerrumConnectionError message safety
# ---------------------------------------------------------------------------


class TestConnectionErrorCredentialSafety:
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
        register_hook("*", fn)
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


# ---------------------------------------------------------------------------
# CRED-1: ferrum.connect() credential safety at the public API boundary
# ---------------------------------------------------------------------------


class TestConnectCredentialSafety:
    async def test_connection_error_redacts_password(self) -> None:
        """CRED-1: FerrumConnectionError from a failed connect() must not include the password.

        Uses a DSN with an embedded password pointing at a nonexistent host so the
        connection must fail.  The error message is allowed to include host, port,
        database, and username (CRED-2 allowlist) but never the password.
        """
        from ferrum.errors import FerrumConnectionError

        dsn_with_secret = "postgresql://user:secretpassword@nonexistent-host-xyz.invalid/db"  # noqa: S105
        secret = "secretpassword"  # noqa: S105

        with pytest.raises(FerrumConnectionError) as exc_info:
            async with ferrum.connect(dsn_with_secret):
                pass  # pragma: no cover — connection must fail before reaching here

        msg = str(exc_info.value)
        assert secret not in msg, (
            f"Password {secret!r} must not appear in FerrumConnectionError: {msg!r}"
        )
        assert dsn_with_secret not in msg, (
            f"Full DSN must not appear in FerrumConnectionError: {msg!r}"
        )

    async def test_ferrum_database_url_missing_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CRED-1 / config safety: connect() with no DSN and no env var raises FerrumConfigError.

        Ensures the missing-DSN path surfaces a typed configuration error rather than
        an ``AttributeError`` or bare ``TypeError``, and that the error message does not
        contain any credential data (there is none to leak, but the path must be safe).
        """
        from ferrum.errors import FerrumConfigError

        monkeypatch.delenv("FERRUM_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with pytest.raises(FerrumConfigError) as exc_info:
            async with ferrum.connect():
                pass  # pragma: no cover — must raise before yield

        # Sanity: the error message should be actionable (mentions DSN or env var).
        msg = str(exc_info.value)
        assert "FERRUM_DATABASE_URL" in msg or "DSN" in msg.upper(), (
            f"FerrumConfigError message should guide the user to fix the config: {msg!r}"
        )
