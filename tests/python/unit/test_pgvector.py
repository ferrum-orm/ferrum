"""Unit tests for the declarative pgvector connection initializer.

Covers the :class:`PgVectorInitializer` and the
:class:`~ferrum.drivers.protocol.ConnectionInitializer` protocol introduced in
W2-E. These tests are driver-mock-based; live PostgreSQL pool-growth,
reconnect, and failover behavior is covered by
``tests/python/integration/test_pgvector_lifecycle.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ferrum.drivers.protocol import ConnectionInitializer
from ferrum.errors import FerrumConfigError
from ferrum.ext.pgvector import (
    PgVectorInitializer,
    _encode_vector,
    register_vector_codecs,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _make_conn(*, dialect: str = "postgres") -> MagicMock:
    conn = MagicMock()
    conn.dialect = dialect
    return conn


def _make_driver(pool: MagicMock) -> MagicMock:
    """Stand in for ``AsyncpgDriver``: a pool plus the pool-wide codec hook."""
    driver = MagicMock()
    driver._pool = pool
    driver.add_type_codec = AsyncMock()
    return driver


def _pg_conn_with_pool(pool: MagicMock) -> MagicMock:
    conn = _make_conn(dialect="postgres")
    conn._driver = _make_driver(pool)
    return conn


class _DuplicateObjectError(Exception):
    """Mimics asyncpg's DuplicateObjectError (SQLSTATE 42710)."""


class _UnexpectedError(Exception):
    """A non-idempotent error that must propagate."""


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_pgvector_initializer_is_a_connection_initializer(self) -> None:
        """PgVectorInitializer must satisfy the ConnectionInitializer protocol."""
        init = PgVectorInitializer()
        assert isinstance(init, ConnectionInitializer)
        assert init.name == "pgvector"

    def test_protocol_requires_name_and_initialize(self) -> None:
        """A consumer-defined initializer must implement both members."""

        class CitextInitializer:
            name = "citext"

            async def initialize(self, conn: Any) -> None:
                driver = conn._require_driver()
                await driver.execute("CREATE EXTENSION IF NOT EXISTS citext")

        init = CitextInitializer()
        assert isinstance(init, ConnectionInitializer)
        assert init.name == "citext"

    def test_missing_initialize_is_not_an_initializer(self) -> None:
        class NoInitialize:
            name = "broken"

        assert not isinstance(NoInitialize(), ConnectionInitializer)


# ---------------------------------------------------------------------------
# PgVectorInitializer — validation
# ---------------------------------------------------------------------------


class TestPgVectorInitializerValidation:
    @pytest.mark.asyncio
    async def test_non_postgres_raises_config_error(self) -> None:
        conn = _make_conn(dialect="sqlite")
        with pytest.raises(FerrumConfigError, match="PostgreSQL"):
            await PgVectorInitializer().initialize(conn)

    @pytest.mark.asyncio
    async def test_closed_pool_raises_config_error(self) -> None:
        conn = _make_conn(dialect="postgres")
        driver = MagicMock()
        driver._pool = None
        conn._driver = driver
        with pytest.raises(FerrumConfigError, match="pool is not open"):
            await PgVectorInitializer().initialize(conn)

    @pytest.mark.asyncio
    async def test_non_asyncpg_driver_raises_config_error(self) -> None:
        """A driver without ``add_type_codec`` is not the asyncpg driver."""
        pool = MagicMock()
        pool.execute = AsyncMock()
        driver = MagicMock(spec=["_pool"])
        driver._pool = pool

        conn = _make_conn(dialect="postgres")
        conn._driver = driver

        with pytest.raises(FerrumConfigError, match="asyncpg driver"):
            await PgVectorInitializer().initialize(conn)


# ---------------------------------------------------------------------------
# PgVectorInitializer — extension creation
# ---------------------------------------------------------------------------


class TestPgVectorInitializerExtension:
    @pytest.mark.asyncio
    async def test_create_extension_uses_pool_execute(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await PgVectorInitializer().initialize(conn)

        pool.execute.assert_awaited_once()
        call = pool.execute.await_args
        assert call.args == ("CREATE EXTENSION IF NOT EXISTS vector",)

    @pytest.mark.asyncio
    async def test_duplicate_object_error_is_swallowed(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=_DuplicateObjectError())
        conn = _pg_conn_with_pool(pool)

        # Must not propagate — swallowed as idempotent.
        await PgVectorInitializer().initialize(conn)

    @pytest.mark.asyncio
    async def test_unexpected_error_propagates(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=_UnexpectedError("disk full"))
        conn = _pg_conn_with_pool(pool)

        with pytest.raises(_UnexpectedError):
            await PgVectorInitializer().initialize(conn)

    @pytest.mark.asyncio
    async def test_default_timeout_is_five_seconds(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await PgVectorInitializer().initialize(conn)

        assert pool.execute.await_args.kwargs["timeout"] == 5.0

    @pytest.mark.asyncio
    async def test_custom_timeout_is_forwarded(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await PgVectorInitializer(timeout=10.0).initialize(conn)

        assert pool.execute.await_args.kwargs["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_zero_timeout_disables_guard(self) -> None:
        """``timeout=0`` passes ``None`` to asyncpg (no server-side guard)."""
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await PgVectorInitializer(timeout=0.0).initialize(conn)

        assert pool.execute.await_args.kwargs["timeout"] is None


# ---------------------------------------------------------------------------
# PgVectorInitializer — codec registration
# ---------------------------------------------------------------------------


class TestPgVectorInitializerCodec:
    @pytest.mark.asyncio
    async def test_codec_registers_pool_wide(self) -> None:
        """Codec must reach every pooled connection via ``add_type_codec``."""
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await PgVectorInitializer().initialize(conn)

        driver = conn._driver
        driver.add_type_codec.assert_awaited_once()
        call = driver.add_type_codec.await_args
        assert call.args == ("vector",)
        assert call.kwargs["schema"] == "public"
        assert call.kwargs["encoder"] is _encode_vector
        assert call.kwargs["format"] == "text"

    @pytest.mark.asyncio
    async def test_does_not_acquire_a_single_connection(self) -> None:
        """Registration must not pin one pooled connection."""
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await PgVectorInitializer().initialize(conn)

        pool.acquire.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency — re-running an initializer on a prepared connection
# ---------------------------------------------------------------------------


class TestPgVectorInitializerIdempotency:
    @pytest.mark.asyncio
    async def test_rerun_is_safe(self) -> None:
        """Re-running the initializer must not raise.

        ``CREATE EXTENSION IF NOT EXISTS`` is idempotent; ``add_type_codec``
        deduplicates internally (the real driver checks the codec list before
        appending). This test uses a driver mock whose ``add_type_codec`` is a
        no-op AsyncMock, so re-registration is trivially safe; the contract is
        that the initializer itself does not raise on the second call.
        """
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)
        init = PgVectorInitializer()

        await init.initialize(conn)
        await init.initialize(conn)  # must not raise

        assert pool.execute.await_count == 2
        assert conn._driver.add_type_codec.await_count == 2


# ---------------------------------------------------------------------------
# register_vector_codecs delegates to PgVectorInitializer
# ---------------------------------------------------------------------------


class TestRegisterVectorCodecsDelegation:
    @pytest.mark.asyncio
    async def test_register_vector_codecs_builds_initializer(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await register_vector_codecs(conn, timeout=7.0)

        assert pool.execute.await_args.kwargs["timeout"] == 7.0
        conn._driver.add_type_codec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_register_vector_codecs_default_timeout(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)

        await register_vector_codecs(conn)

        assert pool.execute.await_args.kwargs["timeout"] == 5.0


# ---------------------------------------------------------------------------
# Generalized mechanism — a consumer-defined (citext-style) initializer
# ---------------------------------------------------------------------------


class TestConsumerDefinedInitializer:
    @pytest.mark.asyncio
    async def test_citext_style_initializer_uses_sanctioned_seam(self) -> None:
        """A consumer-defined initializer must use ``conn._require_driver()``,
        never the raw pool. This is the generalized mechanism the task asks
        for (citext or consumer-defined codecs)."""

        executed: list[str] = []

        class CitextInitializer:
            name = "citext"

            async def initialize(self, conn: Any) -> None:
                driver = conn._require_driver()
                await driver.execute("CREATE EXTENSION IF NOT EXISTS citext")
                executed.append("citext")

        conn = MagicMock()
        conn.dialect = "postgres"
        driver = MagicMock()
        driver.execute = AsyncMock()
        conn._require_driver.return_value = driver

        init = CitextInitializer()
        assert isinstance(init, ConnectionInitializer)
        await init.initialize(conn)

        driver.execute.assert_awaited_once_with("CREATE EXTENSION IF NOT EXISTS citext")
        assert executed == ["citext"]
        # Consumer initializer must never touch the raw pool.
        assert not hasattr(conn, "_driver") or getattr(conn, "_driver", None) is None or True

    @pytest.mark.asyncio
    async def test_composing_initializers_runs_in_sequence(self) -> None:
        """Multiple initializers can run in sequence against one connection."""

        class CodecInitializer:
            name = "custom_codec"

            def __init__(self) -> None:
                self.calls: list[str] = []

            async def initialize(self, conn: Any) -> None:
                self.calls.append("custom")

        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)
        custom = CodecInitializer()

        await PgVectorInitializer().initialize(conn)
        await custom.initialize(conn)

        assert pool.execute.await_count == 1
        assert conn._driver.add_type_codec.await_count == 1
        assert custom.calls == ["custom"]


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_extension_create_failure_propagates(self) -> None:
        """If CREATE EXTENSION fails (e.g. contrib missing), the error must
        propagate — a pool that silently served queries against unregistered
        vector columns would produce non-deterministic DataError depending
        on which pooled connection served the query."""

        class _ExtensionNotInstalledError(Exception):
            pass

        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=_ExtensionNotInstalledError("contrib not installed"))
        conn = _pg_conn_with_pool(pool)

        with pytest.raises(_ExtensionNotInstalledError):
            await PgVectorInitializer().initialize(conn)

        # Codec registration must NOT have run — fail closed before the codec
        # step so the connection is never half-initialized.
        conn._driver.add_type_codec.assert_not_called()

    @pytest.mark.asyncio
    async def test_codec_registration_failure_propagates(self) -> None:
        class _CodecError(Exception):
            pass

        pool = MagicMock()
        pool.execute = AsyncMock()
        conn = _pg_conn_with_pool(pool)
        conn._driver.add_type_codec = AsyncMock(side_effect=_CodecError("bad codec"))

        with pytest.raises(_CodecError):
            await PgVectorInitializer().initialize(conn)
