"""Unit tests for asyncpg json/jsonb codec helpers and pool configuration."""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ferrum.drivers.postgres import (
    AsyncpgDriver,
    PoolStats,
    _decode_json_param,
    _encode_json_param,
    _register_json_codecs,
)


def test_encode_json_param_passes_through_pre_serialized_strings() -> None:
    raw = '{"a":1}'
    assert _encode_json_param(raw) is raw


def test_encode_json_param_dumps_native_objects() -> None:
    assert json.loads(_encode_json_param({"a": 1, "b": [True]})) == {"a": 1, "b": [True]}


def test_decode_json_param_returns_native_objects() -> None:
    assert _decode_json_param('{"ok":true,"n":2}') == {"ok": True, "n": 2}
    assert _decode_json_param("[1,2]") == [1, 2]


@pytest.mark.asyncio
async def test_register_json_codecs_registers_json_and_jsonb() -> None:
    conn = AsyncMock()
    await _register_json_codecs(conn)
    registered = [call.args[0] for call in conn.set_type_codec.await_args_list]
    assert registered == ["jsonb", "json"]
    for call in conn.set_type_codec.await_args_list:
        assert call.kwargs["schema"] == "pg_catalog"
        assert call.kwargs["format"] == "text"
        assert call.kwargs["encoder"] is _encode_json_param
        assert call.kwargs["decoder"] is _decode_json_param


@pytest.mark.asyncio
async def test_asyncpg_driver_open_registers_json_codecs_via_pool_init() -> None:
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> MagicMock:
        captured["dsn"] = dsn
        captured["init"] = kwargs.get("init")
        return MagicMock(name="pool")

    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = fake_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver("postgresql://u@localhost/db", statement_timeout_ms=1500)
        await driver.open()
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous

    assert captured["dsn"] == "postgresql://u@localhost/db"
    init = captured["init"]
    assert init is not None

    conn = AsyncMock()
    await init(conn)
    conn.execute.assert_awaited_once_with("SET statement_timeout = 1500")
    assert [call.args[0] for call in conn.set_type_codec.await_args_list] == [
        "jsonb",
        "json",
    ]


async def _open_driver_with_fake_pool(pool: MagicMock) -> tuple[AsyncpgDriver, dict[str, Any]]:
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> MagicMock:
        captured["init"] = kwargs.get("init")
        return pool

    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = fake_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver("postgresql://u@localhost/db")
        await driver.open()
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous
    return driver, captured


@pytest.mark.asyncio
async def test_add_type_codec_applies_to_every_pooled_connection() -> None:
    """asyncpg exposes ``set_type_codec`` per connection, not per pool.

    Registering through the pool ``init`` hook (and expiring live connections)
    is what keeps a custom type decoding uniformly regardless of which pooled
    connection serves a query.
    """
    pool = MagicMock(name="pool")
    pool.expire_connections = AsyncMock()
    driver, captured = await _open_driver_with_fake_pool(pool)

    def encoder(value: Any) -> str:
        return str(value)

    def decoder(value: str) -> str:
        return value

    await driver.add_type_codec("vector", schema="public", encoder=encoder, decoder=decoder)
    pool.expire_connections.assert_awaited_once()

    conn = AsyncMock()
    await captured["init"](conn)
    registered = [
        (call.args[0] if call.args else call.kwargs["typename"])
        for call in conn.set_type_codec.await_args_list
    ]
    assert registered == ["jsonb", "json", "vector"]
    vector_call = conn.set_type_codec.await_args_list[-1]
    assert vector_call.kwargs["schema"] == "public"
    assert vector_call.kwargs["encoder"] is encoder
    assert vector_call.kwargs["decoder"] is decoder
    assert vector_call.kwargs["format"] == "text"


@pytest.mark.asyncio
async def test_add_type_codec_is_idempotent() -> None:
    pool = MagicMock(name="pool")
    pool.expire_connections = AsyncMock()
    driver, captured = await _open_driver_with_fake_pool(pool)

    for _ in range(2):
        await driver.add_type_codec(
            "vector",
            schema="public",
            encoder=str,
            decoder=str,
        )

    pool.expire_connections.assert_awaited_once()
    conn = AsyncMock()
    await captured["init"](conn)
    assert conn.set_type_codec.await_count == 3  # jsonb, json, vector


@pytest.mark.asyncio
async def test_driver_open_passes_new_config_to_create_pool() -> None:
    """New pool config knobs are wired to ``asyncpg.create_pool`` kwargs."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock(name="pool")

    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = fake_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver(
            "postgresql://u@localhost/db",
            max_idle_lifetime=60.0,
            max_connection_age=300.0,
            command_timeout=10.0,
            statement_cache_size=100,
            ssl="require",
            server_settings={"work_mem": "64MB"},
            application_name="ferrum-test",
        )
        await driver.open()
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous

    assert captured["max_inactive_connection_lifetime"] == 60.0
    assert captured["command_timeout"] == 10.0
    assert captured["statement_cache_size"] == 100
    assert captured["ssl"] == "require"
    server_settings = captured["server_settings"]
    assert server_settings["work_mem"] == "64MB"
    assert server_settings["application_name"] == "ferrum-test"


@pytest.mark.asyncio
async def test_max_lifetime_maps_to_max_idle_lifetime() -> None:
    """Legacy ``max_lifetime`` is an alias for ``max_idle_lifetime``."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock(name="pool")

    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = fake_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver("postgresql://u@localhost/db", max_lifetime=42.0)
        await driver.open()
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous

    assert captured["max_inactive_connection_lifetime"] == 42.0


@pytest.mark.asyncio
async def test_max_idle_lifetime_takes_precedence_over_max_lifetime() -> None:
    """When both are provided, ``max_idle_lifetime`` wins."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock(name="pool")

    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = fake_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver(
            "postgresql://u@localhost/db",
            max_lifetime=42.0,
            max_idle_lifetime=99.0,
        )
        await driver.open()
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous

    assert captured["max_inactive_connection_lifetime"] == 99.0


@pytest.mark.asyncio
async def test_application_name_folded_into_server_settings() -> None:
    """``application_name`` is set via ``server_settings`` (asyncpg's mechanism)."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock(name="pool")

    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = fake_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver(
            "postgresql://u@localhost/db",
            application_name="my-app",
        )
        await driver.open()
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous

    server_settings = captured.get("server_settings", {})
    assert server_settings.get("application_name") == "my-app"


@pytest.mark.asyncio
async def test_application_name_does_not_override_explicit_server_settings() -> None:
    """Explicit ``server_settings['application_name']`` takes precedence."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock(name="pool")

    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = fake_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver(
            "postgresql://u@localhost/db",
            server_settings={"application_name": "explicit"},
            application_name="ignored",
        )
        await driver.open()
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous

    server_settings = captured.get("server_settings", {})
    assert server_settings.get("application_name") == "explicit"


def test_pool_stats_dataclass_has_required_fields() -> None:
    """PoolStats has all required fields per acceptance criterion 5."""
    stats = PoolStats(
        size=5,
        idle=3,
        acquired=2,
        waiters=1,
        min_size=1,
        max_size=10,
        inflight=2,
        accepting=True,
        closing=False,
    )
    assert stats.size == 5
    assert stats.idle == 3
    assert stats.acquired == 2
    assert stats.waiters == 1
    assert stats.min_size == 1
    assert stats.max_size == 10
    assert stats.inflight == 2
    assert stats.accepting is True
    assert stats.closing is False


def test_pool_stats_is_frozen() -> None:
    """PoolStats is a frozen dataclass (immutable snapshot)."""
    stats = PoolStats(
        size=0,
        idle=0,
        acquired=0,
        waiters=-1,
        min_size=1,
        max_size=10,
        inflight=0,
        accepting=False,
        closing=True,
    )
    with pytest.raises(AttributeError):
        stats.size = 99  # type: ignore[misc]


def test_pool_stats_when_pool_not_open() -> None:
    """Driver returns a zeroed PoolStats when the pool is not open."""
    driver = AsyncpgDriver("postgresql://u@localhost/db")
    stats = driver.pool_stats()
    assert stats.size == 0
    assert stats.idle == 0
    assert stats.acquired == 0
    assert stats.waiters == -1
    assert stats.accepting is False
    assert stats.closing is True


@pytest.mark.asyncio
async def test_pool_stats_reads_asyncpg_internals() -> None:
    """PoolStats reads asyncpg pool internal attributes defensively."""
    fake_holder = MagicMock()
    fake_holder._in_use = True

    fake_holder2 = MagicMock()
    fake_holder2._in_use = False

    fake_pool = MagicMock()
    fake_pool._holders = [fake_holder, fake_holder2]
    fake_pool._closing = False

    driver = AsyncpgDriver("postgresql://u@localhost/db")
    driver._pool = fake_pool

    stats = driver.pool_stats(inflight=3, accepting=True)
    assert stats.size == 2
    assert stats.acquired == 1
    assert stats.idle == 1
    assert stats.inflight == 3
    assert stats.accepting is True
    assert stats.closing is False


@pytest.mark.asyncio
async def test_pool_stats_reports_closing_state() -> None:
    """PoolStats reflects the pool's closing state."""
    fake_pool = MagicMock()
    fake_pool._holders = []
    fake_pool._closing = True

    driver = AsyncpgDriver("postgresql://u@localhost/db")
    driver._pool = fake_pool

    stats = driver.pool_stats(accepting=True)
    assert stats.closing is True
    assert stats.accepting is False  # accepting AND not closing


@pytest.mark.asyncio
async def test_acquire_cm_passes_timeout_to_pool_acquire() -> None:
    """``_acquire_cm`` passes ``acquire_timeout`` to ``pool.acquire``."""
    fake_pool = MagicMock()
    fake_acquire = MagicMock()
    fake_pool.acquire.return_value = fake_acquire

    driver = AsyncpgDriver("postgresql://u@localhost/db", acquire_timeout=5.0)
    driver._pool = fake_pool

    driver._acquire_cm()
    fake_pool.acquire.assert_called_once_with(timeout=5.0)


@pytest.mark.asyncio
async def test_acquire_cm_without_timeout_uses_plain_acquire() -> None:
    """When ``acquire_timeout`` is None, ``pool.acquire()`` is called without timeout."""
    fake_pool = MagicMock()
    fake_pool.acquire.return_value = MagicMock()

    driver = AsyncpgDriver("postgresql://u@localhost/db")
    driver._pool = fake_pool

    driver._acquire_cm()
    fake_pool.acquire.assert_called_once_with()


def _make_fake_pool(raw_conn: Any) -> MagicMock:
    """Create a fake asyncpg pool whose ``acquire`` yields ``raw_conn``."""
    fake_pool = MagicMock()

    @contextlib.asynccontextmanager
    async def _acquire_ctx(*, timeout: float | None = None) -> Any:
        yield raw_conn

    fake_pool.acquire = _acquire_ctx
    return fake_pool


@pytest.mark.asyncio
async def test_fetch_uses_acquire_with_timeout() -> None:
    """Convenience ``fetch`` acquires with timeout, not ``pool.fetch`` directly."""
    fake_raw = AsyncMock()
    fake_raw.fetch.return_value = [{"ok": 1}]

    fake_pool = _make_fake_pool(fake_raw)

    driver = AsyncpgDriver("postgresql://u@localhost/db", acquire_timeout=3.0)
    driver._pool = fake_pool

    result = await driver.fetch("SELECT 1")
    assert len(result) == 1
    fake_raw.fetch.assert_awaited_once_with("SELECT 1")


@pytest.mark.asyncio
async def test_execute_uses_acquire_with_timeout() -> None:
    """Convenience ``execute`` acquires with timeout, not ``pool.execute`` directly."""
    fake_raw = AsyncMock()
    fake_raw.execute.return_value = "OK"

    fake_pool = _make_fake_pool(fake_raw)

    driver = AsyncpgDriver("postgresql://u@localhost/db", acquire_timeout=3.0)
    driver._pool = fake_pool

    result = await driver.execute("INSERT INTO t VALUES (1)")
    assert result == "OK"
    fake_raw.execute.assert_awaited_once_with("INSERT INTO t VALUES (1)")


@pytest.mark.asyncio
async def test_failover_error_triggers_expire_connections() -> None:
    """A failover-category error causes ``expire_connections`` to be called."""
    from ferrum.errors import FerrumConnectionError

    fake_raw = AsyncMock()
    fake_raw.fetchval.side_effect = Exception("server shutdown")

    fake_pool = _make_fake_pool(fake_raw)
    fake_pool.expire_connections = AsyncMock()

    driver = AsyncpgDriver("postgresql://u@localhost/db")
    driver._pool = fake_pool

    # Mock map_db_error to return a failover-category error.
    import ferrum.drivers.postgres as pg_mod

    original_map = pg_mod.map_db_error

    def fake_map(exc: Exception) -> FerrumConnectionError:
        return FerrumConnectionError(
            "Server shutdown. [FERR-E101]",
            category="failover",
        )

    pg_mod.map_db_error = fake_map
    try:
        with pytest.raises(FerrumConnectionError):
            await driver.fetchval("SELECT 1")
        fake_pool.expire_connections.assert_awaited_once()
    finally:
        pg_mod.map_db_error = original_map


@pytest.mark.asyncio
async def test_non_failover_error_does_not_expire() -> None:
    """A non-failover error does NOT call ``expire_connections``."""
    from ferrum.errors import FerrumError

    fake_raw = AsyncMock()
    fake_raw.fetchval.side_effect = Exception("syntax error")

    fake_pool = _make_fake_pool(fake_raw)
    fake_pool.expire_connections = AsyncMock()

    driver = AsyncpgDriver("postgresql://u@localhost/db")
    driver._pool = fake_pool

    with pytest.raises(FerrumError):
        await driver.fetchval("SELECT 1")
    fake_pool.expire_connections.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_open_failure_includes_category() -> None:
    """``FerrumConnectionError`` on pool open failure has ``category='connection'``."""
    fake_asyncpg = MagicMock()

    async def failing_create_pool(dsn: str, **kwargs: Any) -> Any:
        raise OSError("Connection refused")

    fake_asyncpg.create_pool = failing_create_pool
    previous = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = fake_asyncpg
    try:
        driver = AsyncpgDriver("postgresql://user:secret@badhost:5432/db")
        with pytest.raises(Exception) as exc_info:
            await driver.open()
        # The error should have category='connection'.
        assert hasattr(exc_info.value, "category")
        assert exc_info.value.category == "connection"
        # The password must NOT appear in the error message.
        assert "secret" not in str(exc_info.value)
    finally:
        if previous is None:
            sys.modules.pop("asyncpg", None)
        else:
            sys.modules["asyncpg"] = previous
