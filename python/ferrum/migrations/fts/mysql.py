"""MySQL full-text index DDL."""

from __future__ import annotations

from typing import Any


def create_full_text_index(op: dict[str, Any]) -> str:
    table = op["table"]
    name = op["name"]
    columns = op.get("columns", [])
    if not columns:
        msg = "create_full_text_index requires at least one column"
        raise ValueError(msg)
    cols = ", ".join(f"`{c}`" for c in columns)
    return f"ALTER TABLE `{table}` ADD FULLTEXT INDEX `{name}` ({cols})"


def drop_full_text_index(op: dict[str, Any]) -> str:
    table = op["table"]
    name = op["name"]
    return f"DROP INDEX `{name}` ON `{table}`"
