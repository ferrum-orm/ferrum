"""Unit tests for revert/down-migration behavior."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

import ferrum.cli.makemigrations_cmd as makemigrations_cmd
import ferrum.cli.revert_cmd as revert_cmd
from ferrum.migrations.base import Migration
from ferrum.migrations.ledger import delete_applied
from ferrum.migrations.loader import MigrationModule
from ferrum.migrations.operations import (
    AddColumn,
    AddForeignKey,
    AddIndex,
    Column,
    CreateTable,
    DropColumn,
    DropForeignKey,
    DropIndex,
    DropTable,
    Operation,
)


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeDbConn:
    def __init__(self) -> None:
        self.execute = AsyncMock(return_value=None)

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)


class _FakePool:
    def __init__(self, db_conn: _FakeDbConn) -> None:
        self.db_conn = db_conn

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.db_conn)


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    def _require_pool(self) -> _FakePool:
        return self.pool


class _FakeConnect:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _module(
    tmp_path: Path,
    name: str,
    migration: type[Migration],
    *,
    content: str = "content",
) -> MigrationModule:
    path = tmp_path / f"{name}.py"
    path.write_text(content, encoding="utf-8")
    return MigrationModule(name=name, path=path, migration=migration)


def test_migration_reverse_operations_default_is_empty() -> None:
    class BareMigration(Migration):
        operations: ClassVar[list[Operation]] = []

    assert BareMigration.reverse_operations == []


def test_makemigrations_reverse_source_generation_for_reversible_ops() -> None:
    create_table = CreateTable("notes", [Column("id", "BIGSERIAL", primary_key=True)])
    add_column = AddColumn("notes", Column("body", "TEXT"))
    add_index = AddIndex("notes", "idx_notes_body", ["body"])
    add_fk = AddForeignKey("notes", "fk_notes_user_id", "user_id", "users")

    assert makemigrations_cmd._reverse_op_to_source(create_table.to_op_dict()) == (
        "        ops.DropTable('notes'),"
    )
    assert makemigrations_cmd._reverse_op_to_source(add_column.to_op_dict()) == (
        "        ops.DropColumn('notes', 'body'),"
    )
    assert makemigrations_cmd._reverse_op_to_source(add_index.to_op_dict()) == (
        "        ops.DropIndex('idx_notes_body'),"
    )
    assert makemigrations_cmd._reverse_op_to_source(add_fk.to_op_dict()) == (
        "        ops.DropForeignKey('notes', 'fk_notes_user_id'),"
    )


def test_makemigrations_source_generation_for_add_foreign_key() -> None:
    op = AddForeignKey(
        "notes",
        "fk_notes_user_id",
        "user_id",
        "users",
        on_delete="SET NULL",
    )

    assert makemigrations_cmd._op_to_source(op.to_op_dict()) == (
        "        ops.AddForeignKey('notes', 'fk_notes_user_id', "
        "'user_id', 'users', 'id', on_delete='SET NULL'),"
    )


@pytest.mark.asyncio
async def test_delete_applied_calls_parameterized_delete() -> None:
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    conn = MagicMock()
    conn._require_pool.return_value = pool

    await delete_applied(conn, "digest-123")

    pool.execute.assert_awaited_once_with(
        "DELETE FROM ferrum_migrations WHERE digest = $1",
        "digest-123",
    )


@pytest.mark.asyncio
async def test_run_revert_returns_2_when_no_applied_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoopMigration(Migration):
        operations: ClassVar[list[Operation]] = []
        reverse_operations: ClassVar[list[Operation]] = [DropTable("notes")]

    module = _module(tmp_path, "0001_noop", NoopMigration)
    fake_conn = _FakeConn(_FakePool(_FakeDbConn()))

    monkeypatch.setattr(revert_cmd, "connect", lambda: _FakeConnect(fake_conn))
    monkeypatch.setattr(revert_cmd._ledger, "ensure_ledger", AsyncMock(return_value=None))
    monkeypatch.setattr(revert_cmd._loader, "scan", lambda migrations_dir: [module])
    monkeypatch.setattr(revert_cmd._ledger, "is_applied", AsyncMock(return_value=False))

    assert await revert_cmd.run_revert(tmp_path) == 2


@pytest.mark.asyncio
async def test_run_revert_returns_2_for_irreversible_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IrreversibleMigration(Migration):
        operations: ClassVar[list[Operation]] = [CreateTable("notes", [])]
        reverse_operations: ClassVar[list[Operation]] = []

    module = _module(tmp_path, "0001_irreversible", IrreversibleMigration)
    db_conn = _FakeDbConn()
    fake_conn = _FakeConn(_FakePool(db_conn))

    monkeypatch.setattr(revert_cmd, "connect", lambda: _FakeConnect(fake_conn))
    monkeypatch.setattr(revert_cmd._ledger, "ensure_ledger", AsyncMock(return_value=None))
    monkeypatch.setattr(revert_cmd._loader, "scan", lambda migrations_dir: [module])
    monkeypatch.setattr(revert_cmd._ledger, "is_applied", AsyncMock(return_value=True))

    assert await revert_cmd.run_revert(tmp_path) == 2
    db_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_destructive_reverse_operation_requires_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DestructiveReverse(Migration):
        operations: ClassVar[list[Operation]] = [CreateTable("notes", [])]
        reverse_operations: ClassVar[list[Operation]] = [DropTable("notes")]

    module = _module(tmp_path, "0001_destructive", DestructiveReverse)
    db_conn = _FakeDbConn()
    fake_conn = _FakeConn(_FakePool(db_conn))

    monkeypatch.setattr(revert_cmd, "connect", lambda: _FakeConnect(fake_conn))
    monkeypatch.setattr(revert_cmd._ledger, "ensure_ledger", AsyncMock(return_value=None))
    monkeypatch.setattr(revert_cmd._loader, "scan", lambda migrations_dir: [module])
    monkeypatch.setattr(revert_cmd._ledger, "is_applied", AsyncMock(return_value=True))
    delete_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(revert_cmd._ledger, "delete_applied", delete_mock)

    assert await revert_cmd.run_revert(tmp_path, confirm=False) == 2
    db_conn.execute.assert_not_awaited()
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_revert_executes_reverse_ops_and_deletes_ledger_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReversibleMigration(Migration):
        operations: ClassVar[list[Operation]] = [AddColumn("notes", Column("body", "TEXT"))]
        reverse_operations: ClassVar[list[Operation]] = [
            DropColumn("notes", "body"),
            DropIndex("idx_notes_body"),
            DropForeignKey("notes", "fk_notes_user_id"),
        ]

    module = _module(tmp_path, "0002_reversible", ReversibleMigration, content="v2")
    db_conn = _FakeDbConn()
    fake_conn = _FakeConn(_FakePool(db_conn))
    delete_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(revert_cmd, "connect", lambda: _FakeConnect(fake_conn))
    monkeypatch.setattr(revert_cmd._ledger, "ensure_ledger", AsyncMock(return_value=None))
    monkeypatch.setattr(revert_cmd._loader, "scan", lambda migrations_dir: [module])
    monkeypatch.setattr(revert_cmd._ledger, "is_applied", AsyncMock(return_value=True))
    monkeypatch.setattr(revert_cmd._ledger, "delete_applied", delete_mock)

    result = await revert_cmd.run_revert(tmp_path, confirm=True)

    assert result == 0
    assert db_conn.execute.await_count == 3
    executed_sql = [call.args[0] for call in db_conn.execute.await_args_list]
    assert 'ALTER TABLE "notes" DROP COLUMN IF EXISTS "body"' in executed_sql
    assert 'DROP INDEX IF EXISTS "idx_notes_body"' in executed_sql
    assert 'ALTER TABLE "notes" DROP CONSTRAINT IF EXISTS "fk_notes_user_id"' in executed_sql
    delete_mock.assert_awaited_once()
