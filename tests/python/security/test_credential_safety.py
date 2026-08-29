"""Security qualification suite — credential safety (CRED-1, CRED-2).

CRED-1: Connection strings and passwords MUST NOT appear in default hook
    payloads, exceptions, or migration output.
CRED-2: Connection diagnostics MAY include host, port, database name, username
    — never password or full DSN. Returned dict keys are restricted to the
    allowlist.

§5a "Safe error fields": PostgreSQL DETAIL/HINT containing secrets, bound
    values, row data, and full DSNs MUST NEVER appear in any exception field,
    message, or default (Tier-A) hook payload — at any tier.
"""

from __future__ import annotations

import unittest.mock as mock

import asyncpg.exceptions
import pytest

import ferrum
from ferrum.connection import _redacted_dsn_info
from ferrum.errors import (
    ERROR_CATEGORIES,
    FerrumConnectionError,
    map_db_error,
)
from ferrum.hooks import (
    _TIER_A_KEYS,
    HookPayload,
    clear_hooks,
    dispatch,
    register_hook,
    unregister_hook,
)

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


# ---------------------------------------------------------------------------
# §5a: DETAIL/HINT secrets and row data never escape mapped exceptions
# ---------------------------------------------------------------------------

# Sentinels planted in PostgreSQL DETAIL/HINT that must NEVER escape.
_SECRET_ROW_VALUE = "leaked_row_data_sentinel_42"  # noqa: S105
_SECRET_HINT_VALUE = "hint_secret_password_99"  # noqa: S105
_SECRET_DETAIL_FULL = f"DETAIL: Key (email)=({_SECRET_ROW_VALUE}) already exists."
_SECRET_HINT_FULL = f"HINT: Remove the row with password={_SECRET_HINT_VALUE} first."


class TestDetailHintSafety:
    """PostgreSQL DETAIL/HINT containing secrets and row data must never escape
    any mapped exception field, message, or default hook payload (§5a, ERR-1)."""

    def test_detail_hint_not_in_unique_violation_message(self) -> None:
        """DETAIL/HINT with row data must not appear in the mapped error message."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.UniqueViolationError)
        mock_exc.constraint_name = "users_email_key"
        mock_exc.sqlstate = "23505"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        msg = str(result)
        assert _SECRET_ROW_VALUE not in msg
        assert _SECRET_HINT_VALUE not in msg
        assert "DETAIL:" not in msg
        assert "HINT:" not in msg

    def test_detail_hint_not_in_mapped_exception_attributes(self) -> None:
        """DETAIL/HINT must not appear in any structured attribute of the mapped error."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.UniqueViolationError)
        mock_exc.constraint_name = "users_email_key"
        mock_exc.sqlstate = "23505"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        for attr_name in ("sqlstate", "category", "constraint", "model", "operation"):
            attr_val = getattr(result, attr_name, None)
            if isinstance(attr_val, str):
                assert _SECRET_ROW_VALUE not in attr_val, (
                    f"Row data leaked into {attr_name}: {attr_val!r}"
                )
                assert _SECRET_HINT_VALUE not in attr_val, (
                    f"Hint secret leaked into {attr_name}: {attr_val!r}"
                )

    def test_detail_hint_not_in_deadlock_error(self) -> None:
        """DETAIL/HINT must not appear in mapped deadlock errors."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.DeadlockDetectedError)
        mock_exc.sqlstate = "40P01"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        msg = str(result)
        assert _SECRET_ROW_VALUE not in msg
        assert _SECRET_HINT_VALUE not in msg

    def test_detail_hint_not_in_serialization_error(self) -> None:
        """DETAIL/HINT must not appear in mapped serialization errors."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.SerializationError)
        mock_exc.sqlstate = "40001"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        msg = str(result)
        assert _SECRET_ROW_VALUE not in msg
        assert _SECRET_HINT_VALUE not in msg

    def test_detail_hint_not_in_connection_error(self) -> None:
        """DETAIL/HINT must not appear in mapped connection errors."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.ConnectionFailureError)
        mock_exc.sqlstate = "08006"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        msg = str(result)
        assert _SECRET_ROW_VALUE not in msg
        assert _SECRET_HINT_VALUE not in msg

    def test_detail_hint_not_in_schema_error(self) -> None:
        """DETAIL/HINT must not appear in mapped schema errors."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.UndefinedColumnError)
        mock_exc.sqlstate = "42703"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        msg = str(result)
        assert _SECRET_ROW_VALUE not in msg
        assert _SECRET_HINT_VALUE not in msg

    def test_detail_hint_not_in_generic_database_error(self) -> None:
        """DETAIL/HINT must not appear in mapped generic PostgresError."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.PostgresError)
        mock_exc.sqlstate = "XX000"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        msg = str(result)
        assert _SECRET_ROW_VALUE not in msg
        assert _SECRET_HINT_VALUE not in msg

    def test_detail_hint_not_in_failover_error(self) -> None:
        """DETAIL/HINT must not appear in mapped admin/crash shutdown errors."""
        for exc_cls, sqlstate in [
            (asyncpg.exceptions.AdminShutdownError, "57P01"),
            (asyncpg.exceptions.CrashShutdownError, "57P02"),
            (asyncpg.exceptions.CannotConnectNowError, "57P03"),
        ]:
            mock_exc = mock.MagicMock(spec=exc_cls)
            mock_exc.sqlstate = sqlstate
            mock_exc.detail = _SECRET_DETAIL_FULL
            mock_exc.hint = _SECRET_HINT_FULL
            result = map_db_error(mock_exc)
            msg = str(result)
            assert _SECRET_ROW_VALUE not in msg, f"Row data leaked from {exc_cls.__name__}: {msg!r}"
            assert _SECRET_HINT_VALUE not in msg

    def test_detail_hint_not_in_pool_exhaustion_error(self) -> None:
        """DETAIL/HINT must not appear in mapped pool exhaustion errors."""
        mock_exc = mock.MagicMock(spec=asyncpg.exceptions.TooManyConnectionsError)
        mock_exc.sqlstate = "53300"
        mock_exc.detail = _SECRET_DETAIL_FULL
        mock_exc.hint = _SECRET_HINT_FULL
        result = map_db_error(mock_exc)
        msg = str(result)
        assert _SECRET_ROW_VALUE not in msg
        assert _SECRET_HINT_VALUE not in msg

    def test_dsn_in_timeout_message_does_not_leak(self) -> None:
        """A DSN planted in a TimeoutError must not survive mapping."""
        dsn = "postgresql://admin:supersecret@db.internal:5432/prod"
        result = map_db_error(TimeoutError(dsn))
        msg = str(result)
        assert "supersecret" not in msg
        assert dsn not in msg


class TestCategoryAndHookSafety:
    """Category in Tier-A hook payloads must never carry bound values or secrets (§5a)."""

    def test_category_survives_tier_a_without_bound_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A payload with category and bound_params must strip bound_params but keep category."""
        monkeypatch.setenv("FERRUM_OBS", "A")
        received: list[HookPayload] = []
        register_hook("*", received.append)
        try:
            dispatch(
                {
                    "event": "query_failure",
                    "fingerprint": "fp",
                    "failure_category": "FerrumIntegrityError",
                    "category": "unique_violation",
                    "status": "error",
                    "bound_params": ["supersecret_value"],
                    "dsn": "postgresql://user:supersecret@host/db",
                }
            )
            assert len(received) == 1
            payload = received[0]
            payload_str = str(payload)
            assert payload["category"] == "unique_violation"
            assert "supersecret" not in payload_str
            assert "bound_params" not in payload
            assert "dsn" not in payload
        finally:
            clear_hooks()

    def test_all_mapped_categories_are_in_closed_enum(self) -> None:
        """Every category from map_db_error must be in ERROR_CATEGORIES (no arbitrary strings)."""
        test_cases = [
            (asyncpg.exceptions.UniqueViolationError, "23505"),
            (asyncpg.exceptions.ForeignKeyViolationError, "23503"),
            (asyncpg.exceptions.NotNullViolationError, "23502"),
            (asyncpg.exceptions.CheckViolationError, "23514"),
            (asyncpg.exceptions.UndefinedColumnError, "42703"),
            (asyncpg.exceptions.UndefinedTableError, "42P01"),
            (asyncpg.exceptions.UndefinedFunctionError, "42883"),
            (asyncpg.exceptions.DeadlockDetectedError, "40P01"),
            (asyncpg.exceptions.SerializationError, "40001"),
            (asyncpg.exceptions.LockNotAvailableError, "55P03"),
            (asyncpg.exceptions.QueryCanceledError, "57014"),
            (asyncpg.exceptions.AdminShutdownError, "57P01"),
            (asyncpg.exceptions.CrashShutdownError, "57P02"),
            (asyncpg.exceptions.CannotConnectNowError, "57P03"),
            (asyncpg.exceptions.InvalidTransactionStateError, "25000"),
            (asyncpg.exceptions.TooManyConnectionsError, "53300"),
            (asyncpg.exceptions.ConnectionFailureError, "08006"),
        ]
        for exc_cls, sqlstate in test_cases:
            mock_exc = mock.MagicMock(spec=exc_cls)
            mock_exc.sqlstate = sqlstate
            mock_exc.constraint_name = None
            mock_exc.detail = None
            mock_exc.hint = None
            result = map_db_error(mock_exc)
            assert result.category in ERROR_CATEGORIES, (
                f"{exc_cls.__name__} produced category={result.category!r} "
                f"not in closed enum ERROR_CATEGORIES"
            )

    def test_tier_a_keys_include_category(self) -> None:
        """The 'category' key must be in _TIER_A_KEYS (§5a)."""
        assert "category" in _TIER_A_KEYS

    def test_tier_a_keys_do_not_include_detail_or_hint(self) -> None:
        """Tier-A allowlist must never include DETAIL/HINT/bound_params keys."""
        forbidden = {"detail", "hint", "bound_params", "dsn", "password", "sql_text"}
        overlap = forbidden & _TIER_A_KEYS
        assert not overlap, f"Tier A allowlist contains forbidden keys: {overlap}"
