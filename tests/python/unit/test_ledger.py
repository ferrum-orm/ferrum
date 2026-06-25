"""Unit tests for migration ledger (ferrum.migrations.ledger).

Invariants covered:
- compute_digest returns a consistent sha256 hex string.
- ensure_ledger calls pool.execute with CREATE_LEDGER_SQL.
- record_applied calls pool.execute with the correct INSERT params.
- record_applied wraps asyncpg UniqueViolationError → FerrumMigrationError.
- is_applied returns True when fetchrow finds a row.
- is_applied returns False when fetchrow returns None.

All asyncpg calls are mocked; no database is required.
"""

from __future__ import annotations

import hashlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.ledger import (
    _create_ledger_sql,
    compute_digest,
    ensure_ledger,
    find_applied_digest_by_name,
    is_applied,
    record_applied,
    verify_checksum,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(*, pool: object | None = None, dialect: str = "postgres") -> MagicMock:
    """Return a mock Connection whose _require_driver returns pool."""
    conn = MagicMock()
    conn.dialect = dialect
    conn._require_driver.return_value = pool or MagicMock()
    return conn


class _FakeUniqueViolationError(Exception):
    """Stand-in for asyncpg.exceptions.UniqueViolationError in patched tests."""


def _fake_asyncpg_exc_module() -> MagicMock:
    mod = MagicMock()
    mod.UniqueViolationError = _FakeUniqueViolationError
    return mod


# ---------------------------------------------------------------------------
# compute_digest
# ---------------------------------------------------------------------------


class TestComputeDigest:
    def test_returns_64_char_lowercase_hex(self) -> None:
        digest = compute_digest("0001_create_note", "content here")
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (
            f"Expected 64-char lowercase hex, got: {digest!r}"
        )

    def test_is_stable_for_same_inputs(self) -> None:
        name = "0001_create_note"
        content = "class Migration: pass"
        assert compute_digest(name, content) == compute_digest(name, content)

    def test_different_names_produce_different_digests(self) -> None:
        content = "class Migration: pass"
        assert compute_digest("0001_a", content) != compute_digest("0002_a", content)

    def test_different_content_produces_different_digest(self) -> None:
        name = "0001_create_note"
        assert compute_digest(name, "v1") != compute_digest(name, "v2")

    def test_matches_manual_sha256(self) -> None:
        name = "0001_create_note"
        content = "some migration content"
        expected = hashlib.sha256(f"{name}:{content}".encode()).hexdigest()
        assert compute_digest(name, content) == expected

    def test_digest_does_not_contain_raw_content(self) -> None:
        sensitive = "secret_marker_value_xyz"
        digest = compute_digest("migration_name", sensitive)
        assert sensitive not in digest


# ---------------------------------------------------------------------------
# ensure_ledger
# ---------------------------------------------------------------------------


class TestEnsureLedger:
    @pytest.mark.asyncio
    async def test_calls_execute_with_create_ledger_sql(self) -> None:
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value=None)
        conn = _make_conn(pool=pool)

        await ensure_ledger(conn)

        pool.execute.assert_awaited_once_with(_create_ledger_sql("postgres"))

    @pytest.mark.asyncio
    async def test_calls_require_driver(self) -> None:
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value=None)
        conn = _make_conn(pool=pool)

        await ensure_ledger(conn)

        conn._require_driver.assert_called_once()


# ---------------------------------------------------------------------------
# record_applied
# ---------------------------------------------------------------------------


class TestRecordApplied:
    @pytest.mark.asyncio
    async def test_calls_execute_with_correct_insert_params(self) -> None:
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value=None)
        conn = _make_conn(pool=pool)

        await record_applied(
            conn, "abc123", environment="production", description="0001_create_note"
        )

        pool.execute.assert_awaited_once_with(
            "INSERT INTO ferrum_migrations (digest, environment, description) VALUES ($1, $2, $3)",
            "abc123",
            "production",
            "0001_create_note",
        )

    @pytest.mark.asyncio
    async def test_default_environment_is_development(self) -> None:
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value=None)
        conn = _make_conn(pool=pool)

        await record_applied(conn, "digest_x")

        _call_args = pool.execute.await_args
        # positional args to execute: SQL, digest, environment, description
        assert _call_args.args[2] == "development"

    @pytest.mark.asyncio
    async def test_default_description_is_empty_string(self) -> None:
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value=None)
        conn = _make_conn(pool=pool)

        await record_applied(conn, "digest_x")

        _call_args = pool.execute.await_args
        assert _call_args.args[3] == ""

    @pytest.mark.asyncio
    async def test_unique_violation_wrapped_as_ferrum_migration_error(self) -> None:
        """UniqueViolationError from asyncpg must map to FerrumMigrationError (MIG-8)."""
        pool = AsyncMock()
        pool.execute = AsyncMock(side_effect=_FakeUniqueViolationError("duplicate key"))
        conn = _make_conn(pool=pool)

        fake_exc_mod = _fake_asyncpg_exc_module()

        with (
            patch("ferrum.migrations.ledger._HAS_ASYNCPG", True),
            patch("ferrum.migrations.ledger._asyncpg_exc", fake_exc_mod),
            pytest.raises(FerrumMigrationError, match="already been applied"),
        ):
            await record_applied(conn, "abc123", description="0001_create_note")

    @pytest.mark.asyncio
    async def test_ferrum_migration_error_does_not_contain_credentials(self) -> None:
        """Error message for a replay must not echo the digest value (CRED-1)."""
        pool = AsyncMock()
        pool.execute = AsyncMock(side_effect=_FakeUniqueViolationError("dupe"))
        conn = _make_conn(pool=pool)

        secret_digest = "super_secret_digest_value_never_leak"  # noqa: S105
        fake_exc_mod = _fake_asyncpg_exc_module()

        with (
            patch("ferrum.migrations.ledger._HAS_ASYNCPG", True),
            patch("ferrum.migrations.ledger._asyncpg_exc", fake_exc_mod),
            pytest.raises(FerrumMigrationError) as exc_info,
        ):
            await record_applied(conn, secret_digest)

        # Digest value must not appear verbatim in error message (CRED-1).
        assert secret_digest not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_other_exceptions_are_re_raised_unchanged(self) -> None:
        """Non-UniqueViolation exceptions bubble up without wrapping."""
        pool = AsyncMock()
        pool.execute = AsyncMock(side_effect=RuntimeError("unexpected DB error"))
        conn = _make_conn(pool=pool)

        with pytest.raises(RuntimeError, match="unexpected DB error"):
            await record_applied(conn, "digest_x")


# ---------------------------------------------------------------------------
# is_applied
# ---------------------------------------------------------------------------


class TestIsApplied:
    @pytest.mark.asyncio
    async def test_returns_true_when_row_exists(self) -> None:
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"1": 1})
        conn = _make_conn(pool=pool)

        result = await is_applied(conn, "abc123")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_row(self) -> None:
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        conn = _make_conn(pool=pool)

        result = await is_applied(conn, "abc123")

        assert result is False

    @pytest.mark.asyncio
    async def test_calls_fetchrow_with_correct_query_and_digest(self) -> None:
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        conn = _make_conn(pool=pool)

        await is_applied(conn, "target_digest")

        pool.fetchrow.assert_awaited_once_with(
            "SELECT 1 FROM ferrum_migrations WHERE digest = $1",
            "target_digest",
        )


class TestChecksumHelpers:
    @pytest.mark.asyncio
    async def test_find_applied_digest_by_name_returns_value(self) -> None:
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"digest": "abc"})
        conn = _make_conn(pool=pool)

        result = await find_applied_digest_by_name(conn, "0001_init")

        assert result == "abc"

    @pytest.mark.asyncio
    async def test_verify_checksum_raises_on_mismatch(self) -> None:
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"digest": "old"})
        conn = _make_conn(pool=pool)

        with pytest.raises(FerrumMigrationError, match="checksum mismatch"):
            await verify_checksum(conn, "0001_init", "new")
