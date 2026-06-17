"""Schema drift detection.

``detect_drift`` compares the live PostgreSQL schema against registered model
metadata and returns a ``DriftReport`` describing any divergence.

The report surfaces missing/extra tables and missing/extra columns.  Type-change
classification is out of scope for v0.1 (the migration planner is additive-only).

Security invariants:
- No row data, bound values, or credentials appear in the report.
- The live schema is queried via ``fetch_schema_state`` which uses only
  parameterised queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ferrum.migrations.introspect import fetch_schema_state

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import Model

# Tables always present in a Ferrum database that are not model-owned; excluded
# from "extra tables" reporting.
_SYSTEM_TABLES: frozenset[str] = frozenset({"ferrum_migrations"})


@dataclass
class DriftReport:
    """Result of a schema drift detection pass.

    Attributes:
        has_drift: ``True`` if any divergence was detected.
        missing_tables: Tables defined in models but absent from the live DB.
        extra_tables: Tables present in the DB but not owned by any registered
            model (excluding Ferrum system tables).
        column_diffs: Per-table column divergence keyed by table name.  Each
            entry has ``"missing_columns"`` (in model, absent from DB) and
            ``"extra_columns"`` (in DB, absent from model) sorted lists.
    """

    has_drift: bool = False
    missing_tables: list[str] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)
    column_diffs: dict[str, dict[str, list[str]]] = field(default_factory=dict)


async def detect_drift(
    conn: Connection,
    models: list[type[Model]],
    *,
    schema: str = "public",
) -> DriftReport:
    """Detect schema drift between the live PostgreSQL schema and registered models.

    Compares ``models`` against the live schema introspected from ``conn`` and
    returns a ``DriftReport``.  Returns ``DriftReport(has_drift=False)`` when
    ``models`` is empty or when the schema exactly matches the models.

    Args:
        conn: Open Ferrum connection.
        models: Ferrum ``Model`` subclasses to compare against the live schema.
        schema: PostgreSQL schema to inspect (default ``"public"``).

    Returns:
        ``DriftReport`` describing missing/extra tables and column-level diffs.
        ``has_drift`` is ``True`` whenever any divergence exists.
    """
    live_state = await fetch_schema_state(conn, schema=schema)

    model_table_names: set[str] = {cls.get_metadata().table_name for cls in models}

    missing_tables: list[str] = []
    extra_tables: list[str] = []
    column_diffs: dict[str, dict[str, list[str]]] = {}

    # Tables defined in models but absent from the live DB.
    for table in sorted(model_table_names):
        if table not in live_state:
            missing_tables.append(table)

    # Tables present in DB but not in any registered model.
    for table in sorted(live_state):
        if table not in model_table_names and table not in _SYSTEM_TABLES:
            extra_tables.append(table)

    # Column-level diffs for tables that exist in both models and DB.
    for cls in models:
        meta = cls.get_metadata()
        table = meta.table_name
        if table not in live_state:
            continue  # Already captured as a missing table.

        live_cols: set[str] = set(live_state[table].keys())
        model_cols: set[str] = {f.column_name for f in meta.fields}

        missing_cols = sorted(model_cols - live_cols)
        extra_cols = sorted(live_cols - model_cols)

        if missing_cols or extra_cols:
            column_diffs[table] = {
                "missing_columns": missing_cols,
                "extra_columns": extra_cols,
            }

    has_drift = bool(missing_tables or column_diffs)
    return DriftReport(
        has_drift=has_drift,
        missing_tables=missing_tables,
        extra_tables=extra_tables,
        column_diffs=column_diffs,
    )
