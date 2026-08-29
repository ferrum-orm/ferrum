"""Integration tests for ``Connection.transaction`` against live backends.

Invariants:
- terminals run *inside* a transaction sharing one pinned connection,
- clean exit commits; an exception rolls the whole unit of work back,
- savepoints roll back independently of the enclosing transaction,
- cancellation mid-transaction rolls back and leaves the pool usable,
- isolation modifiers are accepted by the server.

Skipped unless the active backend declares ``TRANSACTIONS`` / ``SAVEPOINTS``.
"""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

import ferrum
from ferrum.errors import FerrumTimeoutError

from .backends import POSTGRES, Backend, Capability
from .schema import Column, transient_table


def _model(table_name: str) -> type[ferrum.Model]:
    class Account(ferrum.Model):
        id: int = 0
        name: str = ""
        balance: int = 0

        class Meta:
            table = table_name

    return Account


def _account_columns() -> list[Column]:
    return [
        Column("id", "pk_serial"),
        Column("name", "text", null=False),
        Column("balance", "int", null=False, default="0"),
    ]


async def _row_count(
    conn: ferrum.connection.Connection,
    backend: Backend,
    table_name: str,
) -> int:
    q = backend.quote
    return int(await conn._require_driver().fetchval(f"SELECT count(*) FROM {q(table_name)}"))


@pytest.mark.integration
async def test_commit_persists_multiple_terminals(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.TRANSACTIONS)

    table = f"ferrum_int_tx_commit_{unique_suffix}"
    model = _model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_account_columns()):
        async with db_conn.transaction() as tx:
            a = await model.objects.create(tx, name="alice", balance=100)
            await model.objects.create(tx, name="bob", balance=50)
            # Visible within the same transaction before commit.
            assert await model.objects.count(tx) == 2
            assert a.id > 0
        # Both rows survive the commit.
        assert await _row_count(db_conn, backend, table) == 2


@pytest.mark.integration
async def test_rollback_on_exception_discards_all(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.TRANSACTIONS)

    table = f"ferrum_int_tx_rollback_{unique_suffix}"
    model = _model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_account_columns()):
        with pytest.raises(RuntimeError, match="boom"):
            async with db_conn.transaction() as tx:
                await model.objects.create(tx, name="alice", balance=100)
                raise RuntimeError("boom")
        assert await _row_count(db_conn, backend, table) == 0


@pytest.mark.integration
async def test_savepoint_rolls_back_independently(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.SAVEPOINTS)

    table = f"ferrum_int_tx_savepoint_{unique_suffix}"
    model = _model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_account_columns()):
        async with db_conn.transaction() as tx:
            await model.objects.create(tx, name="outer", balance=1)
            with pytest.raises(RuntimeError, match="inner"):
                async with tx.savepoint() as sp:
                    await model.objects.create(sp, name="inner", balance=2)
                    raise RuntimeError("inner")
            # Outer insert survives the savepoint rollback.
            assert await model.objects.count(tx) == 1
        rows = await model.objects.all(db_conn)
        assert [r.name for r in rows] == ["outer"]


@pytest.mark.integration
async def test_cancellation_rolls_back_and_pool_usable(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.TRANSACTIONS)

    table = f"ferrum_int_tx_cancel_{unique_suffix}"
    model = _model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_account_columns()):

        async def unit() -> None:
            async with db_conn.transaction() as tx:
                await model.objects.create(tx, name="doomed", balance=1)
                await asyncio.sleep(10)  # cancelled here

        task = asyncio.ensure_future(unit())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Rolled back: no rows, and the pool still works afterwards.
        assert await _row_count(db_conn, backend, table) == 0
        await model.objects.create(db_conn, name="after", balance=9)
        assert await _row_count(db_conn, backend, table) == 1


@pytest.mark.integration
async def test_serializable_isolation_accepted(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.TRANSACTIONS)
    if backend.name != "postgres":
        pytest.skip("serializable isolation level is verified on PostgreSQL only")

    table = f"ferrum_int_tx_iso_{unique_suffix}"
    model = _model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_account_columns()):
        async with db_conn.transaction(isolation="serializable") as tx:
            await model.objects.create(tx, name="iso", balance=1)
        assert await _row_count(db_conn, backend, table) == 1


@pytest.mark.integration
async def test_deadline_rolls_back(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.TRANSACTIONS)

    table = f"ferrum_int_tx_deadline_{unique_suffix}"
    model = _model(table)
    async with transient_table(db_conn, table, backend=backend, columns=_account_columns()):
        with pytest.raises(FerrumTimeoutError):
            async with db_conn.transaction(deadline=0.05) as tx:
                await model.objects.create(tx, name="slow", balance=1)
                await asyncio.sleep(5)
        assert await _row_count(db_conn, backend, table) == 0


# ---------------------------------------------------------------------------
# select_for_update — live PostgreSQL locking (§5a acceptance criterion 4)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_select_for_update_nowait_raises_lock_timeout(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """NOWAIT: a second FOR UPDATE on a locked row raises lock_timeout (55P03)."""
    table = f"ferrum_int_tx_for_update_nowait_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn,
        table,
        backend=POSTGRES,
        columns=_account_columns(),
    ):
        await model.objects.create(pg_conn, name="alice", balance=100)
        # Hold a FOR UPDATE lock in a transaction.
        async with pg_conn.transaction() as tx1:
            rows = await model.objects.filter(name="alice").select_for_update().all(tx1)
            assert len(rows) == 1
            # A second transaction with NOWAIT should raise lock_timeout.
            async with pg_conn.transaction() as tx2:
                with pytest.raises(FerrumTimeoutError) as exc_info:
                    await model.objects.filter(name="alice").select_for_update(nowait=True).all(tx2)
                assert exc_info.value.category == "lock_timeout"


@pytest.mark.integration
async def test_select_for_update_skip_locked(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """SKIP LOCKED: a second FOR UPDATE skips the locked row."""
    table = f"ferrum_int_tx_for_update_skip_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn,
        table,
        backend=POSTGRES,
        columns=_account_columns(),
    ):
        await model.objects.create(pg_conn, name="alice", balance=100)
        await model.objects.create(pg_conn, name="bob", balance=50)
        async with pg_conn.transaction() as tx1:
            # Lock alice.
            rows = await model.objects.filter(name="alice").select_for_update().all(tx1)
            assert len(rows) == 1
            # A second transaction with SKIP LOCKED should get only bob.
            async with pg_conn.transaction() as tx2:
                rows2 = await model.objects.select_for_update(skip_locked=True).all(tx2)
                names = {r.name for r in rows2}
                assert "bob" in names
                assert "alice" not in names


@pytest.mark.integration
async def test_select_for_update_blocks_until_commit(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """FOR UPDATE (no NOWAIT) blocks until the holding transaction commits."""
    table = f"ferrum_int_tx_for_update_block_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn,
        table,
        backend=POSTGRES,
        columns=_account_columns(),
    ):
        await model.objects.create(pg_conn, name="alice", balance=100)

        holder_done = asyncio.Event()

        async def hold_lock() -> None:
            async with pg_conn.transaction() as tx:
                await model.objects.filter(name="alice").select_for_update().all(tx)
                await asyncio.sleep(0.2)
            holder_done.set()

        hold_task = asyncio.ensure_future(hold_lock())
        await asyncio.sleep(0.05)  # let the holder acquire the lock
        # This should block until the holder commits.
        async with pg_conn.transaction() as tx2:
            rows = await model.objects.filter(name="alice").select_for_update().all(tx2)
            assert len(rows) == 1
        await hold_task
        assert holder_done.is_set()


@pytest.mark.integration
async def test_select_for_update_rejected_on_write(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """select_for_update is rejected by write terminals (_check_write_scope)."""
    from ferrum.errors import FerrumCompileError

    table = f"ferrum_int_tx_for_update_reject_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn,
        table,
        backend=POSTGRES,
        columns=_account_columns(),
    ):
        await model.objects.create(pg_conn, name="alice", balance=100)
        with pytest.raises(FerrumCompileError, match="FOR UPDATE"):
            await (
                model.objects.filter(name="alice").select_for_update().update(pg_conn, balance=200)
            )


# ---------------------------------------------------------------------------
# Advisory locks — live PostgreSQL (§5a acceptance criterion 5)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_advisory_xact_lock_exclusive(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    """pg_advisory_xact_lock holds exclusively within a transaction."""
    key = 42
    async with pg_conn.transaction() as tx1:
        await tx1.advisory_xact_lock(key)
        # A second transaction trying the same key should block (or return False
        # with try_lock). Use try_lock to avoid hanging the test.
        async with pg_conn.transaction() as tx2:
            acquired = await tx2.advisory_try_xact_lock(key)
            assert acquired is False
    # After tx1 commits, a new transaction can acquire the lock.
    async with pg_conn.transaction() as tx3:
        acquired = await tx3.advisory_try_xact_lock(key)
        assert acquired is True


@pytest.mark.integration
async def test_advisory_xact_lock_two_part_key(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    """Two-part (int, int) advisory lock keys work."""
    key = (100, 200)
    async with pg_conn.transaction() as tx1:
        await tx1.advisory_xact_lock(key)
        async with pg_conn.transaction() as tx2:
            acquired = await tx2.advisory_try_xact_lock(key)
            assert acquired is False


@pytest.mark.integration
async def test_advisory_session_lock(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
) -> None:
    """Connection.advisory_lock pins a connection and holds a session lock."""
    key = 999
    async with pg_conn.advisory_lock(key) as locked_conn:
        # Inside the lock, we can run queries on the pinned connection.
        result = await locked_conn._require_driver().fetchval("SELECT 1")
        assert result == 1
        # A different connection's transaction cannot acquire the same session lock.
        async with pg_conn.transaction() as tx2:
            acquired = await tx2.advisory_try_xact_lock(key)
            assert acquired is False
    # After the context exits, the lock is released.
    async with pg_conn.transaction() as tx3:
        acquired = await tx3.advisory_try_xact_lock(key)
        assert acquired is True


# ---------------------------------------------------------------------------
# run_transaction — live replay and no-leak (§5a acceptance criterion 3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_run_transaction_no_connection_leak(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    """run_transaction with retries does not leak pooled connections."""

    table = f"ferrum_int_tx_run_tx_leak_{unique_suffix}"
    model = _model(table)
    async with transient_table(
        pg_conn,
        table,
        backend=POSTGRES,
        columns=_account_columns(),
    ):
        stats_before = pg_conn.pool_stats()
        calls = 0

        async def fn(tx: ferrum.Transaction) -> int:
            nonlocal calls
            calls += 1
            await model.objects.create(tx, name=f"row-{calls}", balance=calls)
            return calls

        result = await pg_conn.run_transaction(fn)
        assert result == 1
        assert calls == 1

        stats_after = pg_conn.pool_stats()
        if stats_before is not None and stats_after is not None:
            assert stats_after.acquired == stats_before.acquired
            assert stats_after.inflight == stats_before.inflight
