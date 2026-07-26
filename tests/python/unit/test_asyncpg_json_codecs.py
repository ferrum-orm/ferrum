"""Unit tests for asyncpg json/jsonb codec helpers."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ferrum.drivers.postgres import (
    AsyncpgDriver,
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
