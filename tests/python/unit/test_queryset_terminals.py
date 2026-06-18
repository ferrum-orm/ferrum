"""Unit tests for QuerySet terminal methods and Connection env-var detection.

These tests run without the native Rust extension built, verifying:
- All terminal methods raise ``FerrumConfigError`` when the extension is absent.
- ``ferrum.connect()`` / ``Connection`` pick up ``FERRUM_DATABASE_URL``.
- ``FerrumConfigError`` is exported from the public namespace.
- Error codes are present on exception classes.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.errors import (
    FerrumConfigError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
)
from ferrum.queryset import QuerySet, _decode_bound_param

# ---------------------------------------------------------------------------
# Fixture models
# ---------------------------------------------------------------------------


class Post(ferrum.Model):
    id: int = 0
    title: str = ""
    published: bool = False


# ---------------------------------------------------------------------------
# FerrumConfigError — extension not built
# ---------------------------------------------------------------------------
# When the native extension is absent (_native_ext is None), every terminal
# method that needs compilation must raise FerrumConfigError, not ImportError
# or NotImplementedError.  The unit-test environment never has the wheel built,
# so these tests verify the guard branch directly.


class TestTerminalsRaiseConfigErrorWithoutExtension:
    """All four terminals guard against a missing extension before any I/O.

    The native extension may or may not be present in any given test
    environment, so we explicitly patch ``_native_ext`` to ``None`` to
    exercise the "extension not built" code path in isolation.
    """

    @pytest.mark.asyncio
    async def test_all_raises_ferrum_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.all(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_first_raises_ferrum_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.first(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_get_raises_ferrum_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.get(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_count_raises_ferrum_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.count(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_config_error_is_ferrum_error_subclass(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post)
        with patch("ferrum.queryset._native_ext", None), pytest.raises(ferrum.FerrumError):
            await qs.all(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_all_with_filter_raises_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post).filter(published=True)
        with patch("ferrum.queryset._native_ext", None), pytest.raises(FerrumConfigError):
            await qs.all(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_create_raises_ferrum_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.create(None, id=1, title="hello")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_delete_with_filter_raises_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post).filter(published=True)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.delete(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_update_with_filter_raises_config_error(self) -> None:
        qs: QuerySet[Post] = QuerySet(Post).filter(published=True)
        with (
            patch("ferrum.queryset._native_ext", None),
            pytest.raises(FerrumConfigError, match="maturin develop"),
        ):
            await qs.update(None, title="updated")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FerrumConfigError is in the public namespace
# ---------------------------------------------------------------------------


class TestPublicNamespaceExports:
    def test_ferrum_config_error_exported(self) -> None:
        assert hasattr(ferrum, "FerrumConfigError")
        assert ferrum.FerrumConfigError is FerrumConfigError

    def test_ferrum_multiple_objects_error_exported(self) -> None:
        assert hasattr(ferrum, "FerrumMultipleObjectsError")
        assert ferrum.FerrumMultipleObjectsError is FerrumMultipleObjectsError

    def test_ferrum_not_found_error_exported(self) -> None:
        assert hasattr(ferrum, "FerrumNotFoundError")
        assert ferrum.FerrumNotFoundError is FerrumNotFoundError


# ---------------------------------------------------------------------------
# FERR-XXXX codes on exception classes (DX blocker B-6)
# ---------------------------------------------------------------------------


class TestErrorCodes:
    def test_config_error_code(self) -> None:
        assert FerrumConfigError.code == "FERR-C001"

    def test_compile_error_code(self) -> None:
        assert ferrum.FerrumCompileError.code == "FERR-C102"

    def test_not_found_error_code(self) -> None:
        assert FerrumNotFoundError.code == "FERR-Q404"

    def test_multiple_objects_error_code(self) -> None:
        assert FerrumMultipleObjectsError.code == "FERR-Q405"

    def test_connection_error_code(self) -> None:
        assert ferrum.FerrumConnectionError.code == "FERR-E101"

    def test_danger_api_error_code(self) -> None:
        from ferrum.errors import FerrumDangerApiError

        assert FerrumDangerApiError.code == "FERR-U301"

    def test_all_codes_start_with_ferr(self) -> None:
        import ferrum.errors as errs

        classes = [
            errs.FerrumConfigError,
            errs.FerrumCompileError,
            errs.FerrumNotFoundError,
            errs.FerrumMultipleObjectsError,
            errs.FerrumIntegrityError,
            errs.FerrumConnectionError,
            errs.FerrumTimeoutError,
            errs.FerrumInternalError,
            errs.FerrumMigrationError,
            errs.FerrumDangerApiError,
        ]
        for cls in classes:
            assert cls.code.startswith("FERR-"), f"{cls.__name__}.code = {cls.code!r}"


# ---------------------------------------------------------------------------
# FERRUM_DATABASE_URL env-var detection (DX blocker B-5)
# ---------------------------------------------------------------------------


class TestFERRUM_DATABASE_URL:  # noqa: N801
    def test_connection_uses_env_var(self) -> None:
        dsn = "postgresql://user:pass@localhost/testdb"
        with patch.dict(os.environ, {"FERRUM_DATABASE_URL": dsn}):
            conn = Connection()
        assert conn._dsn == dsn

    def test_explicit_dsn_takes_precedence_over_env(self) -> None:
        explicit = "postgresql://explicit@host/db"
        env_dsn = "postgresql://env@host/db"
        with patch.dict(os.environ, {"FERRUM_DATABASE_URL": env_dsn}):
            conn = Connection(explicit)
        assert conn._dsn == explicit

    def test_missing_dsn_and_env_raises_config_error(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "FERRUM_DATABASE_URL"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(FerrumConfigError, match="FERRUM_DATABASE_URL"),
        ):
            Connection()

    def test_config_error_message_contains_env_var_name(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "FERRUM_DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(FerrumConfigError) as exc_info:
            Connection()
        assert "FERRUM_DATABASE_URL" in str(exc_info.value)

    def test_config_error_does_not_contain_dsn_value(self) -> None:
        """Error message must never include the DSN value (CRED-1)."""
        env = {k: v for k, v in os.environ.items() if k != "FERRUM_DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(FerrumConfigError) as exc_info:
            Connection()
        assert "postgresql://" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# _decode_bound_param round-trip
# ---------------------------------------------------------------------------


class TestDecodeBoundParam:
    def test_null(self) -> None:
        import json

        assert _decode_bound_param(json.dumps({"type": "null"})) is None

    def test_bool_true(self) -> None:
        import json

        assert _decode_bound_param(json.dumps({"type": "bool", "value": True})) is True

    def test_bool_false(self) -> None:
        import json

        assert _decode_bound_param(json.dumps({"type": "bool", "value": False})) is False

    def test_int(self) -> None:
        import json

        assert _decode_bound_param(json.dumps({"type": "int", "value": 42})) == 42

    def test_float(self) -> None:
        import json

        val = _decode_bound_param(json.dumps({"type": "float", "value": 3.14}))
        assert abs(float(val) - 3.14) < 1e-9  # type: ignore[arg-type]

    def test_text(self) -> None:
        import json

        assert _decode_bound_param(json.dumps({"type": "text", "value": "hello"})) == "hello"

    def test_bytes(self) -> None:
        import json

        encoded = json.dumps({"type": "bytes", "value": [1, 2, 3]})
        assert _decode_bound_param(encoded) == b"\x01\x02\x03"

    def test_datetime_returns_datetime_object(self) -> None:
        import json
        from datetime import datetime

        iso = "2024-06-01T12:00:00"
        result = _decode_bound_param(json.dumps({"type": "datetime", "value": iso}))
        assert isinstance(result, datetime)
        assert result == datetime(2024, 6, 1, 12, 0, 0)

    def test_round_trip_int(self) -> None:
        import json

        from ferrum.queryset import _encode_bind_value

        original = 99
        encoded = _encode_bind_value(original)
        decoded = _decode_bound_param(json.dumps(encoded))
        assert decoded == original

    def test_round_trip_text(self) -> None:
        import json

        from ferrum.queryset import _encode_bind_value

        original = "test@example.com"
        encoded = _encode_bind_value(original)
        decoded = _decode_bound_param(json.dumps(encoded))
        assert decoded == original


# ---------------------------------------------------------------------------
# B-1: status-string row-count parsing must not raise ValueError
# ---------------------------------------------------------------------------


class TestWritePathStatusStringParsing:
    """Row-count parsing must fall back to 0 on non-numeric status tokens."""

    @pytest.mark.asyncio
    async def test_delete_non_numeric_status_returns_zero(self) -> None:
        import json

        qs = QuerySet(Post).filter(published=True)
        mock_ext = MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "DELETE FROM post WHERE published = $1",
            "bound_params": [json.dumps({"type": "bool", "value": True})],
            "fingerprint": "fp1",
            "operation": "delete",
        }
        mock_conn = MagicMock()
        mock_conn.dialect = "postgres"
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(return_value="DELETE OK")
        mock_conn._require_driver.return_value = mock_pool
        with patch("ferrum.queryset._native_ext", mock_ext):
            result = await qs.delete(mock_conn)
        assert result == 0

    @pytest.mark.asyncio
    async def test_update_non_numeric_status_returns_zero(self) -> None:
        import json

        qs = QuerySet(Post).filter(published=True)
        mock_ext = MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "UPDATE post SET title = $1 WHERE published = $2",
            "bound_params": [
                json.dumps({"type": "text", "value": "x"}),
                json.dumps({"type": "bool", "value": True}),
            ],
            "fingerprint": "fp2",
            "operation": "update",
        }
        mock_conn = MagicMock()
        mock_conn.dialect = "postgres"
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(return_value="UPDATE OK")
        mock_conn._require_driver.return_value = mock_pool
        with patch("ferrum.queryset._native_ext", mock_ext):
            result = await qs.update(mock_conn, title="x")
        assert result == 0


# ---------------------------------------------------------------------------
# B-2: hook dispatch on write path
# ---------------------------------------------------------------------------


class TestWritePathHookDispatch:
    """query_start / query_success / query_failure fire on write terminals."""

    @pytest.mark.asyncio
    async def test_delete_dispatches_query_start_and_success(self) -> None:
        import json

        qs = QuerySet(Post).filter(published=True)
        mock_ext = MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "DELETE FROM post WHERE published = $1",
            "bound_params": [json.dumps({"type": "bool", "value": True})],
            "fingerprint": "test-fp",
            "operation": "delete",
        }
        mock_conn = MagicMock()
        mock_conn.dialect = "postgres"
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(return_value="DELETE 2")
        mock_conn._require_driver.return_value = mock_pool

        dispatched: list[dict] = []

        from ferrum.hooks import clear_hooks, register_hook

        def capture(payload: dict) -> None:
            dispatched.append(payload)

        register_hook("*", capture)
        try:
            with patch("ferrum.queryset._native_ext", mock_ext):
                count = await qs.delete(mock_conn)
        finally:
            clear_hooks()

        assert count == 2
        events = [p.get("event") for p in dispatched]
        assert "query_start" in events
        assert "query_success" in events
        assert "query_failure" not in events

    @pytest.mark.asyncio
    async def test_update_dispatches_query_failure_on_db_error(self) -> None:
        import json

        import asyncpg

        qs = QuerySet(Post).filter(published=True)
        mock_ext = MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "UPDATE post SET title = $1 WHERE published = $2",
            "bound_params": [
                json.dumps({"type": "text", "value": "x"}),
                json.dumps({"type": "bool", "value": True}),
            ],
            "fingerprint": "test-fp2",
            "operation": "update",
        }
        mock_conn = MagicMock()
        mock_conn.dialect = "postgres"
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(
            side_effect=asyncpg.exceptions.PostgresConnectionError("connection lost")
        )
        mock_conn._require_driver.return_value = mock_pool

        dispatched: list[dict] = []
        from ferrum.hooks import clear_hooks, register_hook

        def capture(payload: dict) -> None:
            dispatched.append(payload)

        register_hook("*", capture)
        try:
            with patch("ferrum.queryset._native_ext", mock_ext), pytest.raises(ferrum.FerrumError):
                await qs.update(mock_conn, title="x")
        finally:
            clear_hooks()

        events = [p.get("event") for p in dispatched]
        assert "query_start" in events
        assert "query_failure" in events
        assert "query_success" not in events

    @pytest.mark.asyncio
    async def test_write_path_hook_payload_tier_a_only(self) -> None:
        """Hook payloads on write path must not contain bound values (LOG-1)."""
        import json

        qs = QuerySet(Post).filter(published=True)
        mock_ext = MagicMock()
        mock_ext.compile_query.return_value = {
            "sql_text": "DELETE FROM post WHERE published = $1",
            "bound_params": [json.dumps({"type": "text", "value": "supersecretvalue"})],
            "fingerprint": "fp-secret",
            "operation": "delete",
        }
        mock_conn = MagicMock()
        mock_conn.dialect = "postgres"
        mock_pool = AsyncMock()
        mock_pool.execute = AsyncMock(return_value="DELETE 1")
        mock_conn._require_driver.return_value = mock_pool

        dispatched: list[dict] = []
        from ferrum.hooks import clear_hooks, register_hook

        register_hook("*", dispatched.append)
        try:
            with patch("ferrum.queryset._native_ext", mock_ext):
                await qs.delete(mock_conn)
        finally:
            clear_hooks()

        for payload in dispatched:
            payload_str = str(payload)
            assert "supersecretvalue" not in payload_str
