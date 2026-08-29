"""Focused unit tests for SQLite driver result and cancellation cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ferrum.drivers.sqlite import AiosqliteDriver


def _driver_with_conn() -> tuple[AiosqliteDriver, MagicMock]:
    driver = AiosqliteDriver("sqlite:///:memory:")
    conn = MagicMock()
    conn.interrupt = AsyncMock()
    conn.rollback = AsyncMock()
    conn.close = AsyncMock()
    driver._conn = conn
    return driver, conn


@pytest.mark.asyncio
async def test_execute_drains_returning_rows_before_commit() -> None:
    driver, conn = _driver_with_conn()
    events: list[str] = []
    cursor = MagicMock()
    cursor.description = (("id",),)
    cursor.rowcount = 1

    async def fetchall() -> list[tuple[int]]:
        events.append("fetchall")
        return [(1,)]

    async def close() -> None:
        events.append("close")

    async def commit() -> None:
        events.append("commit")

    cursor.fetchall = fetchall
    cursor.close = close
    conn.execute = AsyncMock(return_value=cursor)
    conn.commit = commit

    result = await driver.execute('UPDATE "items" SET "active" = ? RETURNING *', True)

    assert result == "UPDATE 1"
    assert events == ["fetchall", "close", "commit"]


@pytest.mark.asyncio
async def test_fetch_cancellation_interrupts_then_rolls_back_and_reraises() -> None:
    driver, conn = _driver_with_conn()
    events: list[str] = []
    cursor = MagicMock()

    async def fetchall() -> list[object]:
        raise asyncio.CancelledError

    async def interrupt() -> None:
        events.append("interrupt")

    async def close() -> None:
        events.append("close")

    async def rollback() -> None:
        events.append("rollback")

    cursor.fetchall = fetchall
    cursor.close = close
    conn.execute = AsyncMock(return_value=cursor)
    conn.interrupt = interrupt
    conn.rollback = rollback

    with pytest.raises(asyncio.CancelledError):
        await driver.fetch("SELECT expensive_operation()")

    assert events == ["interrupt", "close", "rollback"]
    assert driver._conn is conn


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["fetch", "fetchrow", "execute"])
async def test_cancellation_during_execute_cleans_connection(method_name: str) -> None:
    driver, conn = _driver_with_conn()
    conn.execute = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await getattr(driver, method_name)("SELECT 1")

    conn.interrupt.assert_awaited_once()
    conn.rollback.assert_awaited_once()
