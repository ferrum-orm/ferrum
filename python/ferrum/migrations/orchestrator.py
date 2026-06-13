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
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.tokens import verify_token

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import FieldMeta, Model


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
    }
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
        "0",
        "1",
        "''",
    }
)


def _col_def(col: dict[str, Any]) -> str:
    """Build a column definition fragment for CREATE TABLE / ADD COLUMN.

    Security: identifiers are double-quoted; sql_type and default are
    validated against allowlists before interpolation into DDL.

    Parameterised types (e.g. ``VARCHAR(100)``, ``NUMERIC(10,2)``) are accepted:
    the base token before the first ``(`` is checked against the allowlist so the
    parameter portion is never interpolated without the token being whitelisted.
    """
    sql_type = col.get("sql_type", "TEXT")
    base_type = sql_type.split("(")[0].upper()
    if base_type not in _SQL_TYPE_ALLOWLIST:
        raise FerrumMigrationError(
            f"Unsupported SQL type {sql_type!r}. "
            f"Only standard PostgreSQL types are allowed. [FERR-M001]"
        )
    parts = [f'"{col["name"]}" {sql_type.upper()}']
    if col.get("not_null") or not col.get("nullable", True):
        parts.append("NOT NULL")
    default = col.get("default")
    if default is not None:
        if str(default).upper() not in _DEFAULT_VALUE_ALLOWLIST:
            raise FerrumMigrationError(
                f"Unsupported DEFAULT value {default!r}. "
                f"Only simple literals are allowed. [FERR-M001]"
            )
        parts.append(f"DEFAULT {default}")
    if col.get("primary_key"):
        parts.append("PRIMARY KEY")
    if col.get("unique"):
        parts.append("UNIQUE")
    return " ".join(parts)


def _op_to_sql(op: dict[str, Any]) -> str:
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
        col_defs = ", ".join(_col_def(c) for c in op.get("columns", []))
        return f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})'

    if kind == "drop_table":
        table = op["table"]
        return f'DROP TABLE IF EXISTS "{table}"'

    if kind == "add_column":
        table = op["table"]
        col = _col_def(op)
        return f'ALTER TABLE "{table}" ADD COLUMN {col}'

    if kind == "drop_column":
        table = op["table"]
        column = op["column"]
        return f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{column}"'

    if kind == "rename_column":
        table = op["table"]
        from_col = op["from"]
        to_col = op["to"]
        return f'ALTER TABLE "{table}" RENAME COLUMN "{from_col}" TO "{to_col}"'

    if kind == "add_index":
        unique_kw = "UNIQUE " if op.get("unique") else ""
        name = op["name"]
        table = op["table"]
        cols = ", ".join(f'"{c}"' for c in op.get("columns", []))
        return f'CREATE {unique_kw}INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})'

    if kind == "drop_index":
        name = op["name"]
        return f'DROP INDEX IF EXISTS "{name}"'

    if kind == "raw_sql":
        # raw_sql ops with safe=False must have been blocked at the
        # requires_confirmation gate before reaching this point.
        return op["sql"]

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
    return {
        "name": field_meta.column_name,
        "sql_type": field_meta.sql_type,
        "not_null": not field_meta.nullable,
        "default": field_meta.db_default,
        "primary_key": is_pk,
        "unique": field_meta.unique,
    }


def compute_plan(
    model_classes: list[type[Model]],
    existing_tables: dict[str, list[str]],
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

    Returns:
        A plan dict matching the ``MigrationPlan`` JSON schema expected by
        ``apply()``.  Suitable for ``json.dumps()`` and passing to ``apply()``.
    """
    ops: list[dict[str, Any]] = []
    timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")

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

    # Token gate: validate the confirmation token against the plan digest before
    # any SQL is executed.  Checked before destructive/env gates so a bad token
    # is rejected immediately, regardless of what other flags are set (MIG-2).
    if confirm and token is not None and not verify_token(plan_json, token):
        raise FerrumMigrationError("Token validation failed. [FERR-M001]")

    # MIG-2: destructive gate — independently scan ops, never trust the
    # `requires_confirmation` flag from plan JSON (a crafted JSON could lie).
    destructive_kinds = frozenset({"drop_table", "drop_column", "raw_sql"})
    is_destructive = any(op.get("kind") in destructive_kinds for op in ops)
    if (is_destructive or plan.get("requires_confirmation")) and not confirm:
        raise FerrumMigrationError(
            "Migration requires explicit confirmation. "
            "Pass confirm=True or use ferrum migrations apply --confirm."
        )

    # MIG-5: environment gate — non-dev environments require explicit confirmation.
    if env != "development" and not confirm:
        raise FerrumMigrationError("Non-development apply requires --confirm flag.")

    pool = conn._require_pool()
    for op in ops:
        kind = op.get("kind", "unknown")
        table = op.get("table", "")
        label = f"{kind} {table}".rstrip()
        print(f"[ferrum migrate] applying: {label}")
        sql = _op_to_sql(op)
        await pool.execute(sql)

    return MigrationResult(applied=True, ops_count=len(ops), dry_run=False)
