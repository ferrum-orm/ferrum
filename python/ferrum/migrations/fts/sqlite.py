"""SQLite FTS5 virtual-table DDL with external-content sync triggers."""

from __future__ import annotations

from typing import Any


def create_full_text_index(op: dict[str, Any]) -> str:
    """Create FTS5 virtual table + INSERT/UPDATE/DELETE sync triggers."""
    table = op["table"]
    name = op["name"]
    columns = op.get("columns", [])
    content = op.get("sqlite_content_table") or table
    if not columns:
        msg = "create_full_text_index requires at least one column"
        raise ValueError(msg)
    col_list = ", ".join(columns)
    stmts = [
        (
            f'CREATE VIRTUAL TABLE IF NOT EXISTS "{name}" USING fts5('
            f'{col_list}, content="{content}", content_rowid=rowid)'
        ),
        (
            f'CREATE TRIGGER IF NOT EXISTS "{name}_ai" AFTER INSERT ON "{content}" BEGIN '
            f'INSERT INTO "{name}"(rowid, {col_list}) VALUES (new.rowid, '
            + ", ".join(f'new."{c}"' for c in columns)
            + "); END"
        ),
        (
            f'CREATE TRIGGER IF NOT EXISTS "{name}_ad" AFTER DELETE ON "{content}" BEGIN '
            f'INSERT INTO "{name}"("{name}", rowid, {col_list}) '
            f"VALUES('delete', old.rowid, " + ", ".join(f'old."{c}"' for c in columns) + "); END"
        ),
        (
            f'CREATE TRIGGER IF NOT EXISTS "{name}_au" AFTER UPDATE ON "{content}" BEGIN '
            f'INSERT INTO "{name}"("{name}", rowid, {col_list}) '
            f"VALUES('delete', old.rowid, " + ", ".join(f'old."{c}"' for c in columns) + "); "
            f'INSERT INTO "{name}"(rowid, {col_list}) VALUES (new.rowid, '
            + ", ".join(f'new."{c}"' for c in columns)
            + "); END"
        ),
    ]
    return ";\n".join(stmts)


def drop_full_text_index(op: dict[str, Any]) -> str:
    name = op["name"]
    stmts = [
        f'DROP TRIGGER IF EXISTS "{name}_au"',
        f'DROP TRIGGER IF EXISTS "{name}_ad"',
        f'DROP TRIGGER IF EXISTS "{name}_ai"',
        f'DROP TABLE IF EXISTS "{name}"',
    ]
    return ";\n".join(stmts)
