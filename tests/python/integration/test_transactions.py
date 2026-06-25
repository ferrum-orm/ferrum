"""Integration tests for ``Connection.transaction`` against live PostgreSQL.

Invariants:
- terminals run *inside* a transaction sharing one pinned connection,
- clean exit commits; an exception rolls the whole unit of work back,
- savepoints roll back independently of the enclosing transaction,
- cancellation mid-transaction rolls back and leaves the pool usable,
- isolation modifiers are accepted by the server.

Skipped unless ``FERRUM_TEST_DSN`` is set and the Rust extension is built.
"""

from __future__ import annotations

import asyncio

import pytest

import ferrum
from ferrum.errors import FerrumTimeoutError

from .helpers import raw_pool, transient_table


def _model(table_name: str) -> type[ferrum.Model]:
    class Account(ferrum.Model):
        id: int = 0
        name: str = ""
        balance: int = 0

        class Meta:
            table = table_name

    return Account


_CREATE = """
    CREATE TABLE "{t}" (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0
    )
"""


async def _row_count(pg_conn: ferrum.connection.Connection, table_name: str) -> int:
    pool = raw_pool(pg_conn)
    async with pool.acquire() as raw:
        return await raw.fetchval(f'SELECT count(*) FROM "{table_name}"')  # noqa: S608


@pytest.mark.integration
async def test_commit_persists_multiple_terminals(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_tx_commit_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        async with pg_conn.transaction() as tx:
            a = await model.objects.create(tx, name="alice", balance=100)
            await model.objects.create(tx, name="bob", balance=50)
            # Visible within the same transaction before commit.
            assert await model.objects.count(tx) == 2
            assert a.id > 0
        # Both rows survive the commit.
        assert await _row_count(pg_conn, table) == 2


@pytest.mark.integration
async def test_rollback_on_exception_discards_all(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_tx_rollback_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        with pytest.raises(RuntimeError, match="boom"):
            async with pg_conn.transaction() as tx:
                await model.objects.create(tx, name="alice", balance=100)
                raise RuntimeError("boom")
        assert await _row_count(pg_conn, table) == 0


@pytest.mark.integration
async def test_savepoint_rolls_back_independently(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_tx_savepoint_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        async with pg_conn.transaction() as tx:
            await model.objects.create(tx, name="outer", balance=1)
            with pytest.raises(RuntimeError, match="inner"):
                async with tx.savepoint() as sp:
                    await model.objects.create(sp, name="inner", balance=2)
                    raise RuntimeError("inner")
            # Outer insert survives the savepoint rollback.
            assert await model.objects.count(tx) == 1
        rows = await model.objects.all(pg_conn)
        assert [r.name for r in rows] == ["outer"]


@pytest.mark.integration
async def test_cancellation_rolls_back_and_pool_usable(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_tx_cancel_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):

        async def unit() -> None:
            async with pg_conn.transaction() as tx:
                await model.objects.create(tx, name="doomed", balance=1)
                await asyncio.sleep(10)  # cancelled here

        task = asyncio.ensure_future(unit())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Rolled back: no rows, and the pool still works afterwards.
        assert await _row_count(pg_conn, table) == 0
        await model.objects.create(pg_conn, name="after", balance=9)
        assert await _row_count(pg_conn, table) == 1


@pytest.mark.integration
async def test_serializable_isolation_accepted(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_tx_iso_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        async with pg_conn.transaction(isolation="serializable") as tx:
            await model.objects.create(tx, name="iso", balance=1)
        assert await _row_count(pg_conn, table) == 1


@pytest.mark.integration
async def test_deadline_rolls_back(
    pg_conn: ferrum.connection.Connection, require_native: None, unique_suffix: str
) -> None:
    table = f"ferrum_int_tx_deadline_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn, create_sql=_CREATE.format(t=table), drop_sql=f'DROP TABLE "{table}"'
    ):
        with pytest.raises(FerrumTimeoutError):
            async with pg_conn.transaction(deadline=0.05) as tx:
                await model.objects.create(tx, name="slow", balance=1)
                await asyncio.sleep(5)
        assert await _row_count(pg_conn, table) == 0
