"""Extended migration autodiff for Ferrum.

Extends the base autodiff in :func:`orchestrator.compute_plan` with detection
for:

- Column type changes (always classified destructive per W1-C — never guesses
  a "safe" widening; the developer must confirm the apply).
- Column renames (require explicit developer hints via *rename_hints*; never
  guessed — missing intent raises :class:`FerrumMigrationError`).
- Foreign-key constraint add/drop on *existing* tables (the base ``compute_plan``
  only emits ``add_fk`` for *new* tables).
- Index attribute changes (drop + re-add when ``unique``, ``using``, ``where``,
  or ``columns`` changed on an existing index).
- Full-text index add/drop on existing and new tables.

The module also provides :func:`build_extended_state`, which replays *all*
migration op kinds (including ``rename_column``, ``add_fk``/``drop_fk``,
``create_extension``/``drop_extension``, ``enable_rls``/``disable_rls``,
``create_policy``/``drop_policy``, ``create_function``/``drop_function``,
``create_full_text_index``/``drop_full_text_index``) into an
:class:`AutodiffSchemaState`. This enables round-trip schema-state replay
tests for every op kind — not just the model-declarable subset.

Security invariants (AGENTS.md §2.9, §3):

- All identifiers come from model-metadata allowlists or prior migration ops
  (which themselves source from metadata allowlists) — never user input.
- Destructive classification is consistent with W1-C
  (:func:`orchestrator._is_op_destructive`,
  :attr:`operations.AlterColumn.classification`): any ``alter_column`` with
  ``sql_type`` set is destructive (type narrowing). The autodiff never marks a
  type change as "safe" — the developer must confirm the apply.
- Ambiguous renames are never guessed. Missing rename intent raises
  :class:`FerrumMigrationError` *before* any SQL is emitted.
- No raw SQL escape hatches (AGENTS.md §2.9). This module emits only operation
  dicts in the exact shape consumed by :func:`orchestrator._op_to_sql`.

This module is import-only with respect to ``orchestrator.py``,
``operations.py``, ``base.py``, ``loader.py``, ``tokens.py`` (W1-C/W3-A own
those — complete). It does not modify them.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import TYPE_CHECKING, Any

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.orchestrator import (
    ColumnState,
    IndexState,
    SchemaState,
    _is_op_destructive,
    compute_plan,
)

if TYPE_CHECKING:
    from ferrum.migrations.loader import MigrationModule
    from ferrum.models import Model, ModelMetadata


# ---------------------------------------------------------------------------
# Extended schema state — tracks FKs, extensions, RLS, policies, functions, FTS
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ForeignKeyState:
    """Projected state of a single FK constraint after replaying migration ops."""

    table: str
    column: str
    ref_table: str
    ref_column: str = "id"
    on_delete: str = "CASCADE"


@dataclasses.dataclass
class PolicyState:
    """Projected state of a single RLS policy after replaying migration ops."""

    name: str
    table: str
    using: str
    check_expr: str | None = None
    command: str = "ALL"
    role: str | None = None


@dataclasses.dataclass
class FullTextIndexState:
    """Projected state of a single full-text index after replaying migration ops."""

    table: str
    columns: list[str] = dataclasses.field(default_factory=list)
    config: str | None = None


@dataclasses.dataclass
class AutodiffSchemaState(SchemaState):
    """Extended schema state tracking FKs, extensions, RLS, functions, FTS.

    Subclasses :class:`SchemaState` (which tracks ``tables`` and ``indexes``)
    and adds tracking for foreign keys, extensions, RLS state, policies,
    functions, and full-text indexes. Subclassing — not modifying
    :class:`SchemaState` — keeps :mod:`orchestrator` untouched.

    Because :func:`orchestrator.compute_plan` accepts a ``SchemaState`` via
    ``isinstance``, an ``AutodiffSchemaState`` IS-A ``SchemaState`` and the
    base autodiff path runs unchanged when an extended state is passed.
    """

    foreign_keys: dict[str, ForeignKeyState] = dataclasses.field(default_factory=dict)
    extensions: dict[str, str | None] = dataclasses.field(default_factory=dict)
    rls_enabled: dict[str, bool] = dataclasses.field(default_factory=dict)
    rls_forced: dict[str, bool] = dataclasses.field(default_factory=dict)
    policies: dict[str, PolicyState] = dataclasses.field(default_factory=dict)
    functions: dict[str, str] = dataclasses.field(default_factory=dict)
    full_text_indexes: dict[str, FullTextIndexState] = dataclasses.field(default_factory=dict)


RenameHints = dict[str, dict[str, str]]
"""Per-table rename hints: ``{table_name: {old_column_name: new_column_name}}``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_type(sql_type: str | None) -> str:
    """Normalize a SQL type string for case-insensitive comparison."""
    if sql_type is None:
        return ""
    return sql_type.upper().strip()


def _normalize_default(value: str | None) -> str | None:
    """Normalize a default value to canonical uppercase for consistent comparison."""
    if value is None:
        return None
    from ferrum.models import _normalize_db_default

    return _normalize_db_default(value)


# ---------------------------------------------------------------------------
# Schema-state replay — handles ALL migration op kinds
# ---------------------------------------------------------------------------


def build_extended_state(migrations: list[MigrationModule]) -> AutodiffSchemaState:
    """Replay prior migration operations to produce an extended schema state.

    Handles all migration op kinds: ``create_table``, ``add_column``,
    ``drop_table``, ``drop_column``, ``add_index``, ``drop_index``,
    ``alter_column`` (type / default / not_null), ``rename_column``,
    ``add_fk``, ``drop_fk``, ``create_extension``, ``drop_extension``,
    ``enable_rls``, ``disable_rls``, ``create_policy``, ``drop_policy``,
    ``create_function``, ``drop_function``, ``create_full_text_index``,
    ``drop_full_text_index``.

    This is a static reconstruction — no database connection is used. All
    identifiers come from developer-authored migration files (themselves
    sourced from model-metadata allowlists at generation time), never from
    user input.

    Args:
        migrations: Migration modules in topological order (typically from
            :func:`loader.scan`).

    Returns:
        An :class:`AutodiffSchemaState` reflecting the schema after applying
        all *migrations* in order.
    """
    state = AutodiffSchemaState()
    for mig_mod in migrations:
        for op in mig_mod.migration.operations:
            op_dict = op.to_op_dict()
            kind = op_dict.get("kind", "")
            _apply_op_to_state(state, op_dict, kind)
    return state


def _apply_op_to_state(
    state: AutodiffSchemaState,
    op_dict: dict[str, Any],
    kind: str,
) -> None:
    """Apply a single op dict to the extended state (mutating).

    Unknown kinds are silently skipped for forward-compatibility with future
    op kinds that this module does not yet understand.
    """
    if kind == "create_table":
        table = op_dict["table"]
        state.tables[table] = {
            c["name"]: ColumnState(
                sql_type=c.get("sql_type", ""),
                not_null=bool(c.get("not_null", False)),
                default=_normalize_default(c.get("default")),
            )
            for c in op_dict.get("columns", [])
        }
    elif kind == "add_column":
        table = op_dict["table"]
        col_name = op_dict["name"]
        state.tables.setdefault(table, {})[col_name] = ColumnState(
            sql_type=op_dict.get("sql_type", ""),
            not_null=bool(op_dict.get("not_null", False)),
            default=_normalize_default(op_dict.get("default")),
        )
    elif kind == "drop_table":
        table = op_dict.get("table", "")
        state.tables.pop(table, None)
        # Cascade: drop indexes, FKs, RLS state, policies associated with the table.
        for idx_name in [n for n, i in state.indexes.items() if i.table == table]:
            state.indexes.pop(idx_name, None)
        for fk_name in [n for n, f in state.foreign_keys.items() if f.table == table]:
            state.foreign_keys.pop(fk_name, None)
        state.rls_enabled.pop(table, None)
        state.rls_forced.pop(table, None)
        for pol_name in [n for n, p in state.policies.items() if p.table == table]:
            state.policies.pop(pol_name, None)
        for fts_name in [n for n, f in state.full_text_indexes.items() if f.table == table]:
            state.full_text_indexes.pop(fts_name, None)
    elif kind == "drop_column":
        table = op_dict.get("table", "")
        col = op_dict.get("column", "")
        if table in state.tables:
            state.tables[table].pop(col, None)
    elif kind == "rename_column":
        table = op_dict.get("table", "")
        old = op_dict.get("from", "")
        new = op_dict.get("to", "")
        if table in state.tables and old in state.tables[table]:
            col_state = state.tables[table].pop(old)
            state.tables[table][new] = col_state
    elif kind == "add_index":
        idx_name = op_dict.get("name", "")
        if idx_name:
            state.indexes[idx_name] = IndexState(
                table=op_dict.get("table", ""),
                columns=list(op_dict.get("columns", [])),
                unique=bool(op_dict.get("unique", False)),
                using=op_dict.get("using", "btree"),
                where=op_dict.get("where"),
            )
    elif kind == "drop_index":
        state.indexes.pop(op_dict.get("name", ""), None)
    elif kind == "alter_column":
        table = op_dict.get("table", "")
        col = op_dict.get("column", "")
        if table in state.tables and col in state.tables[table]:
            col_state = state.tables[table][col]
            if op_dict.get("sql_type") is not None:
                col_state.sql_type = op_dict["sql_type"]
            if op_dict.get("not_null") is not None:
                col_state.not_null = bool(op_dict["not_null"])
            if op_dict.get("default") is not None:
                col_state.default = _normalize_default(op_dict["default"])
            if op_dict.get("drop_default"):
                col_state.default = None
    elif kind == "add_fk":
        name = op_dict.get("name", "")
        if name:
            state.foreign_keys[name] = ForeignKeyState(
                table=op_dict.get("table", ""),
                column=op_dict.get("column", ""),
                ref_table=op_dict.get("ref_table", ""),
                ref_column=op_dict.get("ref_column", "id"),
                on_delete=op_dict.get("on_delete", "CASCADE"),
            )
    elif kind == "drop_fk":
        state.foreign_keys.pop(op_dict.get("name", ""), None)
    elif kind == "create_extension":
        name = op_dict.get("name", "")
        if name:
            state.extensions[name] = op_dict.get("schema")
    elif kind == "drop_extension":
        state.extensions.pop(op_dict.get("name", ""), None)
    elif kind == "enable_rls":
        table = op_dict.get("table", "")
        state.rls_enabled[table] = True
        if op_dict.get("force"):
            state.rls_forced[table] = True
        else:
            state.rls_forced.pop(table, None)
    elif kind == "disable_rls":
        table = op_dict.get("table", "")
        state.rls_enabled.pop(table, None)
        state.rls_forced.pop(table, None)
    elif kind == "create_policy":
        name = op_dict.get("name", "")
        if name:
            state.policies[name] = PolicyState(
                name=name,
                table=op_dict.get("table", ""),
                using=op_dict.get("using", ""),
                check_expr=op_dict.get("check_expr"),
                command=op_dict.get("command", "ALL"),
                role=op_dict.get("role"),
            )
    elif kind == "drop_policy":
        state.policies.pop(op_dict.get("name", ""), None)
    elif kind == "create_function":
        name = op_dict.get("name", "")
        if name:
            state.functions[name] = op_dict.get("args", "")
    elif kind == "drop_function":
        state.functions.pop(op_dict.get("name", ""), None)
    elif kind == "create_full_text_index":
        name = op_dict.get("name", "")
        if name:
            state.full_text_indexes[name] = FullTextIndexState(
                table=op_dict.get("table", ""),
                columns=list(op_dict.get("columns", [])),
                config=op_dict.get("config"),
            )
    elif kind == "drop_full_text_index":
        state.full_text_indexes.pop(op_dict.get("name", ""), None)
    # Unknown kinds: silently skipped (forward-compat).


# ---------------------------------------------------------------------------
# Extended autodiff plan
# ---------------------------------------------------------------------------


def compute_autodiff_plan(
    model_classes: list[type[Model]],
    existing_state: AutodiffSchemaState,
    *,
    rename_hints: RenameHints | None = None,
) -> dict[str, Any]:
    """Compute an extended migration plan with type/rename/constraint/relation/
    index/FTS change detection.

    Extends :func:`orchestrator.compute_plan` with:

    - **Column type changes**: emits/merges ``alter_column`` with ``sql_type``
      set. Per W1-C (:func:`orchestrator._is_op_destructive`), any
      ``alter_column`` with ``sql_type`` is destructive — the autodiff never
      guesses a "safe" widening; the developer must confirm the apply.
    - **Column renames**: requires explicit *rename_hints*; never guesses.
      Missing intent raises :class:`FerrumMigrationError` before any SQL is
      emitted. A hint with incompatible types also raises — the developer
      must rename first, then alter the type in a separate op.
    - **Foreign-key add/drop** on existing tables (the base ``compute_plan``
      only emits ``add_fk`` for new tables).
    - **Index attribute changes**: drop + re-add when ``unique``, ``using``,
      ``where``, or ``columns`` changed on an existing index.
    - **Full-text index add/drop** (the base ``compute_plan`` does not emit
      FTS ops at all).

    Extensions, RLS, and functions are not model-declarable — there is no
    model metadata to diff against — so this function does not emit ops for
    them. They are tracked in :class:`AutodiffSchemaState` for round-trip
    replay tests via :func:`build_extended_state`.

    Args:
        model_classes: Ferrum ``Model`` subclasses to inspect. Their
            ``ModelMetadata`` (built at class-definition time) is the sole
            source of table/column names; no user input reaches SQL
            identifiers.
        existing_state: Extended schema state from
            :func:`build_extended_state`. Must be an
            :class:`AutodiffSchemaState` (subclass of ``SchemaState``) so the
            base ``compute_plan`` path runs unchanged.
        rename_hints: Per-table rename hints
            ``{table_name: {old_column_name: new_column_name}}``. Required
            when a column was renamed; without a matching hint, the diff
            raises :class:`FerrumMigrationError` rather than guessing.

    Returns:
        A plan dict matching the ``MigrationPlan`` JSON schema expected by
        :func:`orchestrator.apply`. The ``destructive`` and
        ``requires_confirmation`` fields are recomputed via
        :func:`orchestrator._is_op_destructive` for transparency; the apply
        path independently scans ops and never trusts the plan flag
        (orchestrator.py MIG-2).

    Raises:
        FerrumMigrationError: when a column was removed from the model and no
            rename hint matches (require developer intent); or when a rename
            hint has incompatible types (require a separate AlterColumn); or
            when a rename hint targets a column that is not newly-appeared.
    """
    rename_hints = rename_hints or {}

    # Start from the base plan — handles create_table, add_column, add_index,
    # drop_index, alter_column for default/nullability, add_fk for new tables.
    base_plan = compute_plan(model_classes, existing_state)
    ops: list[dict[str, Any]] = list(base_plan["ops"])

    # Build per-table model metadata lookup and a model-name → metadata map for
    # FK target table resolution (mirrors compute_plan's own lookup).
    models_by_table: dict[str, ModelMetadata] = {}
    meta_by_name: dict[str, ModelMetadata] = {}
    for cls in model_classes:
        metadata = cls.get_metadata()
        models_by_table[metadata.table_name] = metadata
        meta_by_name[cls.__name__] = metadata

    # ------------------------------------------------------------------
    # 1. Column renames — detect before type/FK/index diff to avoid conflicts.
    #    compute_plan emits add_column for the new name; we remove that and
    #    emit rename_column instead. Never guesses — requires rename_hints.
    # ------------------------------------------------------------------
    consumed_add_columns: set[tuple[str, str]] = set()

    for table, metadata in models_by_table.items():
        if table not in existing_state.tables:
            continue  # new table — compute_plan handled create_table
        existing_cols = set(existing_state.tables[table].keys())
        model_cols = {f.column_name for f in metadata.fields}
        disappeared = existing_cols - model_cols
        appeared = model_cols - existing_cols

        table_hints = rename_hints.get(table, {})
        for old_col in sorted(disappeared):
            new_col = table_hints.get(old_col)
            if new_col is None:
                raise FerrumMigrationError(
                    f"Column {table}.{old_col!r} was removed from the model. "
                    f"Provide a rename hint via rename_hints="
                    f"{{ {table!r}: {{ {old_col!r}: '<new_name>' }} }} "
                    f"or explicitly add a DropColumn op in a hand-written "
                    f"migration. Autodiff will not guess. [FERR-M001]"
                )
            if new_col not in appeared:
                raise FerrumMigrationError(
                    f"Rename hint {table}.{old_col!r} -> {new_col!r} does not "
                    f"match a newly-appeared column in the model. The hint "
                    f"target must be a new column name that exists in the "
                    f"model. [FERR-M001]"
                )
            # Validate type compatibility — rename must not change the type.
            # A type change requires a separate AlterColumn after the rename.
            old_type = _normalize_type(existing_state.tables[table][old_col].sql_type)
            new_field = next(f for f in metadata.fields if f.column_name == new_col)
            new_type = _normalize_type(new_field.sql_type)
            if old_type and new_type and old_type != new_type:
                raise FerrumMigrationError(
                    f"Rename hint {table}.{old_col!r} -> {new_col!r} has "
                    f"incompatible types ({old_type!r} vs {new_type!r}). "
                    f"Rename first, then add a separate AlterColumn to change "
                    f"the type. [FERR-M001]"
                )
            ops.append(
                {
                    "kind": "rename_column",
                    "table": table,
                    "from": old_col,
                    "to": new_col,
                }
            )
            consumed_add_columns.add((table, new_col))

    # Remove spurious add_column ops that compute_plan emitted for rename targets.
    if consumed_add_columns:
        ops = [
            op
            for op in ops
            if not (
                op.get("kind") == "add_column"
                and (op.get("table"), op.get("name")) in consumed_add_columns
            )
        ]

    # ------------------------------------------------------------------
    # 2. Column type changes — emit/merge alter_column with sql_type set
    #    (destructive per W1-C). Merge into compute_plan's existing
    #    alter_column ops where possible to avoid duplicate ALTER TABLE.
    # ------------------------------------------------------------------
    alter_index: dict[tuple[str, str], int] = {}
    for i, op in enumerate(ops):
        if op.get("kind") == "alter_column":
            alter_index[(op["table"], op["column"])] = i

    for table, metadata in models_by_table.items():
        if table not in existing_state.tables:
            continue
        for f in metadata.fields:
            if f.column_name not in existing_state.tables[table]:
                continue  # new column — add_column already emitted
            if (table, f.column_name) in consumed_add_columns:
                continue  # renamed column — type matches (validated above)
            col_state = existing_state.tables[table][f.column_name]
            old_type = _normalize_type(col_state.sql_type)
            new_type = _normalize_type(f.sql_type)
            if not old_type or not new_type or old_type == new_type:
                continue
            key = (table, f.column_name)
            if key in alter_index:
                # Merge sql_type into the existing alter_column op.
                ops[alter_index[key]]["sql_type"] = f.sql_type
            else:
                new_op: dict[str, Any] = {
                    "kind": "alter_column",
                    "table": table,
                    "column": f.column_name,
                    "sql_type": f.sql_type,
                }
                ops.append(new_op)

    # ------------------------------------------------------------------
    # 3. Foreign-key add/drop on existing tables.
    #    compute_plan emits add_fk for new tables; we handle existing tables.
    # ------------------------------------------------------------------
    for table, metadata in models_by_table.items():
        if table not in existing_state.tables:
            continue  # new table — compute_plan emitted add_fk
        desired_fks: dict[str, ForeignKeyState] = {}
        for rel in metadata.relations:
            if rel.kind in ("fk", "one_to_one") and rel.db_column:
                constraint_name = f"fk_{table}_{rel.db_column}"
                target_meta = meta_by_name.get(rel.to_model)
                target_table = target_meta.table_name if target_meta else rel.to_model.lower()
                desired_fks[constraint_name] = ForeignKeyState(
                    table=table,
                    column=rel.db_column,
                    ref_table=target_table,
                    ref_column="id",
                    on_delete=rel.on_delete or "CASCADE",
                )
        existing_fks = {
            name: fk for name, fk in existing_state.foreign_keys.items() if fk.table == table
        }
        # Drop FKs no longer desired.
        for name in sorted(existing_fks):
            if name not in desired_fks:
                ops.append({"kind": "drop_fk", "table": table, "name": name})
        # Add new FKs.
        for name, fk in desired_fks.items():
            if name not in existing_fks:
                ops.append(
                    {
                        "kind": "add_fk",
                        "table": table,
                        "name": name,
                        "column": fk.column,
                        "ref_table": fk.ref_table,
                        "ref_column": fk.ref_column,
                        "on_delete": fk.on_delete,
                    }
                )

    # ------------------------------------------------------------------
    # 4. Index attribute changes — drop + re-add when unique/using/where/
    #    columns changed on an existing index that exists by the same name.
    # ------------------------------------------------------------------
    for table, metadata in models_by_table.items():
        if table not in existing_state.tables:
            continue
        desired_indexes: dict[str, dict[str, Any]] = {}
        for f in metadata.fields:
            if f.db_index:
                idx_name = f"idx_{table}_{f.name}"
                desired_indexes[idx_name] = {
                    "kind": "add_index",
                    "table": table,
                    "name": idx_name,
                    "columns": [f.column_name],
                    "unique": False,
                    "using": "btree",
                }
        for index in metadata.indexes:
            column_names = [
                next((ff.column_name for ff in metadata.fields if ff.name == fn), fn)
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
            desired_indexes[index.name] = idx_op

        for idx_name, idx_state in existing_state.indexes.items():
            if idx_state.table != table:
                continue
            if idx_name not in desired_indexes:
                continue  # drop handled by compute_plan
            desired = desired_indexes[idx_name]
            if (
                idx_state.unique != desired.get("unique", False)
                or idx_state.using != desired.get("using", "btree")
                or idx_state.where != desired.get("where")
                or list(idx_state.columns) != list(desired.get("columns", []))
            ):
                # Attribute changed — drop + re-add. Remove any add_index op
                # that compute_plan emitted for this index (it would have
                # emitted add_index because the name matches, but with the
                # new attributes — the drop is needed first).
                ops = [
                    op
                    for op in ops
                    if not (op.get("kind") == "add_index" and op.get("name") == idx_name)
                ]
                ops.append({"kind": "drop_index", "name": idx_name, "table": table})
                ops.append(desired)

    # ------------------------------------------------------------------
    # 5. Full-text index add/drop.
    #    compute_plan does not emit FTS ops at all; we handle both new and
    #    existing tables here.
    # ------------------------------------------------------------------
    for table, metadata in models_by_table.items():
        desired_fts: dict[str, FullTextIndexState] = {}
        for fts in metadata.full_text_indexes:
            desired_fts[fts.name] = FullTextIndexState(
                table=table,
                columns=list(fts.fields),
                config=fts.config,
            )
        existing_fts = {
            name: fts
            for name, fts in existing_state.full_text_indexes.items()
            if fts.table == table
        }
        # Drop FTS indexes no longer desired.
        for name in sorted(existing_fts):
            if name not in desired_fts:
                ops.append(
                    {
                        "kind": "drop_full_text_index",
                        "table": table,
                        "name": name,
                    }
                )
        # Add new FTS indexes.
        for name, fts in desired_fts.items():
            if name not in existing_fts:
                fts_op: dict[str, Any] = {
                    "kind": "create_full_text_index",
                    "table": table,
                    "name": name,
                    "columns": list(fts.columns),
                }
                if fts.config is not None:
                    fts_op["config"] = fts.config
                ops.append(fts_op)

    # ------------------------------------------------------------------
    # Recompute destructive flag for transparency (W1-C consistency).
    # The apply path (orchestrator.py:1371) independently scans ops via
    # _is_op_destructive and never trusts the plan flag (MIG-2).
    # ------------------------------------------------------------------
    has_destructive = any(_is_op_destructive(op) for op in ops)

    timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")
    return {
        "version": 1,
        "name": f"autodiff_{timestamp}",
        "ops": ops,
        "destructive": has_destructive,
        "requires_confirmation": has_destructive,
    }
