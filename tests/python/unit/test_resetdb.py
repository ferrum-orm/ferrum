"""Unit tests for the resetdb CLI implementation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import ferrum.cli.resetdb_cmd as resetdb_cmd
from ferrum.errors import FerrumConfigError
from ferrum.migrations.ledger import LEDGER_TABLE


class _FakeConn:
    def __init__(self, pool: AsyncMock) -> None:
        self.pool = pool

    def _require_pool(self) -> AsyncMock:
        return self.pool


class _FakeConnect:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FailingConnect:
    async def __aenter__(self) -> _FakeConn:
        raise FerrumConfigError("missing FERRUM_DATABASE_URL")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_run_resetdb_without_confirm_returns_2_and_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_mock = AsyncMock()
    monkeypatch.setattr(resetdb_cmd, "_get_all_model_table_names", lambda: ["users"])
    monkeypatch.setattr(resetdb_cmd, "connect", connect_mock)

    result = await resetdb_cmd.run_resetdb(confirm=False)

    assert result == 2
    connect_mock.assert_not_called()


@pytest.mark.asyncio
async def test_successful_reset_drops_registered_tables_with_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    fake_conn = _FakeConn(pool)

    monkeypatch.setattr(
        resetdb_cmd,
        "_get_all_model_table_names",
        lambda: ["user account", "order_item"],
    )
    monkeypatch.setattr(resetdb_cmd, "connect", lambda: _FakeConnect(fake_conn))

    result = await resetdb_cmd.run_resetdb(confirm=True)

    assert result == 0
    executed_sql = [call.args[0] for call in pool.execute.await_args_list]
    assert 'DROP TABLE IF EXISTS "user account" CASCADE' in executed_sql
    assert 'DROP TABLE IF EXISTS "order_item" CASCADE' in executed_sql


@pytest.mark.asyncio
async def test_successful_reset_truncates_migration_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    fake_conn = _FakeConn(pool)

    monkeypatch.setattr(resetdb_cmd, "_get_all_model_table_names", lambda: [])
    monkeypatch.setattr(resetdb_cmd, "connect", lambda: _FakeConnect(fake_conn))

    result = await resetdb_cmd.run_resetdb(confirm=True)

    assert result == 0
    pool.execute.assert_awaited_once_with(f"TRUNCATE {LEDGER_TABLE}")


@pytest.mark.asyncio
async def test_run_resetdb_config_error_returns_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resetdb_cmd, "_get_all_model_table_names", lambda: ["users"])
    monkeypatch.setattr(resetdb_cmd, "connect", lambda: _FailingConnect())

    assert await resetdb_cmd.run_resetdb(confirm=True) == 2
