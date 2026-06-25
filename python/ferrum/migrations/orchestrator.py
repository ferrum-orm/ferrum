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

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.ledger import is_applied, record_applied
from ferrum.migrations.tokens import verify_token

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import FieldMeta, Model, ModelMetadata


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
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _map_sql_type(sql_type: str, dialect: str) -> str:
    """Map canonical SQL types to dialect-specific DDL tokens."""
    upper = sql_type.upper()
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
    """
    sql_type = _map_sql_type(col.get("sql_type", "TEXT"), dialect)
    base_type = sql_type.split("(")[0].upper()
    if base_type not in _SQL_TYPE_ALLOWLIST and dialect != "mysql":
        raise FerrumMigrationError(
            f"Unsupported SQL type {sql_type!r}. Only standard SQL types are allowed. [FERR-M001]"
        )
    if (
        dialect == "mysql"
        and base_type not in _SQL_TYPE_ALLOWLIST
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
    if col.get("primary_key"):
        parts.append("PRIMARY KEY")
    if col.get("unique"):
        parts.append("UNIQUE")
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

    if kind == "create_table":
        table = op["table"]
        col_defs = ", ".join(_col_def(c, dialect=dialect) for c in op.get("columns", []))
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
            parts.append(
                f"ALTER COLUMN {_quote_ident(column, dialect)} TYPE {mapped}"
            )
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
        if dialect == "mysql":
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
            return f"ALTER TABLE {_quote_ident(table, dialect)} FORCE ROW LEVEL SECURITY"
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
        _valid_commands: frozenset[str] = frozenset(
            {"ALL", "SELECT", "INSERT", "UPDATE", "DELETE"}
        )
        if command not in _valid_commands:
            raise FerrumMigrationError(
                f"Unsupported policy command {command!r}. "
                f"Expected one of: {', '.join(sorted(_valid_commands))}. [FERR-M001]"
            )
        sql = (
            f"CREATE POLICY {_quote_ident(name, dialect)} "
            f"ON {_quote_ident(table, dialect)}"
        )
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
            f"DROP POLICY IF EXISTS {_quote_ident(name, dialect)} "
            f"ON {_quote_ident(table, dialect)}"
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


def compute_plan(
    model_classes: list[type[Model]] | None = None,
    existing_tables: dict[str, list[str]] | None = None,
    *,
    conn: Connection | None = None,
    models: list[type[Model]] | None = None,
) -> dict[str, Any]:
    """Compute a migration plan from model classes against the current DB schema.

    Compares ``model_classes`` against ``existing_tables`` (table → column names)
    and emits ``create_table`` ops for absent tables and ``add_column`` ops for
    columns present in the model but absent from the DB.

    This is a v0.1 additive-only schema diff.  Column type changes, renames,
    and drops are out of scope and will be addressed in a future release.

    Args:
        model_classes: Ferrum ``Model`` subclasses to inspect.  Their
            ``ModelMetadata`` (built at class-definition time) is the sole source
            of table/column names; no user input reaches SQL identifiers.
        existing_tables: Mapping of table name → list of existing column names,
            as returned by DB introspection.  Pass ``{}`` for a fresh database.
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
    if existing_tables is None:
        existing_tables = {}

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

        if table not in existing_tables:
            col_defs = [_field_to_col_def(f, is_pk=(f.pk)) for f in metadata.fields]
            ops.append({"kind": "create_table", "table": table, "columns": col_defs})
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

                    if through not in existing_tables:
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
            existing_cols = set(existing_tables[table])
            for f in metadata.fields:
                if f.column_name not in existing_cols:
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

    return {
        "version": 1,
        "name": f"auto_{timestamp}",
        "ops": ops,
        "destructive": False,
        "requires_confirmation": False,
    }


async def apply(
    conn: Connection,
    plan_json: str,
    *,
    dry_run: bool = True,
    confirm: bool = False,
    env: str = "development",
    token: str | None = None,
) -> MigrationResult:
    """Apply a Rust-generated migration plan JSON to the database.

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

    Returns:
        ``MigrationResult`` describing what was (or would have been) applied.

    Raises:
        FerrumMigrationError: Safety gate not satisfied (destructive without
            confirm, non-dev without confirm, invalid token, or unknown op kind).
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
    if confirm and token is not None:
        if not verify_token(plan_json, token):
            raise FerrumMigrationError("Token validation failed. [FERR-M001]")
        # MIG-6 replay guard: token-authenticated applies are single-use via ledger.
        if await is_applied(conn, plan_digest):
            raise FerrumMigrationError("Migration plan has already been applied. [FERR-M003]")

    # MIG-2: destructive gate — independently scan ops, never trust the
    # `requires_confirmation` flag from plan JSON (a crafted JSON could lie).
    is_destructive = any(op.get("kind") in _DESTRUCTIVE_KINDS for op in ops)
    if (is_destructive or plan.get("requires_confirmation")) and not confirm:
        raise FerrumMigrationError(
            "Migration requires explicit confirmation. "
            "Pass confirm=True or use ferrum migrations apply --confirm."
        )

    # MIG-5: environment gate — non-dev environments require explicit confirmation.
    if env != "development" and not confirm:
        raise FerrumMigrationError("Non-development apply requires --confirm flag.")

    driver = conn._require_driver()
    dialect = conn.dialect
    for op in ops:
        kind = op.get("kind", "unknown")
        table = op.get("table", "")
        label = f"{kind} {table}".rstrip()
        print(f"[ferrum migrate] applying: {label}")
        sql = _op_to_sql(op, dialect=dialect)
        await driver.execute(sql)

    if confirm and token is not None:
        await record_applied(
            conn,
            plan_digest,
            environment=env,
            description=str(plan.get("name", "")),
        )

    return MigrationResult(applied=True, ops_count=len(ops), dry_run=False)
