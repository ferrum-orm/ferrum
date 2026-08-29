"""Migration orchestrator: dry-run, plan classification, apply sequencing.

The orchestrator is the entry point for all migration operations. It enforces
the mandatory dry-run → confirm → apply sequence (MIG-1) and routes plans
through the appropriate gate checks (MIG-2 / MIG-5) before any SQL reaches
the database.

No SQL is applied without a completed dry-run cycle. This is enforced
structurally: ``apply()`` requires the ``MigrationPlan`` object returned by
``dry_run()``, not raw SQL strings.

Security invariants:
- All SQL identifiers emitted by ``_op_to_sql`` are double-quoted.
- Identifier values are sourced exclusively from the Rust-generated plan JSON,
  which itself sources them from model-metadata allowlists (AGENTS.md §2.9).
- Bound parameter values never appear in plan JSON; only DDL identifiers do.
- Destructive operations require explicit ``confirm=True`` (MIG-2).
- Non-development environments require explicit ``confirm=True`` (MIG-5).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.ledger import (
    ADVISORY_LOCK_KEY_1,
    ADVISORY_LOCK_KEY_2,
    advisory_lock_sql,
    ensure_ledger,
    is_applied_on_conn,
    record_applied,
    record_applied_on_conn,
)
from ferrum.migrations.tokens import verify_token

if TYPE_CHECKING:
    from ferrum.connection import Connection, ConnectionLike
    from ferrum.migrations.loader import MigrationModule
    from ferrum.models import FieldMeta, Model, ModelMetadata


# ---------------------------------------------------------------------------
# Schema state dataclasses — projected migration state used by makemigrations
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ColumnState:
    """Projected state of a single table column after replaying migration ops."""

    sql_type: str
    not_null: bool = False
    default: str | None = None


@dataclasses.dataclass
class IndexState:
    """Projected state of a single index after replaying migration ops."""

    table: str
    columns: list[str] = dataclasses.field(default_factory=list)
    unique: bool = False
    using: str = "btree"
    where: str | None = None


@dataclasses.dataclass
class SchemaState:
    """Full projected schema state derived by replaying prior migration files.

    ``tables`` maps table name → column name → ``ColumnState``.
    ``indexes`` maps index name → ``IndexState``.
    """

    tables: dict[str, dict[str, ColumnState]] = dataclasses.field(default_factory=dict)
    indexes: dict[str, IndexState] = dataclasses.field(default_factory=dict)


class OperationClass(Enum):
    """Classification of a migration operation by safety profile."""

    SAFE = "safe"
    DESTRUCTIVE = "destructive"
    NON_TRANSACTIONAL = "non_transactional"


@dataclass
class PlannedOperation:
    """A single DDL operation within a dry-run plan.

    Carries the rendered ``sql``, a human-readable ``description``, its
    ``classification`` (safety profile), and the target ``table``. Holds no
    bound values or row data (MIG identifiers only).
    """

    sql: str
    description: str
    classification: OperationClass
    table: str = ""


@dataclass
class MigrationPlan:
    """The output of a dry-run pass. Required as input to ``apply()``."""

    operations: list[PlannedOperation] = field(default_factory=list)
    digest: str = ""
    dry_run_completed: bool = False
    has_destructive: bool = False

    def __post_init__(self) -> None:
        self.has_destructive = any(
            op.classification == OperationClass.DESTRUCTIVE for op in self.operations
        )


@dataclass
class MigrationResult:
    """Result of an ``apply()`` call."""

    applied: bool
    ops_count: int
    dry_run: bool


# Destructive migration op kinds — require explicit ``confirm=True`` (MIG-2).
# Non-transactional op kinds — must run outside an explicit transaction block
# on some PostgreSQL configurations (ADR-004 gate, future enforcement).
_DESTRUCTIVE_KINDS: frozenset[str] = frozenset(
    {
        "drop_table",
        "drop_column",
        "drop_fk",
        "raw_sql",
        "drop_extension",
        "disable_rls",
        "drop_policy",
        "drop_function",
    }
)
_NON_TRANSACTIONAL_KINDS: frozenset[str] = frozenset(
    {
        "create_extension",
        "create_function",
    }
)


def _is_op_destructive(op: dict[str, Any]) -> bool:
    """Return True if *op* requires explicit destructive confirmation (MIG-2).

    W1-C: ``alter_column`` is destructive when it narrows the type
    (``sql_type`` set) or sets ``NOT NULL`` — both can fail on populated
    columns. ``DROP NOT NULL`` / ``SET DEFAULT`` / ``DROP DEFAULT`` are safe.
    ``add_index`` with ``concurrently=True`` is non-transactional, not
    destructive. Other kinds use the ``_DESTRUCTIVE_KINDS`` allowlist.
    """
    kind = op.get("kind", "")
    if kind in _DESTRUCTIVE_KINDS:
        return True
    if kind == "alter_column":
        return op.get("not_null") is True or op.get("sql_type") is not None
    return False


def _is_op_non_transactional(op: dict[str, Any]) -> bool:
    """Return True if *op* cannot run inside a transaction block (W1-C)."""
    kind = op.get("kind", "")
    if kind in _NON_TRANSACTIONAL_KINDS:
        return True
    return kind == "add_index" and op.get("concurrently", False)


# W1-C: validated timeout strings for SET LOCAL. Only plain integers
# (milliseconds) or ``<number><unit>`` with unit s/ms/min/h are accepted so
# the value can never be a SQL injection vector.
_TIMEOUT_RE: re.Pattern[str] = re.compile(r"^\d+(ms|s|min|h)?$")


def _validate_timeout(value: str | None, name: str) -> None:
    """Validate a SET LOCAL timeout string against a strict pattern (W1-C)."""
    if value is None:
        return
    if not isinstance(value, str) or not _TIMEOUT_RE.match(value):
        raise FerrumMigrationError(
            f"Invalid {name}: {value!r}. Expected a number optionally followed by "
            "ms, s, min, or h (e.g. '5s', '100ms', '30s'). [FERR-M001]"
        )


# SQL type allowlist — only these tokens may appear in DDL type position.
# Prevents DDL injection if upstream metadata validation is bypassed.
_SQL_TYPE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "INT",
        "INT2",
        "INT4",
        "INT8",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "SERIAL",
        "BIGSERIAL",
        "FLOAT",
        "FLOAT4",
        "FLOAT8",
        "REAL",
        "DOUBLE PRECISION",
        "NUMERIC",
        "TEXT",
        "VARCHAR",
        "CHAR",
        "BOOLEAN",
        "BYTEA",
        "TIMESTAMPTZ",
        "TIMESTAMP",
        "DATE",
        "TIME",
        "UUID",
        "JSONB",
        "JSON",
        "INET",
        "VECTOR",
        "TSVECTOR",
        # PostgreSQL array types
        "TEXT[]",
        "INTEGER[]",
        "UUID[]",
        "FLOAT8[]",
    }
)

# Allowlist for FK ON DELETE actions — mirrors _ON_DELETE_ALLOWLIST in models.py.
_FK_ON_DELETE_ALLOWLIST: frozenset[str] = frozenset(
    {"CASCADE", "SET NULL", "RESTRICT", "SET DEFAULT", "NO ACTION"}
)

# Index access methods allowed in CREATE INDEX ... USING ...
_INDEX_USING_ALLOWLIST: frozenset[str] = frozenset(
    {"btree", "gin", "gist", "hash", "brin", "hnsw", "ivfflat"}
)

# Default value allowlist — only simple literals permitted.
_DEFAULT_VALUE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "NULL",
        "TRUE",
        "FALSE",
        "NOW()",
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "GEN_RANDOM_UUID()",
        "UUIDV7()",
        "0",
        "1",
        "''",
    }
)


def _normalize_column_default(default: Any) -> str:  # noqa: ANN401
    """Normalize a migration default value to its SQL literal token."""
    text = str(default)
    if text == "":
        return "''"
    return text


def _python_default_to_sql(*, value: Any, field_meta: FieldMeta) -> str | None:  # noqa: ANN401
    """Map a Python-side field default to an allowed SQL DEFAULT literal, if possible."""
    if value is None:
        return None
    field_type = field_meta.field_type
    if field_type in ("text", "varchar") and isinstance(value, str):
        if value == "":
            return "''"
        return None
    if field_type == "bool" and isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if field_type in ("int", "big_int") and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if field_type == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _quote_ident(name: str, dialect: str) -> str:
    """Quote a DDL identifier for the target dialect."""
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    if dialect == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


# Base type tokens permitted in MSSQL DDL after mapping (parameter portion,
# e.g. ``(MAX)`` / ``(10,2)``, is appended separately and not allowlisted here).
_MSSQL_TYPE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "INT",
        "BIGINT",
        "SMALLINT",
        "BIT",
        "FLOAT",
        "REAL",
        "NUMERIC",
        "DECIMAL",
        "NVARCHAR",
        "NCHAR",
        "VARBINARY",
        "UNIQUEIDENTIFIER",
        "DATETIMEOFFSET",
        "DATETIME2",
        "DATE",
        "TIME",
        "INT IDENTITY",
        "BIGINT IDENTITY",
        "SMALLINT IDENTITY",
    }
)

# Migration op kinds that are PostgreSQL-only and rejected on the MSSQL backend
# (thin parity: no RLS, extensions, stored functions, in-place column alters,
# or column rename DDL in v0.1).
_MSSQL_UNSUPPORTED_KINDS: frozenset[str] = frozenset(
    {
        "alter_column",
        "rename_column",
        "create_extension",
        "drop_extension",
        "enable_rls",
        "disable_rls",
        "create_policy",
        "drop_policy",
        "create_function",
        "drop_function",
    }
)


def _map_sql_type_mssql(sql_type: str) -> str:
    """Map a canonical SQL type to its T-SQL token (preserving any ``(...)``)."""
    upper = sql_type.upper()
    base = upper.split("(")[0].strip()
    paren = sql_type[sql_type.index("(") :] if "(" in sql_type else ""
    direct: dict[str, str] = {
        "SERIAL": "INT IDENTITY(1,1)",
        "SERIAL4": "INT IDENTITY(1,1)",
        "BIGSERIAL": "BIGINT IDENTITY(1,1)",
        "SERIAL8": "BIGINT IDENTITY(1,1)",
        "SMALLSERIAL": "SMALLINT IDENTITY(1,1)",
        "SERIAL2": "SMALLINT IDENTITY(1,1)",
        "BOOLEAN": "BIT",
        "BOOL": "BIT",
        "BYTEA": "VARBINARY(MAX)",
        "UUID": "UNIQUEIDENTIFIER",
        "TIMESTAMPTZ": "DATETIMEOFFSET",
        "TIMESTAMP": "DATETIME2",
        "TEXT": "NVARCHAR(MAX)",
        "JSONB": "NVARCHAR(MAX)",
        "JSON": "NVARCHAR(MAX)",
        "DOUBLE PRECISION": "FLOAT",
        "FLOAT8": "FLOAT",
        "FLOAT": "FLOAT",
        "FLOAT4": "REAL",
        "REAL": "REAL",
        "INT": "INT",
        "INT4": "INT",
        "INTEGER": "INT",
        "INT8": "BIGINT",
        "BIGINT": "BIGINT",
        "INT2": "SMALLINT",
        "SMALLINT": "SMALLINT",
        "DATE": "DATE",
        "TIME": "TIME",
    }
    if base in direct:
        return direct[base]
    if base in ("VARCHAR", "CHARACTER VARYING"):
        return f"NVARCHAR{paren}" if paren else "NVARCHAR(MAX)"
    if base in ("CHAR", "CHARACTER"):
        return f"NCHAR{paren}" if paren else "NCHAR(1)"
    if base in ("NUMERIC", "DECIMAL"):
        return f"NUMERIC{paren}"
    raise FerrumMigrationError(
        f"SQL type {sql_type!r} is not supported on the MSSQL backend (thin parity). [FERR-M001]"
    )


def _map_sql_type(sql_type: str, dialect: str) -> str:
    """Map canonical SQL types to dialect-specific DDL tokens."""
    upper = sql_type.upper()
    if dialect == "mssql":
        return _map_sql_type_mssql(sql_type)
    if dialect == "mysql" and upper.startswith("BOOLEAN"):
        return sql_type.replace("BOOLEAN", "TINYINT(1)").replace("boolean", "TINYINT(1)")
    if dialect == "mysql" and upper == "BYTEA":
        return "BLOB"
    if dialect == "sqlite" and upper == "BYTEA":
        return "BLOB"
    if dialect == "sqlite" and upper.startswith("TIMESTAMPTZ"):
        return sql_type.replace("TIMESTAMPTZ", "TEXT").replace("timestamptz", "TEXT")
    return sql_type


def _col_def(col: dict[str, Any], *, dialect: str = "postgres") -> str:
    """Build a column definition fragment for CREATE TABLE / ADD COLUMN.

    Security: identifiers are double-quoted; sql_type and default are
    validated against allowlists before interpolation into DDL.

    Parameterised types (e.g. ``VARCHAR(100)``, ``NUMERIC(10,2)``) are accepted:
    the base token before the first ``(`` is checked against the allowlist so the
    parameter portion is never interpolated without the token being whitelisted.

    For composite-PK tables ``primary_key`` may be ``False`` on individual columns
    (the table-level constraint is emitted by ``_op_to_sql`` for ``create_table``).
    """
    sql_type = _map_sql_type(col.get("sql_type", "TEXT"), dialect)
    base_type = sql_type.split("(")[0].upper()
    # Array types end with "[]"; strip that suffix before allowlist check.
    base_type_check = base_type.rstrip("[]") if base_type.endswith("[]") else base_type
    if dialect == "mssql":
        if base_type not in _MSSQL_TYPE_ALLOWLIST:
            raise FerrumMigrationError(f"Unsupported SQL type {sql_type!r}. [FERR-M001]")
    elif (
        base_type not in _SQL_TYPE_ALLOWLIST
        and base_type_check not in _SQL_TYPE_ALLOWLIST
        and dialect != "mysql"
    ):
        raise FerrumMigrationError(
            f"Unsupported SQL type {sql_type!r}. Only standard SQL types are allowed. [FERR-M001]"
        )
    if (
        dialect == "mysql"
        and base_type not in _SQL_TYPE_ALLOWLIST
        and base_type_check not in _SQL_TYPE_ALLOWLIST
        and base_type not in {"TINYINT", "BLOB", "LONGTEXT", "DATETIME"}
    ):
        raise FerrumMigrationError(f"Unsupported SQL type {sql_type!r}. [FERR-M001]")
    parts = [f"{_quote_ident(col['name'], dialect)} {sql_type.upper()}"]
    default = col.get("default")
    not_null = col.get("not_null") or not col.get("nullable", True)
    if not_null:
        if dialect == "sqlite" and default is None and col.get("kind") == "add_column":
            raise FerrumMigrationError(
                "SQLite does not allow ADD COLUMN NOT NULL without a DEFAULT. [FERR-M001]"
            )
        parts.append("NOT NULL")
    if default is not None:
        normalized = _normalize_column_default(default)
        if normalized.upper() not in _DEFAULT_VALUE_ALLOWLIST:
            raise FerrumMigrationError(
                f"Unsupported DEFAULT value {default!r}. "
                f"Only simple literals are allowed. [FERR-M001]"
            )
        parts.append(f"DEFAULT {normalized}")
    # Emit inline PRIMARY KEY only for single-column PKs. Composite PKs are emitted
    # as a separate table-level constraint in _op_to_sql.
    if col.get("primary_key") and not col.get("composite_pk"):
        parts.append("PRIMARY KEY")
    if col.get("unique"):
        parts.append("UNIQUE")
    if col.get("enum_check"):
        parts.append(str(col["enum_check"]))
    return " ".join(parts)


def _op_to_sql(op: dict[str, Any], *, dialect: str = "postgres") -> str:
    """Generate DDL SQL from a MigrationOp dict.

    All table/column/index names are double-quoted. Values are sourced
    exclusively from the Rust-generated plan JSON, which itself sources
    identifiers from model-metadata allowlists (AGENTS.md §2.9). This
    function must never receive user-supplied strings.

    Args:
        op: A migration operation dict with a ``kind`` key and operation-
            specific keys (``table``, ``name``, ``columns``, etc.).

    Returns:
        A complete DDL SQL statement string.

    Raises:
        FerrumMigrationError: If ``kind`` is unrecognised.
    """
    kind = op.get("kind", "")

    if dialect == "mssql" and kind in _MSSQL_UNSUPPORTED_KINDS:
        raise FerrumMigrationError(
            f"Migration op {kind!r} is not supported on the MSSQL backend "
            "(thin parity: no RLS, extensions, functions, column alter/rename). [FERR-M001]"
        )

    if kind == "create_table":
        table = op["table"]
        col_defs_list = [_col_def(c, dialect=dialect) for c in op.get("columns", [])]
        # Table-level composite PRIMARY KEY constraint: emitted when the op carries
        # a "composite_pk_columns" key (set by compute_plan for multi-PK models).
        composite_pk_cols: list[str] = op.get("composite_pk_columns", [])
        if composite_pk_cols:
            pk_cols_sql = ", ".join(_quote_ident(col, dialect) for col in composite_pk_cols)
            col_defs_list.append(f"PRIMARY KEY ({pk_cols_sql})")
        col_defs = ", ".join(col_defs_list)
        if dialect == "mssql":
            # T-SQL has no CREATE TABLE IF NOT EXISTS; guard on object existence.
            return (
                f"IF OBJECT_ID(N'{table}', N'U') IS NULL "
                f"CREATE TABLE {_quote_ident(table, dialect)} ({col_defs})"
            )
        sql = f"CREATE TABLE IF NOT EXISTS {_quote_ident(table, dialect)} ({col_defs})"
        if dialect == "mysql":
            sql += " ENGINE=InnoDB"
        return sql

    if kind == "drop_table":
        table = op["table"]
        return f"DROP TABLE IF EXISTS {_quote_ident(table, dialect)}"

    if kind == "add_column":
        table = op["table"]
        col = {**op, "kind": "add_column"}
        if dialect == "mssql":
            # T-SQL adds columns with ALTER TABLE ... ADD <coldef> (no COLUMN keyword).
            coldef = _col_def(col, dialect=dialect)
            return f"ALTER TABLE {_quote_ident(table, dialect)} ADD {coldef}"
        return (
            f"ALTER TABLE {_quote_ident(table, dialect)} "
            f"ADD COLUMN {_col_def(col, dialect=dialect)}"
        )

    if kind == "drop_column":
        table = op["table"]
        column = op["column"]
        if dialect == "sqlite":
            raise FerrumMigrationError(
                "SQLite does not support DROP COLUMN in Ferrum migrations. [FERR-M001]"
            )
        return (
            f"ALTER TABLE {_quote_ident(table, dialect)} "
            f"DROP COLUMN IF EXISTS {_quote_ident(column, dialect)}"
        )

    if kind == "alter_column":
        table = op["table"]
        column = op["column"]
        parts: list[str] = []
        sql_type = op.get("sql_type")
        if sql_type is not None:
            mapped = _map_sql_type(str(sql_type), dialect)
            if mapped.upper().split("(")[0].strip() not in _SQL_TYPE_ALLOWLIST:
                raise FerrumMigrationError(
                    f"SQL type {sql_type!r} is not in the migration allowlist. [FERR-M001]"
                )
            parts.append(f"ALTER COLUMN {_quote_ident(column, dialect)} TYPE {mapped}")
        if op.get("not_null") is True:
            parts.append(f"ALTER COLUMN {_quote_ident(column, dialect)} SET NOT NULL")
        elif op.get("not_null") is False:
            parts.append(f"ALTER COLUMN {_quote_ident(column, dialect)} DROP NOT NULL")
        default = op.get("default")
        if default is not None:
            default_token = _normalize_column_default(default)
            if default_token.upper() not in _DEFAULT_VALUE_ALLOWLIST:
                raise FerrumMigrationError(
                    f"Default value {default!r} is not in the migration allowlist. [FERR-M001]"
                )
            parts.append(
                f"ALTER COLUMN {_quote_ident(column, dialect)} SET DEFAULT {default_token}"
            )
        if op.get("drop_default"):
            parts.append(f"ALTER COLUMN {_quote_ident(column, dialect)} DROP DEFAULT")
        if not parts:
            raise FerrumMigrationError(
                "alter_column requires at least one of sql_type, not_null, default, drop_default."
            )
        if dialect != "postgres":
            raise FerrumMigrationError(
                "alter_column is only supported on PostgreSQL in Ferrum v0.1. [FERR-M001]"
            )
        inner = ", ".join(parts)
        return f"ALTER TABLE {_quote_ident(table, dialect)} {inner}"

    if kind == "rename_column":
        table = op["table"]
        from_col = op["from"]
        to_col = op["to"]
        if dialect == "mysql":
            return (
                f"ALTER TABLE {_quote_ident(table, dialect)} "
                f"RENAME COLUMN {_quote_ident(from_col, dialect)} "
                f"TO {_quote_ident(to_col, dialect)}"
            )
        return (
            f"ALTER TABLE {_quote_ident(table, dialect)} "
            f"RENAME COLUMN {_quote_ident(from_col, dialect)} "
            f"TO {_quote_ident(to_col, dialect)}"
        )

    if kind == "add_index":
        unique_kw = "UNIQUE " if op.get("unique") else ""
        name = op["name"]
        table = op["table"]
        columns = list(op.get("columns", []))
        opclasses = op.get("opclasses")
        cols = _index_columns_sql(columns, opclasses, dialect=dialect)
        using = op.get("using", "btree")
        if using not in _INDEX_USING_ALLOWLIST:
            raise FerrumMigrationError(f"Unsupported index access method {using!r}. [FERR-M001]")
        concurrently = op.get("concurrently", False)
        if dialect != "postgres" and concurrently:
            raise FerrumMigrationError(
                "CREATE INDEX CONCURRENTLY is PostgreSQL-only in Ferrum v0.1. [FERR-M001]"
            )
        if dialect == "mssql":
            # T-SQL has no CREATE INDEX IF NOT EXISTS / USING; the ledger ensures
            # each migration runs once, so an unguarded CREATE INDEX is safe.
            sql = (
                f"CREATE {unique_kw}INDEX {_quote_ident(name, dialect)} "
                f"ON {_quote_ident(table, dialect)} ({cols})"
            )
        elif concurrently:
            # W1-C: CREATE INDEX CONCURRENTLY cannot use IF EXISTS (PostgreSQL
            # rejects the combination) and cannot run inside a transaction block.
            # The ledger's replay guard prevents double-execution.
            sql = (
                f"CREATE {unique_kw}INDEX CONCURRENTLY {_quote_ident(name, dialect)} "
                f"ON {_quote_ident(table, dialect)} USING {using} ({cols})"
            )
        else:
            sql = (
                f"CREATE {unique_kw}INDEX IF NOT EXISTS {_quote_ident(name, dialect)} "
                f"ON {_quote_ident(table, dialect)}"
            )
            if dialect == "postgres":
                sql += f" USING {using} ({cols})"
            else:
                sql += f" ({cols})"
        where = op.get("where")
        if where:
            sql = f"{sql} WHERE {where}"
        return sql

    if kind == "drop_index":
        name = op["name"]
        if dialect in ("mysql", "mssql"):
            table = op.get("table", "")
            if table:
                return f"DROP INDEX {_quote_ident(name, dialect)} ON {_quote_ident(table, dialect)}"
        return f"DROP INDEX IF EXISTS {_quote_ident(name, dialect)}"

    if kind == "add_fk":
        on_delete = str(op.get("on_delete", "CASCADE")).upper()
        if on_delete not in _FK_ON_DELETE_ALLOWLIST:
            raise FerrumMigrationError(f"Unsupported ON DELETE action {on_delete!r}. [FERR-M001]")
        return (
            f"ALTER TABLE {_quote_ident(op['table'], dialect)} "
            f"ADD CONSTRAINT {_quote_ident(op['name'], dialect)} "
            f"FOREIGN KEY ({_quote_ident(op['column'], dialect)}) "
            f"REFERENCES {_quote_ident(op['ref_table'], dialect)} "
            f"({_quote_ident(op['ref_column'], dialect)})"
            f" ON DELETE {on_delete}"
        )

    if kind == "drop_fk":
        return (
            f"ALTER TABLE {_quote_ident(op['table'], dialect)} "
            f"DROP CONSTRAINT IF EXISTS {_quote_ident(op['name'], dialect)}"
        )

    if kind == "raw_sql":
        # raw_sql ops with safe=False must have been blocked at the
        # requires_confirmation gate before reaching this point.
        return op["sql"]

    # ------------------------------------------------------------------
    # Extension operations
    # ------------------------------------------------------------------

    if kind == "create_extension":
        name = op["name"]
        schema_part = f" SCHEMA {_quote_ident(op['schema'], dialect)}" if op.get("schema") else ""
        return f"CREATE EXTENSION IF NOT EXISTS {_quote_ident(name, dialect)}{schema_part}"

    if kind == "drop_extension":
        name = op["name"]
        cascade_part = " CASCADE" if op.get("cascade") else ""
        return f"DROP EXTENSION IF EXISTS {_quote_ident(name, dialect)}{cascade_part}"

    # ------------------------------------------------------------------
    # Row Level Security operations
    # ------------------------------------------------------------------

    if kind == "enable_rls":
        table = op["table"]
        if op.get("force"):
            # W1-C / W0-B ta-16: FORCE alone leaves ``relrowsecurity`` false and
            # makes every policy a silent no-op. Emit ENABLE then FORCE so the
            # table is both RLS-enabled and owner-enforced in a single op.
            return (
                f"ALTER TABLE {_quote_ident(table, dialect)} ENABLE ROW LEVEL SECURITY; "
                f"ALTER TABLE {_quote_ident(table, dialect)} FORCE ROW LEVEL SECURITY"
            )
        return f"ALTER TABLE {_quote_ident(table, dialect)} ENABLE ROW LEVEL SECURITY"

    if kind == "disable_rls":
        table = op["table"]
        return f"ALTER TABLE {_quote_ident(table, dialect)} DISABLE ROW LEVEL SECURITY"

    if kind == "create_policy":
        # Security note: using and check_expr are developer-supplied SQL expressions
        # from migration files — not from user input. They are emitted verbatim.
        name = op["name"]
        table = op["table"]
        using_expr = op["using"]
        command = str(op.get("command", "ALL")).upper()
        _valid_commands: frozenset[str] = frozenset({"ALL", "SELECT", "INSERT", "UPDATE", "DELETE"})
        if command not in _valid_commands:
            raise FerrumMigrationError(
                f"Unsupported policy command {command!r}. "
                f"Expected one of: {', '.join(sorted(_valid_commands))}. [FERR-M001]"
            )
        sql = f"CREATE POLICY {_quote_ident(name, dialect)} ON {_quote_ident(table, dialect)}"
        if command != "ALL":
            sql += f" FOR {command}"
        role = op.get("role")
        if role:
            sql += f" TO {_quote_ident(role, dialect)}"
        sql += f" USING ({using_expr})"
        check_expr = op.get("check_expr")
        if check_expr:
            sql += f" WITH CHECK ({check_expr})"
        return sql

    if kind == "drop_policy":
        name = op["name"]
        table = op["table"]
        return (
            f"DROP POLICY IF EXISTS {_quote_ident(name, dialect)} ON {_quote_ident(table, dialect)}"
        )

    # ------------------------------------------------------------------
    # Stored function operations
    # ------------------------------------------------------------------

    if kind == "create_function":
        # Security note: body is a developer-supplied full DDL statement from a
        # migration file — never from user input. Emitted verbatim.
        return op["body"]

    if kind == "drop_function":
        name = op["name"]
        args = op.get("args", "")
        return f"DROP FUNCTION IF EXISTS {_quote_ident(name, dialect)}({args})"

    if kind in ("create_full_text_index", "drop_full_text_index", "create_full_text_catalog"):
        from ferrum.migrations.fts import op_to_sql as fts_op_to_sql

        return fts_op_to_sql(op, dialect=dialect)

    raise FerrumMigrationError(f"Unknown migration op kind: {kind!r}. [FERR-M001]")


def _print_plan(plan: dict[str, Any]) -> None:
    """Print a human-readable dry-run summary to stdout."""
    version = plan.get("version", "unknown")
    name = plan.get("name", "unnamed")
    ops = plan.get("ops", [])
    print(f"[ferrum migrate] dry-run: {name} (version={version})")
    for op in ops:
        kind = op.get("kind", "unknown")
        table = op.get("table", "")
        line = f"  - {kind}"
        if table:
            line += f" {table}"
        print(line)
    print(f"[ferrum migrate] {len(ops)} ops total (not applied)")


def _resolve_gin_opclasses(
    metadata: ModelMetadata,
    field_names: tuple[str, ...],
    *,
    using: str,
) -> list[str] | None:
    """Return per-column GIN operator classes when PostgreSQL requires them.

    Plain ``TEXT`` columns need ``gin_trgm_ops`` (``pg_trgm``). ``TSVECTOR`` uses
    the default operator class and needs no suffix.
    """
    if using != "gin":
        return None
    field_by_name = {f.name: f for f in metadata.fields}
    opclasses: list[str] = []
    any_required = False
    for field_name in field_names:
        field_meta = field_by_name[field_name]
        if field_meta.field_type == "text":
            opclasses.append("gin_trgm_ops")
            any_required = True
        else:
            opclasses.append("")
    return opclasses if any_required else None


def _index_columns_sql(
    columns: list[str],
    opclasses: list[str] | None,
    *,
    dialect: str = "postgres",
) -> str:
    """Format index column list, optionally with per-column operator classes."""
    parts: list[str] = []
    for i, column in enumerate(columns):
        opclass = opclasses[i] if opclasses and i < len(opclasses) else ""
        if opclass and dialect == "postgres":
            parts.append(f"{_quote_ident(column, dialect)} {opclass}")
        else:
            parts.append(_quote_ident(column, dialect))
    return ", ".join(parts)


def _field_to_col_def(field_meta: FieldMeta, *, is_pk: bool) -> dict[str, Any]:
    """Convert a ``FieldMeta`` to a column-def dict for the plan JSON.

    Security: all names come from model-metadata allowlists — never user input.
    The ``not_null`` key is set to ``True`` when the field is not nullable so that
    ``_col_def`` emits the ``NOT NULL`` constraint.

    Uses ``FieldMeta.sql_type`` (which honours ``max_length``, ``max_digits``,
    ``decimal_places``) rather than a fixed Python-type → SQL mapping so that
    parameterised column types (``VARCHAR(n)``, ``NUMERIC(p,s)``) are emitted
    correctly.
    """
    default = field_meta.db_default
    if default is None:
        default = _python_default_to_sql(value=field_meta.python_default, field_meta=field_meta)
    return {
        "name": field_meta.column_name,
        "sql_type": field_meta.sql_type,
        "not_null": not field_meta.nullable,
        "default": default,
        "primary_key": is_pk,
        "unique": field_meta.unique,
    }


def _autodiff_existing_table(
    ops: list[dict[str, Any]],
    metadata: ModelMetadata,
    table: str,
    existing_col_names: set[str],
    existing_col_attrs: dict[str, ColumnState],
    existing_indexes: dict[str, IndexState],
) -> None:
    """Emit index and column-attribute diff ops for an existing table.

    Called from ``compute_plan`` only when a rich ``SchemaState`` is available.
    Appends ``add_index``, ``drop_index``, and ``alter_column`` ops to *ops*.

    Security: all identifiers come from model metadata allowlists or
    ``SchemaState`` that was itself built from prior migration ops — never user
    input.  Default values are checked against ``_DEFAULT_VALUE_ALLOWLIST`` by
    ``_op_to_sql`` when the plan is applied.

    Out of scope: sql_type changes, renames, drops (unchanged v0.1 rule).
    ``SET NOT NULL`` is emitted but classified destructive by ``AlterColumn``.
    """
    # Build the set of desired indexes for this table.
    # Key: index name → op dict for add_index.
    desired_indexes: dict[str, dict[str, Any]] = {}

    # db_index=True fields on *existing* columns (new columns are handled above).
    for f in metadata.fields:
        if f.column_name in existing_col_names and f.db_index:
            idx_name = f"idx_{table}_{f.name}"
            desired_indexes[idx_name] = {
                "kind": "add_index",
                "table": table,
                "name": idx_name,
                "columns": [f.column_name],
                "unique": False,
                "using": "btree",
            }

    # Meta.indexes entries.
    for index in metadata.indexes:
        # Only consider indexes whose columns all exist already.
        column_names = [
            next((f.column_name for f in metadata.fields if f.name == fn), fn)
            for fn in index.fields
        ]
        idx_op: dict[str, Any] = {
            "kind": "add_index",
            "table": table,
            "name": index.name,
            "columns": column_names,
            "unique": index.unique,
            "using": index.using,
        }
        if index.where is not None:
            idx_op["where"] = index.where
        opclasses = _resolve_gin_opclasses(metadata, index.fields, using=index.using)
        if opclasses is not None:
            idx_op["opclasses"] = opclasses
        desired_indexes[index.name] = idx_op

    # Emit add_index for desired indexes not yet in state.
    for idx_name, idx_op in desired_indexes.items():
        if idx_name not in existing_indexes:
            ops.append(idx_op)

    # Emit drop_index for Ferrum-tracked indexes that are no longer desired.
    for idx_name, idx_state in existing_indexes.items():
        if idx_state.table == table and idx_name not in desired_indexes:
            ops.append({"kind": "drop_index", "name": idx_name, "table": table})

    # Column default / nullability autodiff.
    for f in metadata.fields:
        if f.column_name not in existing_col_attrs:
            continue  # New column — already emitted as add_column above.
        col_state = existing_col_attrs[f.column_name]

        desired_not_null = not f.nullable
        desired_default = f.db_default  # already normalized in FieldMeta

        # Compare case-insensitively to handle legacy lowercase defaults in state.
        state_default_upper = col_state.default.upper() if col_state.default else None
        desired_default_upper = desired_default.upper() if desired_default else None

        alter_kwargs: dict[str, Any] = {}

        if desired_not_null != col_state.not_null:
            alter_kwargs["not_null"] = desired_not_null

        if desired_default_upper != state_default_upper:
            if desired_default is not None:
                alter_kwargs["default"] = desired_default
            elif col_state.default is not None:
                alter_kwargs["drop_default"] = True

        if alter_kwargs:
            ops.append(
                {
                    "kind": "alter_column",
                    "table": table,
                    "column": f.column_name,
                    **alter_kwargs,
                }
            )


def compute_plan(
    model_classes: list[type[Model]] | None = None,
    existing_tables: dict[str, list[str]] | SchemaState | None = None,
    *,
    conn: Connection | None = None,
    models: list[type[Model]] | None = None,
) -> dict[str, Any]:
    """Compute a migration plan from model classes against the current DB schema.

    Compares ``model_classes`` against ``existing_tables`` and emits:
    - ``create_table`` ops for absent tables (with ``add_index`` for ``db_index`` fields
      and ``Meta.indexes`` entries).
    - ``add_column`` ops for columns present in the model but absent from the DB.
    - When ``existing_tables`` is a :class:`SchemaState` (returned by
      ``_build_existing_state``): also emits ``add_index`` / ``drop_index`` for
      index changes on existing columns, and ``alter_column`` for default /
      nullability changes.

    This is a v0.1 additive schema diff.  Column type changes, renames, and
    drops are out of scope.

    Args:
        model_classes: Ferrum ``Model`` subclasses to inspect.  Their
            ``ModelMetadata`` (built at class-definition time) is the sole source
            of table/column names; no user input reaches SQL identifiers.
        existing_tables: Either a ``dict[str, list[str]]`` (table → column names,
            legacy/backward-compat), a :class:`SchemaState` (richer projected
            state from ``_build_existing_state``), or ``None`` for a fresh database.
        conn: Reserved for a future DB-introspection path.  ``None`` means use
            the supplied/static ``existing_tables`` mapping.
        models: Keyword alias for ``model_classes`` used by CLI/tests.

    Returns:
        A plan dict matching the ``MigrationPlan`` JSON schema expected by
        ``apply()``.  Suitable for ``json.dumps()`` and passing to ``apply()``.
    """
    del conn
    if models is not None:
        if model_classes is not None:
            raise TypeError("Pass either model_classes or models, not both.")
        model_classes = models
    if model_classes is None:
        raise TypeError("compute_plan() requires model_classes or models.")

    # Normalize existing_tables to SchemaState for uniform handling.
    # When the caller passes a legacy dict[str, list[str]], we build a SchemaState
    # with column-name-only entries (no default/not_null/index info) and suppress
    # the index/default autodiff so existing callers are unaffected.
    has_rich_state: bool
    schema_state: SchemaState
    if existing_tables is None:
        schema_state = SchemaState()
        has_rich_state = True
    elif isinstance(existing_tables, SchemaState):
        schema_state = existing_tables
        has_rich_state = True
    else:
        # Legacy dict[str, list[str]] — columns only, no index/default state.
        schema_state = SchemaState(
            tables={
                tbl: {col: ColumnState(sql_type="") for col in cols}
                for tbl, cols in existing_tables.items()
            }
        )
        has_rich_state = False

    ops: list[dict[str, Any]] = []
    timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")

    # Pre-build a model-name → table-name lookup so FK target tables are resolved
    # from actual metadata rather than a naive snake_case conversion.
    meta_by_name: dict[str, Any] = {cls.__name__: cls.get_metadata() for cls in model_classes}

    # Track M2M join tables already emitted to avoid duplicate CREATE TABLE ops
    # when both sides of the relationship appear in model_classes.
    emitted_m2m_tables: set[str] = set()

    for cls in model_classes:
        # ModelMetadata is built once at class-definition time and is read-only.
        metadata = cls.get_metadata()
        table = metadata.table_name

        if table not in schema_state.tables:
            # Detect composite PK: more than one field has pk=True.
            pk_fields_list = [f for f in metadata.fields if f.pk]
            is_composite_pk = len(pk_fields_list) > 1

            # For composite PKs, mark each PK column with composite_pk=True so
            # _col_def skips the inline PRIMARY KEY (the constraint is table-level).
            col_defs: list[dict[str, Any]] = []
            for f in metadata.fields:
                cd = _field_to_col_def(f, is_pk=f.pk)
                if is_composite_pk and f.pk:
                    cd = {**cd, "primary_key": False, "composite_pk": True}
                # Enum columns get an inline CHECK constraint in the DDL.
                if f.field_type == "enum" and f.enum_values:
                    quoted_vals = ", ".join(f"'{v}'" for v in f.enum_values)
                    # Store check expression; _col_def doesn't handle it, so we embed
                    # it as a suffix directly on the column entry via a post-pass.
                    check = f"CHECK ({_quote_ident(f.column_name, 'postgres')} IN ({quoted_vals}))"
                    cd = {**cd, "enum_check": check}
                col_defs.append(cd)

            create_op: dict[str, Any] = {
                "kind": "create_table",
                "table": table,
                "columns": col_defs,
            }
            if is_composite_pk:
                create_op["composite_pk_columns"] = [f.column_name for f in pk_fields_list]
            ops.append(create_op)
            # Emit AddIndex ops for db_index=True fields (after create_table).
            for f in metadata.fields:
                if f.db_index:
                    ops.append(
                        {
                            "kind": "add_index",
                            "table": table,
                            "name": f"idx_{table}_{f.name}",
                            "columns": [f.column_name],
                            "unique": False,
                            "using": "btree",
                        }
                    )
            for index in metadata.indexes:
                column_names = [
                    next(f.column_name for f in metadata.fields if f.name == field_name)
                    for field_name in index.fields
                ]
                index_op: dict[str, Any] = {
                    "kind": "add_index",
                    "table": table,
                    "name": index.name,
                    "columns": column_names,
                    "unique": index.unique,
                    "using": index.using,
                    "where": index.where,
                }
                opclasses = _resolve_gin_opclasses(metadata, index.fields, using=index.using)
                if opclasses is not None:
                    index_op["opclasses"] = opclasses
                ops.append(index_op)

            # Emit AddForeignKey ops for FK / OneToOne relations on new tables.
            for rel in metadata.relations:
                if rel.kind in ("fk", "one_to_one"):
                    target_meta = meta_by_name.get(rel.to_model)
                    target_table = target_meta.table_name if target_meta else rel.to_model.lower()
                    constraint_name = f"fk_{table}_{rel.db_column}"
                    ops.append(
                        {
                            "kind": "add_fk",
                            "table": table,
                            "name": constraint_name,
                            "column": rel.db_column,
                            "ref_table": target_table,
                            "ref_column": "id",
                            "on_delete": rel.on_delete or "CASCADE",
                        }
                    )
                elif rel.kind == "m2m" and rel.through_table not in emitted_m2m_tables:
                    through = rel.through_table
                    if through is None:
                        raise FerrumMigrationError(
                            f"M2M relation {rel.field_name!r} on {table!r} "
                            "has no through_table. [FERR-M001]"
                        )
                    emitted_m2m_tables.add(through)

                    target_meta = meta_by_name.get(rel.to_model)
                    target_table = target_meta.table_name if target_meta else rel.to_model.lower()
                    owner_col = f"{table}_id"
                    target_col = f"{target_table}_id"

                    if through not in schema_state.tables:
                        ops.append(
                            {
                                "kind": "create_table",
                                "table": through,
                                "columns": [
                                    {
                                        "name": "id",
                                        "sql_type": "BIGSERIAL",
                                        "primary_key": True,
                                        "not_null": True,
                                    },
                                    {
                                        "name": owner_col,
                                        "sql_type": "INTEGER",
                                        "not_null": True,
                                    },
                                    {
                                        "name": target_col,
                                        "sql_type": "INTEGER",
                                        "not_null": True,
                                    },
                                ],
                            }
                        )
                        ops.append(
                            {
                                "kind": "add_fk",
                                "table": through,
                                "name": f"fk_{through}_{owner_col}",
                                "column": owner_col,
                                "ref_table": table,
                                "ref_column": "id",
                                "on_delete": "CASCADE",
                            }
                        )
                        ops.append(
                            {
                                "kind": "add_fk",
                                "table": through,
                                "name": f"fk_{through}_{target_col}",
                                "column": target_col,
                                "ref_table": target_table,
                                "ref_column": "id",
                                "on_delete": "CASCADE",
                            }
                        )
        else:
            # Table already exists — add new columns and (when rich state is
            # available) diff indexes and column default/nullability.
            existing_col_attrs = schema_state.tables.get(table, {})
            existing_col_names: set[str] = set(existing_col_attrs.keys())

            for f in metadata.fields:
                if f.column_name not in existing_col_names:
                    col_def = _field_to_col_def(f, is_pk=False)
                    ops.append(
                        {
                            "kind": "add_column",
                            "table": table,
                            **col_def,
                        }
                    )
                    # Emit AddIndex for newly added db_index=True columns.
                    if f.db_index:
                        ops.append(
                            {
                                "kind": "add_index",
                                "table": table,
                                "name": f"idx_{table}_{f.name}",
                                "columns": [f.column_name],
                                "unique": False,
                                "using": "btree",
                            }
                        )

            if has_rich_state:
                _autodiff_existing_table(
                    ops,
                    metadata,
                    table,
                    existing_col_names,
                    existing_col_attrs,
                    schema_state.indexes,
                )

    return {
        "version": 1,
        "name": f"auto_{timestamp}",
        "ops": ops,
        "destructive": False,
        "requires_confirmation": False,
    }


async def _lock_holder_diagnostics(raw_conn: Any) -> str:  # noqa: ANN401
    """Return a sanitized diagnostic string about who holds the migration advisory lock.

    Queries ``pg_locks`` + ``pg_stat_activity`` for granted advisory locks
    in the Ferrum namespace. Returns only PID, application_name, and state —
    never query text, bound values, or row data. PostgreSQL only.
    """
    try:
        rows = await raw_conn.fetch(
            "SELECT l.pid, COALESCE(a.application_name, '') AS app, a.state "
            "FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype = 'advisory' AND l.granted = true "
            "AND l.classid = $1 AND l.objid = $2",
            ADVISORY_LOCK_KEY_1,
            ADVISORY_LOCK_KEY_2,
        )
    except Exception:
        return ""
    if not rows:
        return ""
    parts: list[str] = []
    for r in rows:
        pid = r.get("pid", r[0]) if isinstance(r, dict) else r[0]
        app = r.get("app", r[1]) if isinstance(r, dict) else r[1]
        state = r.get("state", r[2]) if isinstance(r, dict) else r[2]
        parts.append(f"pid={pid} app={app!r} state={state!r}")
    return "; ".join(parts)


def _split_ops_by_phase(
    ops: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ops into (pre_tx, tx, post_tx) phases (W1-C).

    Non-transactional ops (CREATE EXTENSION, CREATE INDEX CONCURRENTLY) must
    run outside the transaction block. They form contiguous pre-tx and/or
    post-tx phases. Transactional ops form a single middle phase wrapped in
    one transaction with the ledger write.

    Raises ``FerrumMigrationError`` if non-transactional ops are interspersed
    with transactional ops (an invalid plan structure that cannot be split
    into contiguous phases).
    """
    pre: list[dict[str, Any]] = []
    tx: list[dict[str, Any]] = []
    post: list[dict[str, Any]] = []
    phase = "pre"
    for op in ops:
        non_tx = _is_op_non_transactional(op)
        if non_tx:
            if phase == "pre":
                pre.append(op)
            elif phase == "tx":
                # First non-tx op after tx ops starts the post phase.
                phase = "post"
                post.append(op)
            else:  # post
                post.append(op)
        else:
            if phase == "pre":
                phase = "tx"
            elif phase == "post":
                raise FerrumMigrationError(
                    "Invalid migration plan: transactional op after non-transactional "
                    "op. Non-transactional ops must form contiguous pre- or post-tx "
                    "phases, not interspersed with transactional ops. [FERR-M001]"
                )
            tx.append(op)
    return pre, tx, post


async def apply(
    conn: Connection,
    plan_json: str,
    *,
    dry_run: bool = True,
    confirm: bool = False,
    env: str = "development",
    token: str | None = None,
    lock_timeout: str | None = None,
    statement_timeout: str | None = None,
) -> MigrationResult:
    """Apply a Rust-generated migration plan JSON to the database.

    W1-C: on PostgreSQL, apply acquires a transaction-scoped advisory lock,
    checks the ledger, runs all transactional ops + the ledger write in one
    atomic transaction on one pinned connection, then runs any
    non-transactional ops (CREATE INDEX CONCURRENTLY, CREATE EXTENSION)
    outside the transaction block. Non-PostgreSQL backends keep
    best-effort autocommit-per-op.

    Args:
        conn: An open Ferrum ``Connection`` (pool must be open).
        plan_json: JSON string produced by ``MigrationPlan.to_json()`` in the
            Rust core.  Identifiers in this payload come from model-metadata
            allowlists (AGENTS.md §2.9).
        dry_run: When ``True`` (default), print the plan and return without
            touching the database.
        confirm: Required for destructive operations and non-development
            environments.  Never auto-applied.
        env: The target environment name.  Non-``"development"`` values require
            ``confirm=True`` (MIG-5).
        token: Optional confirmation token.  When provided alongside
            ``confirm=True``, it is validated against the plan digest using
            ``verify_token``.  An invalid or mismatched token raises
            ``FerrumMigrationError`` before any SQL is executed (MIG-2).
        lock_timeout: Optional ``lock_timeout`` (e.g. ``"5s"``) applied as
            ``SET LOCAL`` inside the transaction. When the advisory lock
            cannot be acquired within this budget, PostgreSQL raises
            SQLSTATE 55P03 and Ferrum surfaces a lock-holder diagnostic.
            PostgreSQL only.
        statement_timeout: Optional ``statement_timeout`` (e.g. ``"30s"``)
            applied as ``SET LOCAL`` inside the transaction. PostgreSQL only.

    Returns:
        ``MigrationResult`` describing what was (or would have been) applied.

    Raises:
        FerrumMigrationError: Safety gate not satisfied (destructive without
            confirm, non-dev without confirm, invalid token, unknown op kind,
            replay guard, lock contention, or invalid plan structure).
    """
    plan = json.loads(plan_json)
    ops: list[dict[str, Any]] = plan.get("ops", [])

    if dry_run:
        _print_plan(plan)
        return MigrationResult(applied=False, ops_count=len(ops), dry_run=True)

    plan_digest = hashlib.sha256(plan_json.encode()).hexdigest()

    # Token gate: validate the confirmation token against the plan digest before
    # any SQL is executed.  Checked before destructive/env gates so a bad token
    # is rejected immediately, regardless of what other flags are set (MIG-2).
    if confirm and token is not None and not verify_token(plan_json, token):
        raise FerrumMigrationError("Token validation failed. [FERR-M001]")

    # MIG-2: destructive gate — independently scan ops, never trust the
    # `requires_confirmation` flag from plan JSON (a crafted JSON could lie).
    # W1-C: uses _is_op_destructive so alter_column SET NOT NULL / type
    # narrowing also hits the confirm gate.
    is_destructive = any(_is_op_destructive(op) for op in ops)
    if (is_destructive or plan.get("requires_confirmation")) and not confirm:
        raise FerrumMigrationError(
            "Migration requires explicit confirmation. "
            "Pass confirm=True or use ferrum migrations apply --confirm."
        )

    # MIG-5: environment gate — non-dev environments require explicit confirmation.
    if env != "development" and not confirm:
        raise FerrumMigrationError("Non-development apply requires --confirm flag.")

    dialect = conn.dialect
    description = str(plan.get("name", ""))

    if dialect == "postgres":
        return await _apply_postgres(
            conn,
            ops,
            plan_digest=plan_digest,
            description=description,
            env=env,
            lock_timeout=lock_timeout,
            statement_timeout=statement_timeout,
        )

    # Non-PostgreSQL backends: best-effort autocommit-per-op (thin parity).
    return await _apply_thin_parity(conn, ops, plan_digest, description=description, env=env)


async def _apply_postgres(
    conn: Connection,
    ops: list[dict[str, Any]],
    *,
    plan_digest: str,
    description: str,
    env: str,
    lock_timeout: str | None,
    statement_timeout: str | None,
) -> MigrationResult:
    """PostgreSQL apply: advisory-locked, transactional, atomic ledger write."""
    pre_ops, tx_ops, post_ops = _split_ops_by_phase(ops)

    # Pre-tx non-transactional phase (autocommit, no ledger yet).
    # If a pre-tx op fails, the ledger is not written and re-running is safe.
    driver = conn._require_driver()
    for op in pre_ops:
        kind = op.get("kind", "unknown")
        table = op.get("table", "")
        label = f"{kind} {table}".rstrip()
        print(f"[ferrum migrate] applying (pre-tx): {label}")
        sql = _op_to_sql(op, dialect="postgres")
        await driver.execute(sql)

    # Transactional phase: pin one connection, acquire advisory lock, check
    # ledger, run ops, write ledger — all atomic.
    _validate_timeout(lock_timeout, "lock_timeout")
    _validate_timeout(statement_timeout, "statement_timeout")
    async with conn.acquire() as raw_conn, raw_conn.transaction():
        # Apply lock_timeout / statement_timeout as SET LOCAL (tx-scoped).
        # Values are validated by _validate_timeout — plain number+unit only.
        if lock_timeout is not None:
            await raw_conn.execute(f"SET LOCAL lock_timeout = {lock_timeout}")
        if statement_timeout is not None:
            await raw_conn.execute(f"SET LOCAL statement_timeout = {statement_timeout}")

        # Advisory lock — serializes concurrent migrators on one connection.
        # Auto-released on commit/rollback (pg_advisory_xact_lock).
        try:
            await raw_conn.execute(advisory_lock_sql(), ADVISORY_LOCK_KEY_1, ADVISORY_LOCK_KEY_2)
        except Exception as exc:
            diag = await _lock_holder_diagnostics(raw_conn)
            sqlstate = ""
            if hasattr(exc, "sqlstate"):
                sqlstate = f" (SQLSTATE {exc.sqlstate})"
            suffix = f" — lock holder: {diag}" if diag else ""
            raise FerrumMigrationError(
                f"Failed to acquire migration advisory lock{sqlstate}. "
                f"Another migrator may be running.{suffix} [FERR-M001]"
            ) from None

        # Ensure ledger table exists on this pinned connection.
        await raw_conn.execute(
            "CREATE TABLE IF NOT EXISTS ferrum_migrations ("
            "id BIGSERIAL PRIMARY KEY, digest TEXT NOT NULL UNIQUE, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "environment TEXT NOT NULL DEFAULT 'development', description TEXT)"
        )

        # Replay guard: reject if this plan was already applied.
        if await is_applied_on_conn(raw_conn, plan_digest, dialect="postgres"):
            raise FerrumMigrationError("Migration plan has already been applied. [FERR-M003]")

        # Run transactional ops on the pinned connection.
        for op_index, op in enumerate(tx_ops):
            kind = op.get("kind", "unknown")
            table = op.get("table", "")
            label = f"{kind} {table}".rstrip()
            print(f"[ferrum migrate] applying: {label}")
            sql = _op_to_sql(op, dialect="postgres")
            try:
                await raw_conn.execute(sql)
            except FerrumMigrationError:
                raise
            except Exception as exc:
                raise migration_op_failure_from(
                    migration_name=description,
                    op_index=op_index,
                    op=op,
                    exc=exc,
                ) from None

        # Atomic ledger write — same transaction, same connection.
        await record_applied_on_conn(
            raw_conn,
            plan_digest,
            environment=env,
            description=description,
            dialect="postgres",
        )

    # Post-tx non-transactional phase (autocommit, ledger already written).
    # If a post-tx op fails, the ledger says "applied" but the op is missing —
    # documented partial-failure semantics. Re-running skips the migration.
    for op in post_ops:
        kind = op.get("kind", "unknown")
        table = op.get("table", "")
        label = f"{kind} {table}".rstrip()
        print(f"[ferrum migrate] applying (post-tx): {label}")
        sql = _op_to_sql(op, dialect="postgres")
        try:
            await driver.execute(sql)
        except FerrumMigrationError:
            raise
        except Exception as exc:
            raise FerrumMigrationError(
                f"Post-transaction op failed after ledger commit ({kind} {table}): "
                f"{type(exc).__name__}. The migration is recorded as applied; "
                f"the failed op must be reconciled manually. [FERR-M001]"
            ) from None

    return MigrationResult(applied=True, ops_count=len(ops), dry_run=False)


async def _apply_thin_parity(
    conn: Connection,
    ops: list[dict[str, Any]],
    plan_digest: str,
    *,
    description: str,
    env: str,
) -> MigrationResult:
    """Non-PostgreSQL backends: best-effort autocommit-per-op (thin parity).

    No advisory lock, no transactional wrap, no atomic ledger. Matches the
    pre-W1-C behavior for MySQL / SQLite / MSSQL.
    """
    driver = conn._require_driver()
    dialect = conn.dialect
    for op in ops:
        kind = op.get("kind", "unknown")
        table = op.get("table", "")
        label = f"{kind} {table}".rstrip()
        print(f"[ferrum migrate] applying: {label}")
        sql = _op_to_sql(op, dialect=dialect)
        await driver.execute(sql)

    await ensure_ledger(conn)
    await record_applied(
        conn,
        plan_digest,
        environment=env,
        description=description,
    )

    return MigrationResult(applied=True, ops_count=len(ops), dry_run=False)


def migration_op_failure_from(
    *,
    migration_name: str,
    op_index: int,
    op: dict[str, Any],
    exc: Exception,
) -> FerrumMigrationError:
    """Build a FerrumMigrationError for a failed transactional op (W1-C).

    Delegates to errors.migration_op_failure for the sanitized message.
    The transaction will be rolled back by the ``async with`` exit.
    """
    from ferrum.errors import migration_op_failure

    return migration_op_failure(
        action="apply",
        migration_name=migration_name,
        op_index=op_index,
        op=op,
        exc=exc,
    )


# ---------------------------------------------------------------------------
# W3-A: Migration graph, reversibility, data migrations, offline SQL
# ---------------------------------------------------------------------------
# The primitives below are read-only graph views and developer-supplied
# data-migration runners. They do NOT replace the W1-C apply path
# (``apply()`` / ``_apply_postgres()`` / ``_apply_thin_parity()``) — the
# graph layer is consumed by tests, audit tooling, and downstream code
# that needs to reason about ordering, status, and recovery without a
# live CLI run. Actual DDL application continues to flow through W1-C.
# ---------------------------------------------------------------------------


# Allowed transaction policies for :class:`DataMigration`.
_DATA_MIGRATION_POLICIES: frozenset[str] = frozenset({"required", "none"})


@dataclass
class MigrationStatus:
    """Per-migration status row returned by :meth:`MigrationGraph.status`.

    Fields:
        name: Migration file stem (e.g. ``"0001_create_note"``).
        state: One of ``"applied"``, ``"pending"``, ``"checksum_mismatch"``,
            or ``"unknown"`` (no ledger reachable).
        digest: The on-disk content digest, or ``""`` if the file is absent.
        stored_digest: The digest recorded in the ledger, or ``""`` if not
            applied. Equal to ``digest`` when the on-disk file matches the
            applied version.
        reversible: ``True`` iff the migration declares non-empty
            ``reverse_operations``.
        has_destructive_reverse: ``True`` iff any reverse operation is
            classified ``"destructive"`` (forces the ``--confirm`` gate on
            revert).
    """

    name: str
    state: str
    digest: str = ""
    stored_digest: str = ""
    reversible: bool = False
    has_destructive_reverse: bool = False


class MigrationGraph:
    """Read-only view over a migration dependency graph plus ledger state.

    Wraps :func:`loader.scan` output and queries the ledger to answer:
    - What is the deterministic topological order?
    - Which migrations are pending / applied / checksum-mismatched?
    - Which migrations must be applied to reach a target (upgrade plan)?
    - Which applied migrations must be reverted to reach a target
      (downgrade plan)?
    - What recovery guidance applies to the current state?

    The graph never acquires a connection for apply/revert — those operations
    stay owned by W1-C (``cli/migrate_cmd.py`` / ``cli/revert_cmd.py``). The
    graph only reads the ledger via the supplied :class:`Connection`.

    Security: identifiers come from migration-file names (developer-controlled,
    validated by :func:`loader.scan` against the ``NNNN_slug.py`` pattern).
    No user input reaches this class.
    """

    def __init__(
        self,
        modules: list[MigrationModule],
        *,
        conn: Connection | None = None,
    ) -> None:
        """Build a graph from a list of migration modules.

        Args:
            modules: Migration modules in any order; the graph stores them in
                deterministic topological order (Kahn's algorithm with
                name-sorted tie-breaks).
            conn: Optional open :class:`Connection`. When supplied, status /
                upgrade / downgrade queries consult the ledger. When
                ``None``, every migration is reported as ``"unknown"`` and
                upgrade / downgrade plans treat all migrations as pending.
        """
        # Importing here avoids a circular import at module load time.
        from ferrum.migrations.loader import topological_sort

        self._modules: list[MigrationModule] = topological_sort(modules)
        self._by_name: dict[str, MigrationModule] = {m.name: m for m in self._modules}
        self._conn = conn

    # ------------------------------------------------------------------
    # Read-only graph views
    # ------------------------------------------------------------------

    @property
    def modules(self) -> list[MigrationModule]:
        """Return the modules in deterministic topological order."""
        return list(self._modules)

    def names(self) -> list[str]:
        """Return migration names in topological order."""
        return [m.name for m in self._modules]

    def topological_order(self) -> list[str]:
        """Alias of :meth:`names` for graph-query callers."""
        return self.names()

    def detect_cycle(self) -> list[str] | None:
        """Return names participating in a cycle, or ``None`` if acyclic.

        The constructor already raises on cycles via
        :func:`loader.topological_sort`; this method is a defensive check
        for callers that construct a graph from an unsorted list and want
        to introspect without raising. In practice it always returns
        ``None`` for a successfully constructed graph.
        """
        from ferrum.migrations.loader import detect_cycle as _detect_cycle

        return _detect_cycle(self._modules)

    def dependencies_of(self, name: str) -> list[str]:
        """Return the declared dependencies of *name* (a copy)."""
        self._require_known(name)
        return list(self._by_name[name].dependencies)

    def _require_known(self, name: str) -> None:
        if name not in self._by_name:
            raise FerrumMigrationError(f"Migration {name!r} is not in this graph. [FERR-M001]")

    # ------------------------------------------------------------------
    # Ledger-backed queries
    # ------------------------------------------------------------------

    async def _digest_for(self, module: MigrationModule) -> str:
        """Return the on-disk content digest for *module*."""
        # Imported here to avoid a circular import.
        from ferrum.migrations.ledger import compute_digest

        content = module.path.read_text(encoding="utf-8")
        return compute_digest(module.name, content)

    async def _applied_digests(self) -> dict[str, str]:
        """Return ``{migration_name: stored_digest}`` for every applied row."""
        if self._conn is None:
            return {}
        from ferrum.migrations.ledger import find_applied_digest_by_name

        result: dict[str, str] = {}
        for module in self._modules:
            stored = await find_applied_digest_by_name(self._conn, module.name)
            if stored is not None:
                result[module.name] = stored
        return result

    async def status(self) -> list[MigrationStatus]:
        """Return per-migration status in topological order.

        Requires a connection; without one every migration is ``"unknown"``.
        """
        from ferrum.migrations.base import (
            is_reversible,
            reverse_classifications,
        )

        applied = await self._applied_digests()
        rows: list[MigrationStatus] = []
        for module in self._modules:
            digest = await self._digest_for(module)
            stored = applied.get(module.name, "")
            if self._conn is None:
                state = "unknown"
            elif stored == "":
                state = "pending"
            elif stored != digest:
                state = "checksum_mismatch"
            else:
                state = "applied"
            rev_classifications = reverse_classifications(module.migration)
            rows.append(
                MigrationStatus(
                    name=module.name,
                    state=state,
                    digest=digest,
                    stored_digest=stored,
                    reversible=is_reversible(module.migration),
                    has_destructive_reverse=any(c == "destructive" for c in rev_classifications),
                )
            )
        return rows

    async def upgrade_plan(self, target: str | None = None) -> list[MigrationModule]:
        """Return pending migrations to apply, in topological order, up to *target*.

        Args:
            target: Optional migration name. When supplied, the plan stops
                *after* ``target`` (inclusive) — i.e. it returns every pending
                migration whose topological position is ≤ ``target``'s.
                When ``None`` (default) every pending migration is returned.

        Raises:
            FerrumMigrationError: if *target* is not in the graph.

        The returned list is read-only — applying it still flows through the
        W1-C CLI/``apply()`` path with advisory lock + atomic ledger.
        """
        if target is not None:
            self._require_known(target)
        applied = await self._applied_digests()
        plan: list[MigrationModule] = []
        for module in self._modules:
            if module.name in applied:
                continue
            plan.append(module)
            if target is not None and module.name == target:
                break
        if target is not None and not any(m.name == target for m in plan):
            # Target is already applied — return an empty plan.
            return []
        return plan

    async def downgrade_plan(self, target: str | None = None) -> list[MigrationModule]:
        """Return applied migrations to revert, in reverse topological order.

        Args:
            target: Optional migration name. When supplied, revert every
                applied migration *after* ``target`` (exclusive). The target
                itself is not reverted. When ``None``, revert only the most
                recently applied migration.

        Raises:
            FerrumMigrationError: if *target* is not in the graph, or if a
                migration in the plan is irreversible.

        Mirrors the ``--target`` semantics of ``ferrum revert``: revert
        everything applied after the target, most recent first.
        """
        if target is not None:
            self._require_known(target)
        from ferrum.migrations.base import is_reversible

        applied = await self._applied_digests()
        # Preserve topological order while filtering to applied ones.
        applied_in_order = [m for m in self._modules if m.name in applied]
        if not applied_in_order:
            return []
        if target is None:
            to_revert = [applied_in_order[-1]]
        else:
            target_idx: int | None = None
            for i, module in enumerate(applied_in_order):
                if module.name == target:
                    target_idx = i
                    break
            if target_idx is None:
                # Target is not applied — nothing to revert.
                return []
            to_revert = list(reversed(applied_in_order[target_idx + 1 :]))
        irreversible = [m.name for m in to_revert if not is_reversible(m.migration)]
        if irreversible:
            raise FerrumMigrationError(
                f"Cannot downgrade past irreversible migration(s): "
                f"{', '.join(irreversible)}. Add reverse_operations or revert "
                f"to a point before them. [FERR-M001]"
            )
        return to_revert

    async def recovery_guidance(self) -> list[str]:
        """Return human-readable recovery hints for the current graph state.

        Detects:
        - Checksum mismatches (applied migration edited on disk).
        - Migrations applied out of order (a migration whose dependencies are
          not all applied).
        - Irreversible migrations among the most-recent applied set (blocks
          ``ferrum revert``).

        Each hint is a single-line actionable string. No secrets, DSNs, or
        bound values appear in the output.
        """
        from ferrum.migrations.base import is_reversible

        hints: list[str] = []
        statuses = await self.status()
        by_name = {s.name: s for s in statuses}
        for s in statuses:
            if s.state == "checksum_mismatch":
                hints.append(
                    f"Migration {s.name!r}: on-disk file was edited after apply. "
                    "Restore the original file or revert and re-apply."
                )
        # Out-of-order: applied migration whose dependency is not applied.
        applied_names = {s.name for s in statuses if s.state == "applied"}
        for module in self._modules:
            if module.name not in applied_names:
                continue
            for dep in module.dependencies:
                dep_status = by_name.get(dep)
                if dep_status is None:
                    hints.append(
                        f"Migration {module.name!r} depends on {dep!r} which is "
                        "not in the graph (missing file or name typo)."
                    )
                elif dep_status.state != "applied":
                    hints.append(
                        f"Migration {module.name!r} is applied but its dependency "
                        f"{dep!r} is {dep_status.state!r}. Restore the dependency "
                        "from the ledger or revert the dependent migration."
                    )
        # Irreversible most-recent applied: blocks plain ``ferrum revert``.
        applied_in_order = [
            m for m in self._modules if by_name.get(m.name) and by_name[m.name].state == "applied"
        ]
        if applied_in_order:
            head = applied_in_order[-1]
            if not is_reversible(head.migration):
                hints.append(
                    f"Most-recent applied migration {head.name!r} is irreversible "
                    "(empty reverse_operations). ``ferrum revert`` will refuse; "
                    "add reverse_operations or restore to a known-good snapshot."
                )
        return hints


# ---------------------------------------------------------------------------
# Data migrations: developer-supplied callables with explicit tx policy
# ---------------------------------------------------------------------------


class DataMigration:
    """Developer-supplied data-migration callable with an explicit transaction policy.

    A data migration is a Python callback that runs alongside DDL operations
    in a migration file. It carries an explicit ``transaction_policy`` so the
    runner knows whether to wrap it in a transaction.

    Security:
    - Data migrations are **developer-authored code** in migration files.
      They are never imported or executed automatically from untrusted
      files. The :class:`DataMigration` base refuses to run when
      ``is_trusted`` is ``False``; subclasses inherit the default ``True``.
    - The callable receives a :class:`ConnectionLike` (Connection or
      Transaction). It must not receive user input.
    - The runner does not inspect or log the callable's source.

    Transaction policies:
    - ``"required"`` (default): the runner wraps the callable in
      :meth:`Connection.transaction`. On PostgreSQL this pins one connection
      and rolls back on any exception or cancellation. On thin-parity
      backends (no transaction support) the runner raises
      :class:`FerrumMigrationError` rather than silently running without
      the requested atomicity.
    - ``"none"``: the callable runs in autocommit on PostgreSQL (each
      statement commits independently). Suitable for non-transactional
      operations like ``CREATE INDEX CONCURRENTLY`` or large backfills
      that cannot fit in one transaction. The caller is responsible for
      idempotency; a mid-flight failure may leave partial state.
    """

    transaction_policy: ClassVar[str] = "required"
    is_trusted: ClassVar[bool] = True

    async def run(self, conn: ConnectionLike) -> None:
        """Override with the data-migration body. Receives a Connection-like."""
        raise NotImplementedError("DataMigration subclasses must override run(conn). [FERR-M001]")


async def run_data_migration(
    conn: Connection,
    migration: DataMigration,
) -> None:
    """Execute a :class:`DataMigration` honoring its declared transaction policy.

    The migration must be a :class:`DataMigration` subclass with
    ``is_trusted = True``. Untrusted instances are refused — this is the
    structural guard against automatic source-code execution from
    untrusted files.

    Args:
        conn: An open :class:`Connection`. The runner may open a transaction
            on it (policy ``"required"``) or use it directly (policy
            ``"none"``).
        migration: The :class:`DataMigration` instance to run.

    Raises:
        FerrumMigrationError: if ``migration.is_trusted`` is ``False``, the
            transaction policy is unknown, the connection's driver does not
            support transactions (for ``"required"``), or the callable raises.
    """
    if not getattr(migration, "is_trusted", False):
        raise FerrumMigrationError(
            "Refusing to run untrusted data migration. Data migrations must be "
            "developer-authored subclasses of DataMigration with "
            "is_trusted=True. [FERR-M001]"
        )
    policy = getattr(migration, "transaction_policy", "required")
    if policy not in _DATA_MIGRATION_POLICIES:
        raise FerrumMigrationError(
            f"Unknown data-migration transaction_policy {policy!r}. Expected one "
            f"of {sorted(_DATA_MIGRATION_POLICIES)}. [FERR-M001]"
        )

    if policy == "required":
        # Wrap in a transaction. Connection.transaction() raises if the
        # driver lacks transaction support (thin-parity backends).
        async with conn.transaction() as tx:
            try:
                await migration.run(tx)
            except FerrumMigrationError:
                raise
            except Exception as exc:
                raise FerrumMigrationError(
                    f"Data migration {type(migration).__name__} failed inside its "
                    f"transaction and will be rolled back: {type(exc).__name__}. "
                    "[FERR-M001]"
                ) from None
    else:  # policy == "none"
        try:
            await migration.run(conn)
        except FerrumMigrationError:
            raise
        except Exception as exc:
            raise FerrumMigrationError(
                f"Data migration {type(migration).__name__} failed outside a "
                f"transaction (policy='none'); partial state may remain: "
                f"{type(exc).__name__}. [FERR-M001]"
            ) from None


# ---------------------------------------------------------------------------
# Offline SQL generation with checksums and phase annotations
# ---------------------------------------------------------------------------


@dataclass
class OfflineSqlPhase:
    """One phase of an offline migration SQL bundle.

    Attributes:
        phase: ``"pre_tx"``, ``"tx"``, or ``"post_tx"`` (matches
            :func:`_split_ops_by_phase`).
        kind: The migration op kind (e.g. ``"create_index"``).
        table: The target table when applicable, else ``""``.
        sql: The rendered SQL statement.
    """

    phase: str
    kind: str
    table: str
    sql: str


@dataclass
class OfflineSqlMigration:
    """Offline SQL bundle for one migration.

    Attributes:
        name: Migration file stem.
        digest: sha256 content digest (matches ``ledger.compute_digest``).
        reversible: ``True`` iff the migration declares non-empty
            ``reverse_operations``.
        has_destructive: ``True`` iff any forward op classifies destructive.
        phases: List of :class:`OfflineSqlPhase` for the forward operations,
            in declared order, each tagged with its tx phase.
    """

    name: str
    digest: str
    reversible: bool
    has_destructive: bool
    phases: list[OfflineSqlPhase] = field(default_factory=list)


@dataclass
class OfflineSqlPlan:
    """Offline SQL bundle for a list of migrations, with per-file checksums.

    The plan contains no DB I/O and no bound values; only DDL identifiers
    (from model-metadata allowlists, per AGENTS.md §2.9). It is safe to
    serialize, log, or hand to an operator for review.

    Attributes:
        migrations: Per-migration bundles in topological order.
        dialect: Dialect the SQL was rendered for.
    """

    migrations: list[OfflineSqlMigration] = field(default_factory=list)
    dialect: str = "postgres"


def generate_offline_sql(
    modules: list[MigrationModule],
    *,
    dialect: str = "postgres",
) -> OfflineSqlPlan:
    """Render offline SQL for *modules* with per-file checksums and phase annotations.

    Does not touch the database. The output is suitable for code review, CI
    artifacts, or operator pre-apply audit. Each migration's digest matches
    what the ledger would record (``ledger.compute_digest``), so an operator
    can compare the offline bundle to the post-apply ledger.

    Args:
        modules: Migration modules in any order; they are sorted
            topologically before rendering.
        dialect: Target SQL dialect (default ``"postgres"``).

    Returns:
        An :class:`OfflineSqlPlan` with one :class:`OfflineSqlMigration` per
        module, each carrying its checksum and phase-annotated SQL.
    """
    from ferrum.migrations.base import is_reversible
    from ferrum.migrations.ledger import compute_digest
    from ferrum.migrations.loader import topological_sort

    ordered = topological_sort(modules)
    plan = OfflineSqlPlan(dialect=dialect)
    for module in ordered:
        content = module.path.read_text(encoding="utf-8")
        digest = compute_digest(module.name, content)
        forward_ops = [op.to_op_dict() for op in module.migration.operations]
        pre_ops, tx_ops, post_ops = _split_ops_by_phase(forward_ops)
        phases: list[OfflineSqlPhase] = []
        for phase_name, phase_ops in (
            ("pre_tx", pre_ops),
            ("tx", tx_ops),
            ("post_tx", post_ops),
        ):
            for op in phase_ops:
                kind = op.get("kind", "unknown")
                table = op.get("table", "")
                sql = _op_to_sql(op, dialect=dialect)
                phases.append(OfflineSqlPhase(phase=phase_name, kind=kind, table=table, sql=sql))
        has_destructive = any(_is_op_destructive(op) for op in forward_ops)
        plan.migrations.append(
            OfflineSqlMigration(
                name=module.name,
                digest=digest,
                reversible=is_reversible(module.migration),
                has_destructive=has_destructive,
                phases=phases,
            )
        )
    return plan
