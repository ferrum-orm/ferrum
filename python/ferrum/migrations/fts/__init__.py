"""Per-dialect full-text index DDL builders (Wave 0 stubs)."""

from __future__ import annotations

from typing import Any


def op_to_sql(op: dict[str, Any], *, dialect: str) -> str:
    """Dispatch a full-text migration op dict to the dialect-specific builder."""
    kind = op.get("kind", "")
    if kind == "create_full_text_index":
        if dialect == "postgres":
            from ferrum.migrations.fts import postgres as backend

            return backend.create_full_text_index(op)
        if dialect == "mysql":
            from ferrum.migrations.fts import mysql as backend

            return backend.create_full_text_index(op)
        if dialect == "sqlite":
            from ferrum.migrations.fts import sqlite as backend

            return backend.create_full_text_index(op)
        if dialect == "mssql":
            from ferrum.migrations.fts import mssql as backend

            return backend.create_full_text_index(op)
        msg = f"Unsupported dialect for FTS migration: {dialect!r}"
        raise ValueError(msg)
    if kind == "drop_full_text_index":
        if dialect == "postgres":
            from ferrum.migrations.fts import postgres as backend

            return backend.drop_full_text_index(op)
        if dialect == "mysql":
            from ferrum.migrations.fts import mysql as backend

            return backend.drop_full_text_index(op)
        if dialect == "sqlite":
            from ferrum.migrations.fts import sqlite as backend

            return backend.drop_full_text_index(op)
        if dialect == "mssql":
            from ferrum.migrations.fts import mssql as backend

            return backend.drop_full_text_index(op)
        msg = f"Unsupported dialect for FTS migration: {dialect!r}"
        raise ValueError(msg)
    if kind == "create_full_text_catalog":
        if dialect == "mssql":
            from ferrum.migrations.fts import mssql as backend

            return backend.create_full_text_catalog(op)
        msg = f"create_full_text_catalog is only supported on mssql, not {dialect!r}"
        raise ValueError(msg)
    msg = f"Unknown FTS migration op kind: {kind!r}"
    raise ValueError(msg)
