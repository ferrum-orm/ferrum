"""Unit tests for Ferrum FastAPI contrib helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from ferrum.connection import Connection
from ferrum.contrib.fastapi import get_ferrum_conn


@pytest.mark.asyncio
async def test_get_ferrum_conn_returns_app_state_connection() -> None:
    app = FastAPI()
    conn = MagicMock(spec=Connection)
    app.state.ferrum_conn = conn

    request = MagicMock()
    request.app = app

    assert await get_ferrum_conn(request) is conn


@pytest.mark.asyncio
async def test_get_ferrum_conn_raises_when_uninitialized() -> None:
    app = FastAPI()
    request = MagicMock()
    request.app = app

    with pytest.raises(RuntimeError, match="not initialized"):
        await get_ferrum_conn(request)


@pytest.mark.asyncio
async def test_ferrum_lifespan_yields_connection() -> None:
    from contextlib import asynccontextmanager

    from ferrum.contrib.fastapi import ferrum_lifespan

    mock_conn = MagicMock(spec=Connection)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    @asynccontextmanager
    async def fake_connect(
        database_url: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ):
        calls.append(((database_url,), {"min_size": min_size, "max_size": max_size}))
        yield mock_conn

    with patch("ferrum.connection.connect", fake_connect):
        async with ferrum_lifespan("postgresql://user@localhost/db") as conn:
            assert conn is mock_conn

    assert calls == [
        (("postgresql://user@localhost/db",), {"min_size": 1, "max_size": 10}),
    ]
