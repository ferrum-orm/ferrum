"""Unit tests for ``Connection.transaction`` and the ``Transaction`` facade.

These run without a live database or the Rust extension by injecting a fake
driver. They cover the orchestration contract:

- the transaction commits on clean exit and rolls back on exception,
- the yielded ``Transaction`` exposes the ``dialect`` / ``_require_driver`` surface
  that QuerySet terminals depend on (so a Transaction is interchangeable with a
  Connection),
- savepoints nest on the same pinned connection,
- the isolation level is validated against an allowlist before reaching the driver,
- a driver without transaction support fails with a clear Ferrum error,
- a ``deadline`` rolls back and raises ``FerrumTimeoutError``.

Live commit/rollback visibility against PostgreSQL is covered by the integration
suite (``tests/python/integration/test_transactions.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from ferrum.connection import Connection, Transaction
from ferrum.errors import FerrumConfigError, FerrumTimeoutError


class _FakeBound:
    """Stand-in for the pinned execution surface inside a transaction."""

    dialect = "postgres"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, sql: str, *params: object) -> list[Any]:
        self.calls.append(("fetch", sql))
        return []

    async def fetchrow(self, sql: str, *params: object) -> Any | None:
        self.calls.append(("fetchrow", sql))
        return None

    async def fetchval(self, sql: str, *params: object) -> Any:
        self.calls.append(("fetchval", sql))
        return None

    async def execute(self, sql: str, *params: object) -> str:
        self.calls.append(("execute", sql))
        return "OK"

    @contextlib.asynccontextmanager
    async def savepoint(self) -> AsyncGenerator[_FakeBound, None]:
        nested = _FakeBound()
        self.committed_savepoint = False
        try:
            yield nested
        except BaseException:
            self.rolled_back_savepoint = True
            raise
        else:
            self.committed_savepoint = True


class _FakeDriver:
    """Driver double recording commit/rollback and the isolation it was given."""

    dialect = "postgres"

    def __init__(self) -> None:
        self.bound = _FakeBound()
        self.committed = False
        self.rolled_back = False
        self.isolation: str | None = None
        self.readonly: bool | None = None
        self.deferrable: bool | None = None

    @contextlib.asynccontextmanager
    async def transaction(
        self,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> AsyncGenerator[_FakeBound, None]:
        self.isolation = isolation
        self.readonly = readonly
        self.deferrable = deferrable
        try:
            yield self.bound
        except BaseException:
            self.rolled_back = True
            raise
        else:
            self.committed = True


class _NoTxDriver:
    """Driver lacking transaction support (e.g. a non-pg driver in v0.1)."""

    dialect = "mysql"


def _conn_with(driver: Any) -> Connection:
    conn = Connection("postgresql://u@localhost/db")
    conn._driver = driver  # white-box: skip real pool open()
    return conn


async def test_commit_on_clean_exit() -> None:
    driver = _FakeDriver()
    conn = _conn_with(driver)
    async with conn.transaction() as tx:
        assert isinstance(tx, Transaction)
        assert tx.dialect == "postgres"
        assert tx._require_driver() is driver.bound
    assert driver.committed is True
    assert driver.rolled_back is False


async def test_rollback_on_exception() -> None:
    driver = _FakeDriver()
    conn = _conn_with(driver)
    with pytest.raises(ValueError, match="boom"):
        async with conn.transaction():
            raise ValueError("boom")
    assert driver.rolled_back is True
    assert driver.committed is False


async def test_transaction_passes_modifiers_to_driver() -> None:
    driver = _FakeDriver()
    conn = _conn_with(driver)
    async with conn.transaction(isolation="serializable", readonly=True, deferrable=True):
        pass
    assert driver.isolation == "serializable"
    assert driver.readonly is True
    assert driver.deferrable is True


@pytest.mark.parametrize("bad", ["SERIALIZABLE", "snapshot", "", "read committed"])
async def test_unknown_isolation_rejected(bad: str) -> None:
    driver = _FakeDriver()
    conn = _conn_with(driver)
    with pytest.raises(FerrumConfigError, match="isolation level"):
        async with conn.transaction(isolation=bad):
            pass
    # Rejected before the driver was ever entered.
    assert driver.isolation is None
    assert driver.committed is False


async def test_driver_without_transaction_support_errors() -> None:
    conn = _conn_with(_NoTxDriver())
    with pytest.raises(FerrumConfigError, match="does not support transactions"):
        async with conn.transaction():
            pass


async def test_savepoint_yields_nested_transaction() -> None:
    driver = _FakeDriver()
    conn = _conn_with(driver)
    async with conn.transaction() as tx, tx.savepoint() as sp:
        assert isinstance(sp, Transaction)
        assert sp.dialect == "postgres"
        # Nested savepoint runs on its own bound surface, not the outer one.
        assert sp._require_driver() is not tx._require_driver()
    assert driver.committed is True


async def test_deadline_rolls_back_and_raises_timeout() -> None:
    driver = _FakeDriver()
    conn = _conn_with(driver)
    with pytest.raises(FerrumTimeoutError, match="deadline"):
        async with conn.transaction(deadline=0.01):
            await asyncio.sleep(1.0)
    assert driver.rolled_back is True
    assert driver.committed is False
