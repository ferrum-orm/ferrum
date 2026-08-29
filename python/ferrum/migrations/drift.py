"""Read-only PostgreSQL schema-fidelity comparison.

Numbered SQL migrations remain authoritative. This module only compares
selected registered model metadata with ``information_schema`` /
``pg_catalog`` and never emits or applies DDL.

Security invariants:
- All introspection queries use ``$1`` bound parameters for the schema name;
  identifiers are never interpolated from user input.
- ``ferrum_migrations`` and the configured unmanaged tables (Better Auth,
  LangGraph, Alembic) are excluded so third-party owned objects never surface
  as drift.
- No credentials, bound values, or row data appear in the report or its JSON
  representation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ferrum.registry import all_models

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import FieldMeta, Model, ModelMetadata

# Tables Ferrum never manages. ``ferrum_migrations`` is the migration ledger.
# The common third-party prefixes below are defaults only — callers can
# always pass an explicit exclude_tables / auth_tables / langgraph_tables /
# alembic_tables set to override or augment.
_SYSTEM_TABLES: frozenset[str] = frozenset({"ferrum_migrations"})

# Common Better Auth table names (subset of the documented schema). These are
# defaults; callers may override via ``auth_tables``.
_DEFAULT_AUTH_TABLES: frozenset[str] = frozenset(
    {
        "user",
        "session",
        "account",
        "verification",
    }
)

# LangGraph checkpoint store tables (Postgres saver schema). Defaults only.
_DEFAULT_LANGGRAPH_TABLES: frozenset[str] = frozenset(
    {
        "checkpoints",
        "checkpoint_writes",
        "checkpoint_blobs",
        "checkpoint_migrations",
    }
)

# Alembic's version tracking table. Defaults only.
_DEFAULT_ALEMBIC_TABLES: frozenset[str] = frozenset({"alembic_version"})


# ---------------------------------------------------------------------------
# Difference records (frozen dataclasses; JSON-serializable via to_dict)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDifference:
    """Actionable difference for one model-backed column."""

    field: str
    column: str
    kind: str
    expected: str
    actual: str
    guidance: str

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "column": self.column,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class PrimaryKeyDifference:
    """Primary-key column-order mismatch for one table."""

    expected: tuple[str, ...]
    actual: tuple[str, ...]
    guidance: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected": list(self.expected),
            "actual": list(self.actual),
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class IndexDifference:
    """Index mismatch for one table (columns, opclass, predicate, uniqueness)."""

    name: str
    kind: str
    expected: str
    actual: str
    guidance: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class ConstraintDifference:
    """Unique / FK / check constraint mismatch for one table."""

    name: str
    kind: str
    expected: str
    actual: str
    guidance: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class ExtensionDifference:
    """Extension mismatch (missing / extra / version)."""

    name: str
    kind: str
    expected: str
    actual: str
    guidance: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class PolicyDifference:
    """RLS policy mismatch for one table."""

    table: str
    name: str
    kind: str
    expected: str
    actual: str
    guidance: str

    def to_dict(self) -> dict[str, str]:
        return {
            "table": self.table,
            "name": self.name,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class FunctionDifference:
    """Function mismatch (missing / extra / signature)."""

    name: str
    kind: str
    expected: str
    actual: str
    guidance: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class DriftReport:
    """Immutable result of a schema-fidelity comparison."""

    has_drift: bool = False
    compared_tables: tuple[str, ...] = ()
    excluded_tables: tuple[str, ...] = ()
    missing_tables: tuple[str, ...] = ()
    extra_tables: tuple[str, ...] = ()
    missing_columns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    extra_columns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    field_differences: Mapping[str, tuple[FieldDifference, ...]] = field(default_factory=dict)
    primary_key_differences: Mapping[str, PrimaryKeyDifference] = field(default_factory=dict)
    index_differences: Mapping[str, tuple[IndexDifference, ...]] = field(default_factory=dict)
    constraint_differences: Mapping[str, tuple[ConstraintDifference, ...]] = field(
        default_factory=dict
    )
    extension_differences: tuple[ExtensionDifference, ...] = ()
    policy_differences: Mapping[str, tuple[PolicyDifference, ...]] = field(default_factory=dict)
    function_differences: tuple[FunctionDifference, ...] = ()
    live_extensions: tuple[Mapping[str, str], ...] = ()
    live_policies: tuple[Mapping[str, str], ...] = ()
    live_functions: tuple[Mapping[str, str], ...] = ()

    @property
    def column_diffs(self) -> dict[str, dict[str, list[str]]]:
        """Backward-compatible missing/extra-column view."""
        tables = set(self.missing_columns) | set(self.extra_columns)
        return {
            table: {
                "missing_columns": list(self.missing_columns.get(table, ())),
                "extra_columns": list(self.extra_columns.get(table, ())),
            }
            for table in sorted(tables)
        }

    def format_summary(self) -> str:
        """Return a concise operator-facing report with remediation guidance."""
        if not self.has_drift:
            return "Selected Ferrum model metadata matches the live schema."
        lines = ["Schema fidelity differences detected (numbered SQL remains authoritative):"]
        lines.extend(f"- Missing table: {table}" for table in self.missing_tables)
        lines.extend(f"- Unmapped live table: {table}" for table in self.extra_tables)
        for table, columns in self.missing_columns.items():
            lines.extend(f"- Missing column: {table}.{column}" for column in columns)
        for table, columns in self.extra_columns.items():
            lines.extend(f"- Extra column: {table}.{column}" for column in columns)
        for table, differences in self.field_differences.items():
            for difference in differences:
                lines.append(
                    f"- {table}.{difference.column} {difference.kind}: expected "
                    f"{difference.expected}, found {difference.actual}. "
                    f"{difference.guidance}"
                )
        for table, difference in self.primary_key_differences.items():
            lines.append(
                f"- {table} primary key: expected {difference.expected!r}, found "
                f"{difference.actual!r}. {difference.guidance}"
            )
        for table, differences in self.index_differences.items():
            for difference in differences:
                lines.append(
                    f"- {table} index {difference.name} {difference.kind}: expected "
                    f"{difference.expected}, found {difference.actual}. "
                    f"{difference.guidance}"
                )
        for table, differences in self.constraint_differences.items():
            for difference in differences:
                lines.append(
                    f"- {table} constraint {difference.name} {difference.kind}: expected "
                    f"{difference.expected}, found {difference.actual}. "
                    f"{difference.guidance}"
                )
        for difference in self.extension_differences:
            lines.append(
                f"- extension {difference.name} {difference.kind}: expected "
                f"{difference.expected}, found {difference.actual}. "
                f"{difference.guidance}"
            )
        for table, differences in self.policy_differences.items():
            for difference in differences:
                lines.append(
                    f"- {table} policy {difference.name} {difference.kind}: expected "
                    f"{difference.expected}, found {difference.actual}. "
                    f"{difference.guidance}"
                )
        for difference in self.function_differences:
            lines.append(
                f"- function {difference.name} {difference.kind}: expected "
                f"{difference.expected}, found {difference.actual}. "
                f"{difference.guidance}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable representation for CI consumption."""
        return {
            "has_drift": self.has_drift,
            "compared_tables": list(self.compared_tables),
            "excluded_tables": list(self.excluded_tables),
            "missing_tables": list(self.missing_tables),
            "extra_tables": list(self.extra_tables),
            "missing_columns": {table: list(cols) for table, cols in self.missing_columns.items()},
            "extra_columns": {table: list(cols) for table, cols in self.extra_columns.items()},
            "field_differences": {
                table: [d.to_dict() for d in diffs]
                for table, diffs in self.field_differences.items()
            },
            "primary_key_differences": {
                table: diff.to_dict() for table, diff in self.primary_key_differences.items()
            },
            "index_differences": {
                table: [d.to_dict() for d in diffs]
                for table, diffs in self.index_differences.items()
            },
            "constraint_differences": {
                table: [d.to_dict() for d in diffs]
                for table, diffs in self.constraint_differences.items()
            },
            "extension_differences": [d.to_dict() for d in self.extension_differences],
            "policy_differences": {
                table: [d.to_dict() for d in diffs]
                for table, diffs in self.policy_differences.items()
            },
            "function_differences": [d.to_dict() for d in self.function_differences],
            "live_extensions": [dict(ext) for ext in self.live_extensions],
            "live_policies": [dict(pol) for pol in self.live_policies],
            "live_functions": [dict(fn) for fn in self.live_functions],
        }

    def to_json(self) -> str:
        """JSON-serialized form of :meth:`to_dict` for the ``--json`` CLI flag."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Live-schema introspection (PostgreSQL only)
# ---------------------------------------------------------------------------


async def _fetch_postgres_schema(
    conn: Connection,
    *,
    schema: str,
) -> tuple[
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Return columns, primary keys, indexes, constraints, extensions, policies, functions.

    Each query uses ``$1`` for the schema name; no identifier interpolation.
    Returns a 7-tuple aligned with the consumers below.
    """
    if conn.dialect != "postgres":
        raise ValueError("Schema-fidelity drift comparison supports PostgreSQL only.")
    driver = conn._require_driver()
    column_rows = await driver.fetch(
        """
        SELECT
            table_name,
            column_name,
            data_type,
            udt_name,
            is_nullable,
            column_default,
            character_maximum_length,
            numeric_precision,
            numeric_scale
        FROM information_schema.columns
        WHERE table_schema = $1
        ORDER BY table_name, ordinal_position
        """,
        schema,
    )
    pk_rows = await driver.fetch(
        """
        SELECT
            tc.table_name,
            kcu.column_name,
            kcu.ordinal_position
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_catalog = kcu.constraint_catalog
         AND tc.constraint_schema = kcu.constraint_schema
         AND tc.constraint_name = kcu.constraint_name
        WHERE tc.table_schema = $1
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY tc.table_name, kcu.ordinal_position
        """,
        schema,
    )
    # Indexes with opclass and predicate. ``pg_index.indclass[]`` holds OIDs of
    # pg_opclass rows; we join via ``pg_opclass.oid`` to get the name. We also
    # surface ``indpred`` (partial-index predicate) via ``pg_get_expr``.
    index_rows = await driver.fetch(
        """
        SELECT
            t.relname AS table_name,
            c.relname AS index_name,
            i.indisunique AS is_unique,
            am.amname AS using_method,
            pg_get_indexdef(i.indexrelid) AS indexdef,
            pg_get_expr(i.indpred, i.indrelid) AS predicate,
            i.indisready AS is_ready
        FROM pg_index AS i
        JOIN pg_class AS c ON c.oid = i.indexrelid
        JOIN pg_class AS t ON t.oid = i.indrelid
        JOIN pg_namespace AS n ON n.oid = t.relnamespace
        JOIN pg_am AS am ON am.oid = c.relam
        WHERE n.nspname = $1
        ORDER BY t.relname, c.relname
        """,
        schema,
    )
    # Constraints: unique, foreign-key, check. Excludes PRIMARY KEY (handled
    # separately above) and PostgreSQL 18+ NOT NULL constraints (contype='n'),
    # which are an implementation detail of NOT NULL columns, not a
    # model-declarable CHECK constraint.
    constraint_rows = await driver.fetch(
        """
        SELECT
            t.relname AS table_name,
            con.conname AS constraint_name,
            CASE con.contype
                WHEN 'u' THEN 'UNIQUE'
                WHEN 'f' THEN 'FOREIGN KEY'
                WHEN 'c' THEN 'CHECK'
            END AS constraint_type,
            a.attname AS column_name,
            ft.relname AS foreign_table,
            fa.attname AS foreign_column,
            rc.delete_rule,
            pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint AS con
        JOIN pg_class AS t ON t.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = con.connamespace
        LEFT JOIN pg_attribute AS a
          ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
        LEFT JOIN pg_class AS ft ON ft.oid = con.confrelid
        LEFT JOIN pg_attribute AS fa
          ON fa.attrelid = con.confrelid AND fa.attnum = con.confkey[1]
        LEFT JOIN information_schema.referential_constraints AS rc
          ON rc.constraint_name = con.conname
        WHERE n.nspname = $1
          AND con.contype IN ('u', 'f', 'c')
        ORDER BY t.relname, con.conname
        """,
        schema,
    )
    extension_rows = await driver.fetch(
        """
        SELECT extname AS name, extversion AS version
        FROM pg_extension
        ORDER BY extname
        """,
    )
    policy_rows = await driver.fetch(
        """
        SELECT
            schemaname AS schema,
            tablename AS table_name,
            policyname AS name,
            permissive,
            roles,
            cmd AS command,
            qual AS using_expr,
            with_check AS check_expr
        FROM pg_policies
        WHERE schemaname = $1
        ORDER BY tablename, policyname
        """,
        schema,
    )
    function_rows = await driver.fetch(
        """
        SELECT
            p.proname AS name,
            pg_get_function_identity_arguments(p.oid) AS arguments,
            pg_get_functiondef(p.oid) AS definition,
            l.lanname AS language
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        JOIN pg_language AS l ON l.oid = p.prolang
        WHERE n.nspname = $1
          AND p.prokind = 'f'
          AND p.proname NOT LIKE 'pg_%'
          AND p.proname NOT LIKE '_pg_%'
          AND NOT EXISTS (
            SELECT 1 FROM pg_depend AS d
            JOIN pg_extension AS e ON e.oid = d.refobjid
            WHERE d.objid = p.oid AND d.deptype = 'e'
          )
        ORDER BY p.proname
        """,
        schema,
    )

    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for row in column_rows:
        values = _row_values(
            row,
            (
                "table_name",
                "column_name",
                "data_type",
                "udt_name",
                "is_nullable",
                "column_default",
                "character_maximum_length",
                "numeric_precision",
                "numeric_scale",
            ),
        )
        table, column = str(values[0]), str(values[1])
        tables.setdefault(table, {})[column] = {
            "data_type": str(values[2]),
            "udt_name": str(values[3]),
            "nullable": values[4] == "YES",
            "default": values[5],
            "max_length": values[6],
            "numeric_precision": values[7],
            "numeric_scale": values[8],
        }
    primary_keys: dict[str, list[tuple[int, str]]] = {}
    for row in pk_rows:
        table, column, position = _row_values(
            row,
            ("table_name", "column_name", "ordinal_position"),
        )
        primary_keys.setdefault(str(table), []).append((int(position), str(column)))
    pk_map = {
        table: tuple(column for _, column in sorted(columns))
        for table, columns in primary_keys.items()
    }

    indexes_by_table: dict[str, list[dict[str, Any]]] = {}
    for row in index_rows:
        values = _row_values(
            row,
            (
                "table_name",
                "index_name",
                "is_unique",
                "using_method",
                "indexdef",
                "predicate",
                "is_ready",
            ),
        )
        table = str(values[0])
        indexdef = str(values[4])
        indexes_by_table.setdefault(table, []).append(
            {
                "name": str(values[1]),
                "unique": bool(values[2]),
                "using": str(values[3]),
                "definition": indexdef,
                "predicate": values[5],
                "fields": _parse_index_columns(indexdef),
            }
        )

    constraints_by_table: dict[str, list[dict[str, Any]]] = {}
    for row in constraint_rows:
        values = _row_values(
            row,
            (
                "table_name",
                "constraint_name",
                "constraint_type",
                "column_name",
                "foreign_table",
                "foreign_column",
                "delete_rule",
                "definition",
            ),
        )
        table = str(values[0])
        constraints_by_table.setdefault(table, []).append(
            {
                "name": str(values[1]),
                "type": str(values[2]),
                "column": values[3],
                "foreign_table": values[4],
                "foreign_column": values[5],
                "delete_rule": values[6],
                "definition": values[7],
            }
        )

    extensions = [
        {
            "name": str(_row_values(r, ("name", "version"))[0]),
            "version": str(_row_values(r, ("name", "version"))[1]),
        }
        for r in extension_rows
    ]
    policies = [
        {
            "schema": str(
                _row_values(
                    r,
                    (
                        "schema",
                        "table_name",
                        "name",
                        "permissive",
                        "roles",
                        "command",
                        "using_expr",
                        "check_expr",
                    ),
                )[0]
            ),
            "table": str(
                _row_values(
                    r,
                    (
                        "schema",
                        "table_name",
                        "name",
                        "permissive",
                        "roles",
                        "command",
                        "using_expr",
                        "check_expr",
                    ),
                )[1]
            ),
            "name": str(
                _row_values(
                    r,
                    (
                        "schema",
                        "table_name",
                        "name",
                        "permissive",
                        "roles",
                        "command",
                        "using_expr",
                        "check_expr",
                    ),
                )[2]
            ),
            "permissive": str(
                _row_values(
                    r,
                    (
                        "schema",
                        "table_name",
                        "name",
                        "permissive",
                        "roles",
                        "command",
                        "using_expr",
                        "check_expr",
                    ),
                )[3]
            ),
            "command": str(
                _row_values(
                    r,
                    (
                        "schema",
                        "table_name",
                        "name",
                        "permissive",
                        "roles",
                        "command",
                        "using_expr",
                        "check_expr",
                    ),
                )[5]
            ),
            "using": _row_values(
                r,
                (
                    "schema",
                    "table_name",
                    "name",
                    "permissive",
                    "roles",
                    "command",
                    "using_expr",
                    "check_expr",
                ),
            )[6],
        }
        for r in policy_rows
    ]
    functions = [
        {
            "name": str(_row_values(r, ("name", "arguments", "definition", "language"))[0]),
            "arguments": str(_row_values(r, ("name", "arguments", "definition", "language"))[1]),
            "language": str(_row_values(r, ("name", "arguments", "definition", "language"))[3]),
        }
        for r in function_rows
    ]

    return tables, pk_map, indexes_by_table, constraints_by_table, extensions, policies, functions


def _row_values(row: Any, keys: tuple[str, ...]) -> tuple[Any, ...]:  # noqa: ANN401
    if isinstance(row, Mapping):
        return tuple(row[key] for key in keys)
    return tuple(row)


def _parse_index_columns(indexdef: str) -> tuple[str, ...]:
    """Extract column names from a ``CREATE INDEX ... (col1, col2, ...)`` statement.

    PostgreSQL's ``pg_get_indexdef`` returns a complete ``CREATE INDEX`` string.
    The column list is the first parenthesized group after the table name.
    Expressions (not plain columns) are returned as the expression text —
    callers comparing against model-declared indexes should treat any
    non-column entry as an expression index that the model cannot declare.
    """
    # Find the first '(' after "ON table" — the column list.
    on_idx = indexdef.upper().find(" ON ")
    if on_idx < 0:
        return ()
    paren_start = indexdef.find("(", on_idx)
    if paren_start < 0:
        return ()
    # Find the matching ')'. Index definitions may contain nested parens
    # for expressions; we balance them.
    depth = 0
    paren_end = paren_start
    for i in range(paren_start, len(indexdef)):
        if indexdef[i] == "(":
            depth += 1
        elif indexdef[i] == ")":
            depth -= 1
            if depth == 0:
                paren_end = i
                break
    inner = indexdef[paren_start + 1 : paren_end]
    # Split on commas at depth 0 (expressions may contain commas inside
    # function calls).
    parts: list[str] = []
    current = ""
    depth = 0
    for char in inner:
        if char == "(":
            depth += 1
            current += char
        elif char == ")":
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    # Strip operator class and ordering suffixes (``col_ops``, ``col ASC``,
    # ``col DESC``, ``NULLS FIRST``, ``NULLS LAST``) from each part to get
    # the bare column name.
    columns: list[str] = []
    for part in parts:
        # Remove opclass (``col_ops``) — a word following the column name
        # separated by a space, ending in ``_ops``.
        token = part.split()[0] if part.split() else part
        columns.append(token.strip('"'))
    return tuple(columns)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------


def _resolve_models(
    selected: Iterable[type[Model] | str] | None,
) -> tuple[type[Model], ...]:
    registered = all_models()
    if selected is None:
        resolved = tuple(registered.values())
    else:
        items: list[type[Model]] = []
        for item in selected:
            if isinstance(item, str):
                try:
                    items.append(registered[item])
                except KeyError as exc:
                    raise ValueError(
                        f"Unknown registered Ferrum model {item!r}. "
                        f"Available models: {sorted(registered)!r}."
                    ) from exc
            elif item.__name__ not in registered or registered[item.__name__] is not item:
                raise ValueError(
                    f"Model {item.__name__!r} is not the currently registered Ferrum model "
                    "for that name."
                )
            else:
                items.append(item)
        resolved = tuple(items)
    table_names = [model.get_metadata().table_name for model in resolved]
    duplicates = sorted({table for table in table_names if table_names.count(table) > 1})
    if duplicates:
        raise ValueError(f"Selected Ferrum models map to duplicate tables: {duplicates!r}.")
    return resolved


# ---------------------------------------------------------------------------
# Type comparison helpers
# ---------------------------------------------------------------------------


def _expected_type(field_meta: FieldMeta) -> str:
    return field_meta.sql_type


def _actual_type(column: Mapping[str, Any]) -> str:
    data_type = str(column["data_type"])
    if data_type == "ARRAY":
        return {
            "_text": "TEXT[]",
            "_int4": "INTEGER[]",
            "_uuid": "UUID[]",
            "_float8": "FLOAT8[]",
        }.get(str(column["udt_name"]), f"{column['udt_name']}[]")
    if data_type == "character varying":
        max_length = column.get("max_length")
        return f"VARCHAR({max_length})" if max_length is not None else "VARCHAR"
    if data_type == "numeric":
        precision = column.get("numeric_precision")
        scale = column.get("numeric_scale")
        if precision is not None and scale is not None:
            return f"NUMERIC({precision},{scale})"
    if data_type == "USER-DEFINED":
        return str(column["udt_name"]).upper()
    aliases = {
        "bigint": "BIGINT",
        "boolean": "BOOLEAN",
        "bytea": "BYTEA",
        "date": "DATE",
        "double precision": "FLOAT8",
        "integer": "INTEGER",
        "jsonb": "JSONB",
        "numeric": "NUMERIC",
        "real": "REAL",
        "text": "TEXT",
        "time without time zone": "TIME",
        "timestamp with time zone": "TIMESTAMPTZ",
        "uuid": "UUID",
    }
    return aliases.get(data_type, data_type.upper())


def _types_match(field_meta: FieldMeta, column: Mapping[str, Any]) -> bool:
    expected = _expected_type(field_meta)
    actual = _actual_type(column)
    if field_meta.field_type == "big_int":
        return actual == "BIGINT"
    return expected == actual


def _normalize_default(value: Any) -> str | None:  # noqa: ANN401
    """Normalize a PostgreSQL ``column_default`` for comparison.

    Strips the type cast that PostgreSQL appends to defaults like
    ``'foo'::text`` or ``now()::timestamptz`` so comparison against the
    model's ``db_default`` (which is a bare expression like ``NOW()``) is
    shape-stable. Returns ``None`` for NULL defaults.
    """
    if value is None:
        return None
    text = str(value).strip()
    if "::" in text:
        text = text.split("::", 1)[0]
    # Strip surrounding single quotes for string-literal defaults so a model
    # ``db_default="''"`` (which normalizes to an empty string literal) lines
    # up with the live ``''::text`` representation.
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        text = text[1:-1]
    return text


def _normalize_db_default(value: str | None) -> str | None:
    """Normalize a Ferrum ``FieldMeta.db_default`` expression.

    The model-side value is stored canonicalized (e.g. ``"NOW()"``). We strip
    surrounding single quotes for string literals so ``"''"`` (the documented
    empty-string default) compares equal to an empty string.
    """
    if value is None:
        return None
    text = value.strip()
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        text = text[1:-1]
    return text


def _defaults_match(field_meta: FieldMeta, column: Mapping[str, Any]) -> bool:
    expected = _normalize_db_default(field_meta.db_default)
    actual = _normalize_default(column.get("default"))
    # Treat "no model default" as compatible with any live default — models
    # may omit ``db_default`` while the DB carries one Ferrum did not author.
    if expected is None:
        return True
    return expected.upper() == actual.upper() if actual is not None else False


def _vector_dimensions_from_udt(column: Mapping[str, Any]) -> int | None:
    """Return vector dimensions recorded on the live column, if any.

    PostgreSQL's ``information_schema.columns`` does not expose the typmod
    that pgvector stores the dimension in. We approximate by parsing the
    ``udt_name`` (``vector``) — the actual dimension is in ``atttypmod`` in
    ``pg_attribute``. Caller can supply an override via the column dict.
    """
    if (
        str(column["data_type"]).upper() == "USER-DEFINED"
        and str(column["udt_name"]).lower() == "vector"
    ):
        # If the introspection layer enriched the column with ``dimensions``,
        # use that; otherwise we cannot determine it from information_schema.
        return column.get("dimensions")  # type: ignore[return-value]
    return None


# ---------------------------------------------------------------------------
# Index / constraint comparison helpers
# ---------------------------------------------------------------------------


def _expected_indexes(metadata: ModelMetadata) -> list[dict[str, Any]]:
    """Return the model-declared indexes in a comparable shape.

    Includes:
    - ``Meta.indexes`` entries (IndexMeta).
    - Per-column ``db_index=True`` single-column indexes.

    Note: ``unique=True`` fields do NOT generate an implicit index entry
    here — PostgreSQL backs the unique constraint with an index, and the
    constraint is reported via :func:`_expected_constraints`. Reporting a
    separate expected index would double-count the same backing object.
    """
    expected: list[dict[str, Any]] = []
    for idx in metadata.indexes:
        expected.append(
            {
                "name": idx.name,
                "fields": list(idx.fields),
                "unique": idx.unique,
                "using": idx.using.upper(),
                "predicate": idx.where,
            }
        )
    # Per-column db_index produces implicit indexes. PostgreSQL names them
    # ``<table>_<column>_idx`` but the actual name is implementation-defined;
    # we compare by columns and uniqueness below, not by name. Unique fields
    # are handled via constraint comparison, not here.
    for field_meta in metadata.fields:
        if field_meta.unique:
            continue
        if field_meta.db_index:
            expected.append(
                {
                    "name": f"idx_{metadata.table_name}_{field_meta.column_name}",
                    "fields": [field_meta.column_name],
                    "unique": False,
                    "using": "BTREE",
                    "predicate": None,
                }
            )
    return expected


def _index_signature(idx: Mapping[str, Any]) -> tuple[Any, ...]:
    """Stable signature for matching model-declared vs. live indexes.

    Name is excluded: PostgreSQL may rename a backing index (e.g. for unique
    constraints). We match on (columns, unique, using, predicate). Column
    order is preserved — btree column order is semantically significant.
    """
    columns = tuple(idx.get("fields") or idx.get("columns") or ())  # type: ignore[arg-type]
    return (
        columns,
        bool(idx.get("unique")),
        str(idx.get("using", "")).upper(),
        (idx.get("predicate") or None),
    )


def _compare_indexes(
    table: str,
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> tuple[IndexDifference, ...]:
    diffs: list[IndexDifference] = []
    actual_by_sig = {_index_signature(a): a for a in actual}
    expected_by_sig = {_index_signature(e): e for e in expected}

    for expected_idx in expected:
        sig = _index_signature(expected_idx)
        if sig not in actual_by_sig:
            diffs.append(
                IndexDifference(
                    name=str(expected_idx["name"]),
                    kind="missing_index",
                    expected=(
                        f"unique={expected_idx['unique']} using={expected_idx['using']} "
                        f"columns={expected_idx['fields']} predicate={expected_idx['predicate']!r}"
                    ),
                    actual="absent",
                    guidance="Add the missing index via a numbered SQL migration.",
                )
            )

    # Report extra live indexes only when they are not backing a unique / PK
    # constraint that the model expects. We exclude indexes that begin with
    # the table name + "_" prefix that PostgreSQL uses for constraint-backed
    # indexes (those are surfaced via constraint comparison instead).
    for actual_idx in actual:
        sig = _index_signature(actual_idx)
        if sig in expected_by_sig:
            continue
        name = str(actual_idx["name"])
        # Skip indexes that back primary-key or unique constraints; those are
        # surfaced via constraint comparison and would produce noise here.
        if name.endswith("_pkey") or name.endswith("_key"):
            continue
        diffs.append(
            IndexDifference(
                name=name,
                kind="extra_index",
                expected="absent",
                actual=(
                    f"unique={actual_idx.get('unique')} using={actual_idx.get('using')} "
                    f"definition={actual_idx.get('definition')!r} "
                    f"predicate={actual_idx.get('predicate')!r}"
                ),
                guidance=(
                    "Verify whether this index was created intentionally outside of Ferrum. "
                    "If it should be tracked, declare it in Meta.indexes."
                ),
            )
        )
    return tuple(diffs)


def _expected_constraints(metadata: ModelMetadata) -> list[dict[str, Any]]:
    """Return model-declared unique / FK / check constraints in comparable shape."""
    expected: list[dict[str, Any]] = []
    # Unique constraints on single columns via Field(unique=True). Composite
    # uniques come from Meta.indexes (handled as indexes; their backing
    # constraint is also surfaced here for clarity).
    for field_meta in metadata.fields:
        if field_meta.unique:
            expected.append(
                {
                    "name": f"{metadata.table_name}_{field_meta.column_name}_key",
                    "type": "UNIQUE",
                    "column": field_meta.column_name,
                    "definition": f"UNIQUE ({field_meta.column_name})",
                }
            )
    # FK constraints from declared relations.
    for relation in metadata.relations:
        if relation.kind in ("fk", "one_to_one") and relation.db_column:
            expected.append(
                {
                    "name": f"{metadata.table_name}_{relation.db_column}_fkey",
                    "type": "FOREIGN KEY",
                    "column": relation.db_column,
                    "foreign_table": None,  # resolved by metadata caller if needed
                    "foreign_column": None,
                    "delete_rule": (relation.on_delete or "NO ACTION").upper(),
                    "definition": (
                        f"FOREIGN KEY ({relation.db_column}) REFERENCES "
                        f"{relation.to_model} (id) ON DELETE "
                        f"{(relation.on_delete or 'NO ACTION').upper()}"
                    ),
                }
            )
    # CHECK constraints derived from enum_values.
    for field_meta in metadata.fields:
        if field_meta.enum_values:
            values_sql = ", ".join(f"'{v}'" for v in field_meta.enum_values)
            expected.append(
                {
                    "name": f"{metadata.table_name}_{field_meta.column_name}_check",
                    "type": "CHECK",
                    "column": field_meta.column_name,
                    "definition": (f"CHECK ({field_meta.column_name} IN ({values_sql}))"),
                }
            )
    return expected


def _compare_constraints(
    table: str,
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> tuple[ConstraintDifference, ...]:
    diffs: list[ConstraintDifference] = []
    actual_by_name = {str(a["name"]): a for a in actual}
    expected_by_name = {str(e["name"]): e for e in expected}

    for name, expected_c in expected_by_name.items():
        if name not in actual_by_name:
            diffs.append(
                ConstraintDifference(
                    name=name,
                    kind=f"missing_{expected_c['type'].lower().replace(' ', '_')}_constraint",
                    expected=str(expected_c.get("definition") or expected_c["type"]),
                    actual="absent",
                    guidance=(
                        "Add the missing constraint via a numbered SQL migration; do not "
                        "auto-apply."
                    ),
                )
            )
        else:
            actual_c = actual_by_name[name]
            # For FK constraints, compare delete rule.
            if expected_c["type"] == "FOREIGN KEY":
                expected_rule = str(expected_c.get("delete_rule") or "NO ACTION").upper()
                actual_rule = str(actual_c.get("delete_rule") or "NO ACTION").upper()
                if expected_rule != actual_rule:
                    diffs.append(
                        ConstraintDifference(
                            name=name,
                            kind="foreign_key_delete_rule",
                            expected=f"ON DELETE {expected_rule}",
                            actual=f"ON DELETE {actual_rule}",
                            guidance="Align the ON DELETE rule via a numbered SQL migration.",
                        )
                    )
            # For CHECK constraints, compare the normalized definition.
            if expected_c["type"] == "CHECK":
                expected_def = str(expected_c.get("definition") or "").upper()
                actual_def = str(actual_c.get("definition") or "").upper()
                if expected_def and actual_def and expected_def != actual_def:
                    diffs.append(
                        ConstraintDifference(
                            name=name,
                            kind="check_definition",
                            expected=expected_def,
                            actual=actual_def,
                            guidance=(
                                "CHECK constraint definition drift; align via a numbered "
                                "SQL migration."
                            ),
                        )
                    )

    # Extra live constraints (not declared by the model). Skip PK constraints
    # (handled separately) and constraints that back unique indexes already
    # reported as indexes — but still report extra FK / CHECK constraints.
    for name, actual_c in actual_by_name.items():
        if name in expected_by_name:
            continue
        kind = str(actual_c["type"])
        if kind == "PRIMARY KEY":
            continue
        diffs.append(
            ConstraintDifference(
                name=name,
                kind=f"extra_{kind.lower().replace(' ', '_')}_constraint",
                expected="absent",
                actual=str(actual_c.get("definition") or kind),
                guidance=(
                    "Verify whether this constraint was created intentionally outside "
                    "of Ferrum. If it should be tracked, declare it on the model."
                ),
            )
        )
    return tuple(diffs)


def _compare_extensions(
    expected: Iterable[str],
    actual: Iterable[Mapping[str, str]],
) -> tuple[ExtensionDifference, ...]:
    expected_set = {name.upper() for name in expected}
    actual_by_name = {str(a["name"]).upper(): a for a in actual}
    diffs: list[ExtensionDifference] = []
    for name in sorted(expected_set):
        if name not in actual_by_name:
            diffs.append(
                ExtensionDifference(
                    name=name,
                    kind="missing_extension",
                    expected="installed",
                    actual="absent",
                    guidance=(
                        "Create the extension via a numbered SQL migration using CreateExtension."
                    ),
                )
            )
    return tuple(diffs)


def _compare_policies(
    table: str,
    expected: Iterable[Mapping[str, str]],
    actual: Iterable[Mapping[str, str]],
) -> tuple[PolicyDifference, ...]:
    expected_by_name = {str(p["name"]): p for p in expected}
    actual_by_name = {str(p["name"]): p for p in actual}
    diffs: list[PolicyDifference] = []
    for name, expected_p in expected_by_name.items():
        if name not in actual_by_name:
            diffs.append(
                PolicyDifference(
                    table=table,
                    name=name,
                    kind="missing_policy",
                    expected=str(expected_p.get("command", "")),
                    actual="absent",
                    guidance="Add the policy via a numbered SQL migration using CreatePolicy.",
                )
            )
    return tuple(diffs)


def _compare_functions(
    expected: Iterable[Mapping[str, str]],
    actual: Iterable[Mapping[str, str]],
) -> tuple[FunctionDifference, ...]:
    expected_by_name = {str(f["name"]): f for f in expected}
    actual_by_name = {str(f["name"]): f for f in actual}
    diffs: list[FunctionDifference] = []
    for name, expected_f in expected_by_name.items():
        if name not in actual_by_name:
            diffs.append(
                FunctionDifference(
                    name=name,
                    kind="missing_function",
                    expected=str(expected_f.get("arguments", "")),
                    actual="absent",
                    guidance="Add the function via a numbered SQL migration using CreateFunction.",
                )
            )
    return tuple(diffs)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def detect_drift(
    conn: Connection,
    models: Iterable[type[Model] | str] | None = None,
    *,
    schema: str = "public",
    exclude_tables: Iterable[str] = (),
    auth_tables: Iterable[str] = (),
    langgraph_tables: Iterable[str] = (),
    alembic_tables: Iterable[str] = (),
    exclude_schemas: Iterable[str] = (),
    include_unmapped_tables: bool = False,
    expected_extensions: Iterable[str] = (),
    expected_policies: Mapping[str, Iterable[Mapping[str, str]]] | None = None,
    expected_functions: Iterable[Mapping[str, str]] | None = None,
) -> DriftReport:
    """Compare selected registered model metadata with live PostgreSQL schema.

    The ``auth_tables``, ``langgraph_tables``, and ``alembic_tables`` carve-outs
    are explicit so callers must document those ownership boundaries at the
    call site. They are treated identically to ``exclude_tables``. Unselected
    live tables are ignored unless ``include_unmapped_tables=True``.

    ``expected_extensions`` / ``expected_policies`` / ``expected_functions``
    allow the caller to compare live state against an explicit allowlist when
    the model metadata does not carry that information (extensions, RLS,
    functions are schema-wide, not per-model). When omitted, the live state is
    still inventoried in the report (``live_extensions`` / ``live_policies`` /
    ``live_functions``) but no diff is computed.

    Read-only: never emits or applies DDL.
    """
    selected_models = _resolve_models(models)
    excluded = frozenset(
        _SYSTEM_TABLES
        | set(exclude_tables)
        | set(auth_tables)
        | set(langgraph_tables)
        | set(alembic_tables)
    )
    if schema in exclude_schemas:
        raise ValueError(
            f"Schema {schema!r} is in exclude_schemas; cannot drift-check an excluded schema."
        )
    selected_metadata: tuple[ModelMetadata, ...] = tuple(
        model.get_metadata()
        for model in selected_models
        if model.get_metadata().table_name not in excluded
    )
    (
        live_state,
        live_primary_keys,
        live_indexes,
        live_constraints,
        live_extensions,
        live_policies,
        live_functions,
    ) = await _fetch_postgres_schema(conn, schema=schema)
    model_table_names = {metadata.table_name for metadata in selected_metadata}
    missing_tables = tuple(sorted(model_table_names - live_state.keys()))
    extra_tables = (
        tuple(sorted(set(live_state) - model_table_names - excluded))
        if include_unmapped_tables
        else ()
    )
    missing_columns: dict[str, tuple[str, ...]] = {}
    extra_columns: dict[str, tuple[str, ...]] = {}
    field_differences: dict[str, tuple[FieldDifference, ...]] = {}
    primary_key_differences: dict[str, PrimaryKeyDifference] = {}
    index_differences: dict[str, tuple[IndexDifference, ...]] = {}
    constraint_differences: dict[str, tuple[ConstraintDifference, ...]] = {}

    for metadata in selected_metadata:
        table = metadata.table_name
        if table not in live_state:
            continue
        live_columns = live_state[table]
        fields_by_column = {item.column_name: item for item in metadata.fields}
        missing = tuple(sorted(set(fields_by_column) - set(live_columns)))
        extra = tuple(sorted(set(live_columns) - set(fields_by_column)))
        if missing:
            missing_columns[table] = missing
        if extra:
            extra_columns[table] = extra
        differences: list[FieldDifference] = []
        for column_name in sorted(set(fields_by_column) & set(live_columns)):
            field_meta = fields_by_column[column_name]
            live_column = live_columns[column_name]
            if not _types_match(field_meta, live_column):
                differences.append(
                    FieldDifference(
                        field=field_meta.name,
                        column=column_name,
                        kind="type",
                        expected=_expected_type(field_meta),
                        actual=_actual_type(live_column),
                        guidance=(
                            "Add or correct the next numbered SQL migration; do not auto-apply."
                        ),
                    )
                )
            if field_meta.nullable != live_column["nullable"]:
                differences.append(
                    FieldDifference(
                        field=field_meta.name,
                        column=column_name,
                        kind="nullability",
                        expected="NULL" if field_meta.nullable else "NOT NULL",
                        actual="NULL" if live_column["nullable"] else "NOT NULL",
                        guidance=(
                            "Align model nullability with authoritative SQL or add a guarded "
                            "numbered migration."
                        ),
                    )
                )
            if not _defaults_match(field_meta, live_column):
                differences.append(
                    FieldDifference(
                        field=field_meta.name,
                        column=column_name,
                        kind="default",
                        expected=str(field_meta.db_default),
                        actual=str(live_column.get("default")),
                        guidance=(
                            "Align model db_default with the live DEFAULT or add a guarded "
                            "numbered migration."
                        ),
                    )
                )
            # Vector dimension comparison (only when the live column exposes it).
            if field_meta.vector_dimensions is not None:
                live_dims = _vector_dimensions_from_udt(live_column)
                if live_dims is not None and int(live_dims) != int(field_meta.vector_dimensions):
                    differences.append(
                        FieldDifference(
                            field=field_meta.name,
                            column=column_name,
                            kind="vector_dimensions",
                            expected=f"VECTOR({field_meta.vector_dimensions})",
                            actual=f"VECTOR({live_dims})",
                            guidance=(
                                "Vector dimension drift requires an explicit numbered SQL "
                                "migration (rebuild the vector column)."
                            ),
                        )
                    )
        if differences:
            field_differences[table] = tuple(differences)
        expected_pk = tuple(metadata.fields[index].column_name for index in metadata.pk_fields)
        actual_pk = live_primary_keys.get(table, ())
        if expected_pk != actual_pk:
            primary_key_differences[table] = PrimaryKeyDifference(
                expected=expected_pk,
                actual=actual_pk,
                guidance=(
                    "Primary-key changes require an explicit numbered SQL migration and "
                    "write-path review."
                ),
            )
        # Index comparison.
        expected_idxs = _expected_indexes(metadata)
        actual_idxs = live_indexes.get(table, [])
        idx_diffs = _compare_indexes(table, expected_idxs, actual_idxs)
        if idx_diffs:
            index_differences[table] = idx_diffs

        # Constraint comparison.
        expected_cons = _expected_constraints(metadata)
        actual_cons = live_constraints.get(table, [])
        cons_diffs = _compare_constraints(table, expected_cons, actual_cons)
        if cons_diffs:
            constraint_differences[table] = cons_diffs

    # Schema-wide comparisons (extensions, policies, functions). Only diff
    # when the caller supplied an expected set.
    extension_differences = _compare_extensions(expected_extensions, live_extensions)
    policy_differences: dict[str, tuple[PolicyDifference, ...]] = {}
    if expected_policies is not None:
        for table, expected_table_policies in expected_policies.items():
            actual_table_policies = [p for p in live_policies if p.get("table") == table]
            pol_diffs = _compare_policies(table, expected_table_policies, actual_table_policies)
            if pol_diffs:
                policy_differences[table] = pol_diffs
    function_differences: tuple[FunctionDifference, ...] = ()
    if expected_functions is not None:
        function_differences = _compare_functions(expected_functions, live_functions)

    has_drift = bool(
        missing_tables
        or extra_tables
        or missing_columns
        or extra_columns
        or field_differences
        or primary_key_differences
        or index_differences
        or constraint_differences
        or extension_differences
        or policy_differences
        or function_differences
    )
    return DriftReport(
        has_drift=has_drift,
        compared_tables=tuple(sorted(model_table_names)),
        excluded_tables=tuple(sorted(excluded)),
        missing_tables=missing_tables,
        extra_tables=extra_tables,
        missing_columns=missing_columns,
        extra_columns=extra_columns,
        field_differences=field_differences,
        primary_key_differences=primary_key_differences,
        index_differences=index_differences,
        constraint_differences=constraint_differences,
        extension_differences=extension_differences,
        policy_differences=policy_differences,
        function_differences=function_differences,
        live_extensions=tuple(dict(ext) for ext in live_extensions),
        live_policies=tuple(dict(pol) for pol in live_policies),
        live_functions=tuple(dict(fn) for fn in live_functions),
    )


# ---------------------------------------------------------------------------
# Default unmanaged-table presets (used by the CLI)
# ---------------------------------------------------------------------------


def default_unmanaged_tables() -> frozenset[str]:
    """Return the default set of third-party-owned table names.

    Combines Better Auth, LangGraph, and Alembic defaults. Callers may
    extend or override via ``detect_drift``'s carve-out parameters.
    """
    return _DEFAULT_AUTH_TABLES | _DEFAULT_LANGGRAPH_TABLES | _DEFAULT_ALEMBIC_TABLES
