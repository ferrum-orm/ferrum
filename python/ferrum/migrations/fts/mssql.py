"""SQL Server full-text catalog and index DDL."""

from __future__ import annotations

from typing import Any


def create_full_text_catalog(op: dict[str, Any]) -> str:
    name = op["name"]
    return f"CREATE FULLTEXT CATALOG [{name}]"


def create_full_text_index(op: dict[str, Any]) -> str:
    table = op["table"]
    catalog = op.get("catalog") or "default_catalog"
    columns = op.get("columns", [])
    pk = op.get("pk_column") or f"PK_{table}"
    if not columns:
        msg = "create_full_text_index requires at least one column"
        raise ValueError(msg)
    cols = ", ".join(f"[{c}]" for c in columns)
    return (
        f"CREATE FULLTEXT INDEX ON [{table}] ({cols}) "
        f"KEY INDEX [{pk}] ON [{catalog}]"
        f" WITH CHANGE_TRACKING AUTO"
    )


def drop_full_text_index(op: dict[str, Any]) -> str:
    table = op["table"]
    return f"DROP FULLTEXT INDEX ON [{table}]"
