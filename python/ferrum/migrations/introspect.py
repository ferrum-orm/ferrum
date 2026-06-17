"""Live PostgreSQL schema introspection for migration planning and drift detection.

Provides two introspection levels:

- ``fetch_existing_tables``: table → [column_names], compatible with
  ``compute_plan(existing_tables=...)``.
- ``fetch_schema_state``: table → {column → {type, nullable, default}}, used by
  drift detection to compare live schema against model metadata.

Security invariants:
- All queries use parameterised ``$1`` placeholders.  The schema name is the
  only variable and is never interpolated into an identifier position.
- ``ferrum_migrations`` is excluded so the ledger table never appears in model
  diffs.
- No user-supplied values reach SQL; only the schema name is variable.
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

    The identity string is embedded in confirmation tokens so that a token
    generated against one database cannot authorize an apply against a different
    database (prevents cross-environment token replay).

    Falls back to ``"host=unknown …"`` when the DSN cannot be parsed.
    """
    try:
        dsn: str = getattr(conn, "_dsn", "") or ""
        parsed = urlparse(dsn)
        host = parsed.hostname or "unknown"
        port = str(parsed.port or 5432)
        dbname = (parsed.path or "").lstrip("/") or "unknown"
        user = parsed.username or "unknown"
        return f"host={host} port={port} dbname={dbname} user={user}"
    except Exception:
        return "host=unknown port=unknown dbname=unknown user=unknown"


async def fetch_existing_tables(
    conn: Connection,
    *,
    schema: str = "public",
) -> dict[str, list[str]]:
    """Return table → ordered column name list for all user tables in ``schema``.

    Suitable as the ``existing_tables`` argument to ``compute_plan()``.

    Args:
        conn: Open Ferrum connection.
        schema: PostgreSQL schema to introspect (default ``"public"``).

    Returns:
        Dict mapping each table name to the ordered list of its column names.
        Tables in ``_EXCLUDED_TABLES`` are omitted.
    """
    pool = conn._require_pool()
    rows = await pool.fetch(
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
        table = row["table_name"]
        if table not in _EXCLUDED_TABLES:
            tables.setdefault(table, []).append(row["column_name"])
    return tables


async def fetch_schema_state(
    conn: Connection,
    *,
    schema: str = "public",
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return a detailed schema snapshot for drift detection.

    Returns table → {column_name → {type, nullable, default}}.

    Args:
        conn: Open Ferrum connection.
        schema: PostgreSQL schema to introspect (default ``"public"``).

    Returns:
        Nested dict: table name → column name → column metadata dict with keys
        ``type`` (PostgreSQL ``data_type``), ``nullable`` (bool), and
        ``default`` (``column_default`` string or ``None``).
        Tables in ``_EXCLUDED_TABLES`` are omitted.
    """
    pool = conn._require_pool()
    rows = await pool.fetch(
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
        table = row["table_name"]
        if table in _EXCLUDED_TABLES:
            continue
        if table not in tables:
            tables[table] = {}
        tables[table][row["column_name"]] = {
            "type": row["data_type"],
            "nullable": row["is_nullable"] == "YES",
            "default": row["column_default"],
        }
    return tables


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


@dataclass
class DriftReport:
    """Result of a schema drift analysis.

    Produced by :func:`detect_drift`.  Used to inform operators whether a
    failed migration mutated the database and what recovery action is expected.
    """

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
    """Compare live schema against expected schema and return a DriftReport.

    This is a pure function — no I/O.  Pass the result of
    :func:`fetch_existing_tables` as ``existing_tables`` and a dict derived
    from model metadata (or a prior migration plan) as ``expected_tables``.

    Column-level drift is only checked for tables present in both snapshots;
    a missing table implies all its columns are also missing.

    Args:
        existing_tables: Mapping of table_name → column_names from live DB.
        expected_tables: Mapping of table_name → column_names from metadata.

    Returns:
        :class:`DriftReport` describing any discrepancies and guidance.
    """
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
