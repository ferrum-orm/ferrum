"""Read-only PostgreSQL schema-fidelity comparison.

Numbered SQL migrations remain authoritative. This module only compares
selected registered model metadata with ``information_schema`` and never emits
or applies DDL.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ferrum.registry import all_models

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import FieldMeta, Model, ModelMetadata

_SYSTEM_TABLES: frozenset[str] = frozenset({"ferrum_migrations"})


@dataclass(frozen=True)
class FieldDifference:
    """Actionable difference for one model-backed column."""

    field: str
    column: str
    kind: str
    expected: str
    actual: str
    guidance: str


@dataclass(frozen=True)
class PrimaryKeyDifference:
    """Primary-key column-order mismatch for one table."""

    expected: tuple[str, ...]
    actual: tuple[str, ...]
    guidance: str


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
        return "\n".join(lines)


async def _fetch_postgres_schema(
    conn: Connection,
    *,
    schema: str,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, tuple[str, ...]]]:
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
            "max_length": values[5],
            "numeric_precision": values[6],
            "numeric_scale": values[7],
        }
    primary_keys: dict[str, list[tuple[int, str]]] = {}
    for row in pk_rows:
        table, column, position = _row_values(
            row,
            ("table_name", "column_name", "ordinal_position"),
        )
        primary_keys.setdefault(str(table), []).append((int(position), str(column)))
    return tables, {
        table: tuple(column for _, column in sorted(columns))
        for table, columns in primary_keys.items()
    }


def _row_values(row: Any, keys: tuple[str, ...]) -> tuple[Any, ...]:  # noqa: ANN401
    if isinstance(row, Mapping):
        return tuple(row[key] for key in keys)
    return tuple(row)


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


async def detect_drift(
    conn: Connection,
    models: Iterable[type[Model] | str] | None = None,
    *,
    schema: str = "public",
    exclude_tables: Iterable[str] = (),
    auth_tables: Iterable[str] = (),
    langgraph_tables: Iterable[str] = (),
    include_unmapped_tables: bool = False,
) -> DriftReport:
    """Compare selected registered model metadata with live PostgreSQL schema.

    ``auth_tables`` and ``langgraph_tables`` are explicit carve-outs, kept
    separate in the signature so callers must document those ownership
    boundaries at the call site. They are treated identically to
    ``exclude_tables``. Unselected live tables are ignored unless
    ``include_unmapped_tables=True``.
    """
    selected_models = _resolve_models(models)
    excluded = frozenset(
        _SYSTEM_TABLES | set(exclude_tables) | set(auth_tables) | set(langgraph_tables)
    )
    selected_metadata: tuple[ModelMetadata, ...] = tuple(
        model.get_metadata()
        for model in selected_models
        if model.get_metadata().table_name not in excluded
    )
    live_state, live_primary_keys = await _fetch_postgres_schema(conn, schema=schema)
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

    has_drift = bool(
        missing_tables
        or extra_tables
        or missing_columns
        or extra_columns
        or field_differences
        or primary_key_differences
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
    )
