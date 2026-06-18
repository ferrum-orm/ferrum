"""Live database schema introspection for migration planning and drift detection.

Provides two introspection levels:

- ``fetch_existing_tables``: table → [column_names], compatible with
  ``compute_plan(existing_tables=...)``.
- ``fetch_schema_state``: table → {column → {type, nullable, default}}, used by
  drift detection to compare live schema against model metadata.

Security invariants:
- All queries use bound parameters; schema names are never interpolated into
  identifier positions.
- ``ferrum_migrations`` is excluded so the ledger table never appears in model
  diffs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ferrum.connection import Connection

# Tables excluded from schema introspection results so they never surface as
# unexpected extra tables in drift reports or migration plans.
_EXCLUDED_TABLES: frozenset[str] = frozenset({"ferrum_migrations"})


def get_db_identity(conn: Connection) -> str:
    """Return a sanitized connection identity string for token binding (ADR-007).

    Returns ``"host=<h> port=<p> dbname=<d> user=<u>"`` — never the password,
    TLS parameters, or the full DSN (CRED-1 allowlist, §3).
    """
    try:
        dsn: str = getattr(conn, "_dsn", "") or ""
        parsed = urlparse(dsn)
        host = parsed.hostname or ("memory" if ":memory:" in dsn else "unknown")
        if parsed.scheme.startswith("sqlite"):
            port = "0"
        else:
            port = str(parsed.port or (3306 if parsed.scheme.startswith("mysql") else 5432))
        dbname = (parsed.path or "").lstrip("/") or "unknown"
        user = parsed.username or "unknown"
        return f"host={host} port={port} dbname={dbname} user={user}"
    except Exception:
        return "host=unknown port=unknown dbname=unknown user=unknown"


async def _fetch_existing_tables_postgres(
    conn: Connection,
    *,
    schema: str,
) -> dict[str, list[str]]:
    driver = conn._require_driver()
    rows = await driver.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = $1
        ORDER BY table_name, ordinal_position
        """,
        schema,
    )
    tables: dict[str, list[str]] = {}
    for row in rows:
        table = row["table_name"] if isinstance(row, dict) else row[0]
        column = row["column_name"] if isinstance(row, dict) else row[1]
        if table not in _EXCLUDED_TABLES:
            tables.setdefault(table, []).append(column)
    return tables


async def _fetch_existing_tables_mysql(
    conn: Connection,
    *,
    schema: str,
) -> dict[str, list[str]]:
    del schema  # MySQL uses DATABASE() for the active schema.
    driver = conn._require_driver()
    rows = await driver.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
        ORDER BY table_name, ordinal_position
        """
    )
    tables: dict[str, list[str]] = {}
    for row in rows:
        table = row["table_name"] if isinstance(row, dict) else row[0]
        column = row["column_name"] if isinstance(row, dict) else row[1]
        if table not in _EXCLUDED_TABLES:
            tables.setdefault(table, []).append(column)
    return tables


async def _fetch_existing_tables_sqlite(conn: Connection) -> dict[str, list[str]]:
    driver = conn._require_driver()
    table_rows = await driver.fetch(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    tables: dict[str, list[str]] = {}
    for trow in table_rows:
        table = trow["name"] if isinstance(trow, dict) else trow[0]
        if table in _EXCLUDED_TABLES:
            continue
        info_rows = await driver.fetch(f"PRAGMA table_info({table!r})")
        cols: list[str] = []
        for irow in info_rows:
            if isinstance(irow, dict):
                cols.append(irow["name"])
            else:
                cols.append(irow[1])
        tables[table] = cols
    return tables


async def fetch_existing_tables(
    conn: Connection,
    *,
    schema: str = "public",
) -> dict[str, list[str]]:
    """Return table → ordered column name list for all user tables.

    Dispatches per ``conn.dialect``.
    """
    dialect = conn.dialect
    if dialect == "postgres":
        return await _fetch_existing_tables_postgres(conn, schema=schema)
    if dialect == "mysql":
        return await _fetch_existing_tables_mysql(conn, schema=schema)
    if dialect == "sqlite":
        return await _fetch_existing_tables_sqlite(conn)
    raise ValueError(f"Unsupported dialect for introspection: {dialect!r}")


async def _fetch_schema_state_postgres(
    conn: Connection,
    *,
    schema: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    driver = conn._require_driver()
    rows = await driver.fetch(
        """
        SELECT
            table_name,
            column_name,
            data_type,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = $1
        ORDER BY table_name, ordinal_position
        """,
        schema,
    )
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if isinstance(row, dict):
            table, column, data_type, is_nullable, default = (
                row["table_name"],
                row["column_name"],
                row["data_type"],
                row["is_nullable"],
                row["column_default"],
            )
        else:
            table, column, data_type, is_nullable, default = row
        if table in _EXCLUDED_TABLES:
            continue
        tables.setdefault(table, {})[column] = {
            "type": data_type,
            "nullable": is_nullable == "YES",
            "default": default,
        }
    return tables


async def _fetch_schema_state_mysql(conn: Connection) -> dict[str, dict[str, dict[str, Any]]]:
    driver = conn._require_driver()
    rows = await driver.fetch(
        """
        SELECT
            table_name,
            column_name,
            data_type,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
        ORDER BY table_name, ordinal_position
        """
    )
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if isinstance(row, dict):
            table, column, data_type, is_nullable, default = (
                row["table_name"],
                row["column_name"],
                row["data_type"],
                row["is_nullable"],
                row["column_default"],
            )
        else:
            table, column, data_type, is_nullable, default = row
        if table in _EXCLUDED_TABLES:
            continue
        tables.setdefault(table, {})[column] = {
            "type": data_type,
            "nullable": is_nullable == "YES",
            "default": default,
        }
    return tables


async def _fetch_schema_state_sqlite(conn: Connection) -> dict[str, dict[str, dict[str, Any]]]:
    driver = conn._require_driver()
    table_rows = await driver.fetch(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for trow in table_rows:
        table = trow["name"] if isinstance(trow, dict) else trow[0]
        if table in _EXCLUDED_TABLES:
            continue
        info_rows = await driver.fetch(f"PRAGMA table_info({table!r})")
        tables[table] = {}
        for irow in info_rows:
            if isinstance(irow, dict):
                name = irow["name"]
                col_type = irow["type"]
                notnull = irow["notnull"]
                default = irow["dflt_value"]
            else:
                name, col_type, notnull, default = irow[1], irow[2], irow[3], irow[4]
            tables[table][name] = {
                "type": col_type,
                "nullable": not bool(notnull),
                "default": default,
            }
    return tables


async def fetch_schema_state(
    conn: Connection,
    *,
    schema: str = "public",
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return a detailed schema snapshot for drift detection."""
    dialect = conn.dialect
    if dialect == "postgres":
        return await _fetch_schema_state_postgres(conn, schema=schema)
    if dialect == "mysql":
        return await _fetch_schema_state_mysql(conn)
    if dialect == "sqlite":
        return await _fetch_schema_state_sqlite(conn)
    raise ValueError(f"Unsupported dialect for introspection: {dialect!r}")


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


@dataclass
class DriftReport:
    """Result of a schema drift analysis."""

    has_drift: bool
    missing_tables: list[str] = field(default_factory=list)
    unexpected_tables: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    unexpected_columns: dict[str, list[str]] = field(default_factory=dict)
    recovery_guidance: str = ""

    def format_summary(self) -> str:
        """Return a human-readable summary suitable for operator output."""
        if not self.has_drift:
            return "Schema matches expectation — no drift detected."

        lines: list[str] = ["Schema drift detected:"]
        for t in self.missing_tables:
            lines.append(f"  - Table '{t}' is expected but missing from the database.")
        for t in self.unexpected_tables:
            lines.append(f"  - Table '{t}' exists in the database but is not expected.")
        for t, cols in self.missing_columns.items():
            for c in cols:
                lines.append(f"  - Column '{t}.{c}' is expected but missing.")
        for t, cols in self.unexpected_columns.items():
            for c in cols:
                lines.append(f"  - Column '{t}.{c}' exists but is not expected.")
        if self.recovery_guidance:
            lines.append(f"\nRecovery: {self.recovery_guidance}")
        return "\n".join(lines)


def detect_drift(
    existing_tables: dict[str, list[str]],
    expected_tables: dict[str, list[str]],
) -> DriftReport:
    """Compare live schema against expected schema and return a DriftReport."""
    existing_set = set(existing_tables)
    expected_set = set(expected_tables)

    missing_tables = sorted(expected_set - existing_set)
    unexpected_tables = sorted(existing_set - expected_set)

    missing_columns: dict[str, list[str]] = {}
    unexpected_columns: dict[str, list[str]] = {}

    for table in sorted(existing_set & expected_set):
        live_cols = set(existing_tables[table])
        want_cols = set(expected_tables[table])
        miss = sorted(want_cols - live_cols)
        extra = sorted(live_cols - want_cols)
        if miss:
            missing_columns[table] = miss
        if extra:
            unexpected_columns[table] = extra

    has_drift = bool(missing_tables or unexpected_tables or missing_columns or unexpected_columns)

    guidance = ""
    if has_drift:
        if missing_tables:
            guidance = (
                "One or more expected tables are absent.  "
                "A migration may have partially failed before creating them.  "
                "Inspect the database manually, then re-run the failed migration "
                "or apply the missing DDL by hand."
            )
        elif missing_columns:
            guidance = (
                "One or more expected columns are absent.  "
                "A migration may have partially failed after the table was created.  "
                "Inspect the database manually and apply the missing ADD COLUMN "
                "statements before re-running the migration."
            )
        else:
            guidance = (
                "The database contains objects not tracked by Ferrum migrations.  "
                "Verify whether these were created intentionally outside of Ferrum.  "
                "Use 'ferrum inspectdb' to scaffold models if needed."
            )

    return DriftReport(
        has_drift=has_drift,
        missing_tables=missing_tables,
        unexpected_tables=unexpected_tables,
        missing_columns=missing_columns,
        unexpected_columns=unexpected_columns,
        recovery_guidance=guidance,
    )
