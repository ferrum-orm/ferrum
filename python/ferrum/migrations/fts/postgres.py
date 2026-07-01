"""PostgreSQL full-text index DDL."""

from __future__ import annotations

from typing import Any


def create_full_text_index(op: dict[str, Any]) -> str:
    """Create a GIN index on ``to_tsvector(config, col1 || ' ' || col2 ...)``."""
    table = op["table"]
    name = op["name"]
    columns = op.get("columns", [])
    config = op.get("config") or "english"
    if not columns:
        msg = "create_full_text_index requires at least one column"
        raise ValueError(msg)
    if not config.replace("_", "").isalnum():
        msg = f"Invalid FTS config identifier: {config!r}"
        raise ValueError(msg)
    col_exprs = " || ' ' || ".join(f'"{c}"' for c in columns)
    expr = f"to_tsvector('{config}', coalesce({col_exprs}, ''))"
    return f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" USING gin ({expr})'


def drop_full_text_index(op: dict[str, Any]) -> str:
    name = op["name"]
    return f'DROP INDEX IF EXISTS "{name}"'
