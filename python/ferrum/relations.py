"""Relationship loading, instance cache, and reverse accessors.

Forward to-one relations are populated by ``select_related()`` using JOIN output
from the Rust compiler. To-many and many-to-many relations are populated by
``prefetch_related()`` using explicit batched Python queries. Accessing an
unloaded relation raises a Ferrum error instead of silently issuing implicit I/O.

Cascade behavior
----------------

Ferrum does **not** implement a SQLAlchemy-style unit-of-work cascade. ON
DELETE actions (``CASCADE``, ``SET NULL``, ``RESTRICT``, ``SET DEFAULT``,
``NO ACTION``) are declared on ``ForeignKey`` / ``OneToOne`` fields and emitted
as ``FOREIGN KEY … ON DELETE`` clauses in DDL. The **database** enforces them
when a parent row is deleted — Ferrum issues no follow-up DELETE/UPDATE
statements to emulate cascades in Python.

This means:

- Deleting a parent row via ``QuerySet.delete()`` or ``bulk_delete()`` lets
  PostgreSQL apply the configured ON DELETE action to all referencing rows.
- There is no Python-side cascade traversal, no per-instance cascade dispatch,
  and no identity-map ordering of cascade operations.
- ``bulk_delete`` on a parent table triggers all FK ON DELETE actions in a
  single database statement — no N+1 cascade queries.
- Applications that need application-level cascade logic (e.g. soft-delete
  propagation, audit logging) must implement it explicitly in their service
  layer, not rely on Ferrum to emulate it.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from ferrum.connection import ConnectionLike
from ferrum.errors import FerrumCompileError, FerrumRelationNotLoadedError
from ferrum.models import Model, RelationMeta
from ferrum.registry import get_model

if TYPE_CHECKING:
    from ferrum.models import Model, ModelMetadata


# PostgreSQL allows up to 65535 bound parameters per statement. We cap prefetch
# IN-clause batches well below that to leave room for other parameters and to
# stay portable across the thin-parity backends (SQLite has a 32766 limit;
# MySQL/MSSQL have their own limits). 32768 is a safe upper bound.
_MAX_PREFETCH_PARAMS = 32768


@dataclasses.dataclass(frozen=True)
class ReverseRelationMeta:
    """Reverse accessor metadata installed on the *target* FK model."""

    accessor: str
    related_model_name: str
    fk_column: str
    fk_field_name: str
    kind: str  # "fk" | "one_to_one" | "m2m"


_REVERSE: dict[str, dict[str, ReverseRelationMeta]] = {}
_RELATION_DESCRIPTORS_INSTALLED: set[type] = set()


def _quote_identifier(identifier: str, dialect: str) -> str:
    """Quote an identifier already resolved from immutable model metadata."""
    if dialect == "mysql":
        return f"`{identifier.replace('`', '``')}`"
    if dialect == "mssql":
        return f"[{identifier.replace(']', ']]')}]"
    if dialect in ("postgres", "sqlite"):
        return f'"{identifier.replace(chr(34), chr(34) * 2)}"'
    raise FerrumCompileError(f"Unsupported prefetch dialect {dialect!r}.")


def _placeholders(count: int, dialect: str) -> str:
    if dialect == "postgres":
        return ", ".join(f"${i}" for i in range(1, count + 1))
    if dialect == "mysql":
        return ", ".join("%s" for _ in range(count))
    if dialect in ("sqlite", "mssql"):
        return ", ".join("?" for _ in range(count))
    raise FerrumCompileError(f"Unsupported prefetch dialect {dialect!r}.")


def _coerce_hydrated_row(model: type[Model], row: dict[str, Any]) -> dict[str, Any]:
    """Coerce only DB integer values backed by boolean model metadata."""
    coerced = dict(row)
    for field in model.get_metadata().fields:
        if field.field_type != "bool":
            continue
        key = field.name if field.name in coerced else field.column_name
        value = coerced.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            coerced[key] = bool(value)
    return coerced


def _chunk_ids(ids: list[Any], max_params: int | None = None) -> list[list[Any]]:
    """Split *ids* into batches that respect the bound-parameter limit.

    Each batch produces one ``IN ($1, $2, …)`` clause with at most *max_params*
    placeholders. This prevents PostgreSQL's 65535-parameter ceiling (and
    SQLite's 32766 ceiling) from aborting prefetch on large parent sets.

    When *max_params* is ``None``, the module-level :data:`_MAX_PREFETCH_PARAMS`
    is read at call time (not capture time), so monkeypatching it in tests
    takes effect.
    """
    if max_params is None:
        max_params = _MAX_PREFETCH_PARAMS
    if max_params < 1:
        max_params = 1
    return [ids[i : i + max_params] for i in range(0, len(ids), max_params)]


def safe_batch_size(
    num_fields_per_row: int,
    *,
    max_params: int = 65535,
    requested: int = 1000,
) -> int:
    """Return a batch size that keeps total bound parameters under *max_params*.

    For bulk INSERT/UPDATE/DELETE, each row contributes ``num_fields_per_row``
    bound parameters. PostgreSQL allows at most 65535 parameters per statement.
    This helper clamps the requested batch size so the total parameter count
    stays within the limit, preventing ``FerrumProgrammingError`` on large
    bulk operations.

    Examples::

        safe_batch_size(3, requested=30000)  # → 21845 (3 * 21845 = 65535)
        safe_batch_size(1, requested=1000)   # → 1000 (well under limit)
    """
    if num_fields_per_row < 1:
        return max(1, requested)
    limit = max_params // num_fields_per_row
    return max(1, min(requested, limit))


def _resolve_through_columns(
    metadata: ModelMetadata,
    rel: RelationMeta,
) -> tuple[str, str]:
    """Resolve the through-table FK column names from model metadata.

    M2M through tables have two FK columns named ``{snake_case_model_name}_id``.
    The owner column points to the model declaring the M2M; the target column
    points to the related model. Both are derived from ``table_name`` (which is
    ``_to_snake_case(class_name)``) — never from user input.
    """
    if rel.through_table is None:
        raise FerrumCompileError(
            f"M2M relation {rel.field_name!r} missing through_table.",
            model=metadata.model_name,
            field=rel.field_name,
        )
    target = get_model(rel.to_model)
    owner_col = f"{metadata.table_name}_id"
    target_col = f"{target.get_metadata().table_name}_id"
    return owner_col, target_col


def register_reverse(*, target_model: str, meta: ReverseRelationMeta) -> None:
    """Register a reverse relation and install it immediately when possible.

    Relationship targets may be imported after the declaring model, so unresolved
    reverse metadata is kept in ``_REVERSE`` and descriptors are installed later
    by ``install_relation_descriptors()`` when the target model becomes known.
    """
    _REVERSE.setdefault(target_model, {})[meta.accessor] = meta
    try:
        cls = get_model(target_model)
    except FerrumCompileError:
        return
    setattr(cls, meta.accessor, _ReverseRelationDescriptor(meta))


def reverse_for(model_name: str) -> dict[str, ReverseRelationMeta]:
    """Return registered reverse accessors for a model name."""
    return _REVERSE.get(model_name, {})


def install_relation_descriptors(model_cls: type[Model]) -> None:
    """Attach forward/reverse relation descriptors once per model class.

    Forward descriptors are installed for **all** relation kinds (FK, OTO, M2M)
    so that accessing an unloaded relation always raises
    :class:`FerrumRelationNotLoadedError` instead of returning the raw
    ``ClassVar`` descriptor object. Per §5a (Explicit rejections), forward
    relations — including forward M2M — raise when not loaded.
    """
    if model_cls in _RELATION_DESCRIPTORS_INSTALLED:
        return
    metadata = model_cls.get_metadata()
    for rel in metadata.relations:
        setattr(model_cls, rel.field_name, _ForwardRelationDescriptor(rel.field_name))
    for rev in reverse_for(metadata.model_name).values():
        setattr(model_cls, rev.accessor, _ReverseRelationDescriptor(rev))
    _RELATION_DESCRIPTORS_INSTALLED.add(model_cls)


def relation_cache(obj: Model) -> dict[str, Any]:
    """Return the per-instance relation cache, creating it on first use.

    The cache lives in the model instance ``__dict__`` and is intentionally not
    part of Pydantic validation or serialization. It stores only loaded relation
    objects produced by explicit eager-loading calls.
    """
    cache = object.__getattribute__(obj, "__dict__").get("__ferrum_relations__")
    if cache is None:
        cache = {}
        object.__getattribute__(obj, "__dict__")["__ferrum_relations__"] = cache
    return cache


def set_relation(obj: Model, name: str, value: Any) -> None:  # noqa: ANN401
    """Store a loaded relation value on one model instance."""
    relation_cache(obj)[name] = value


def get_loaded_relation(obj: Model, name: str) -> Any:  # noqa: ANN401
    """Return a loaded relation or raise when the caller skipped eager loading."""
    cache = relation_cache(obj)
    if name not in cache:
        raise FerrumRelationNotLoadedError(
            f"Relation {name!r} on {type(obj).__name__} is not loaded. "
            "Use select_related() or prefetch_related() before accessing it. [FERR-Q407]"
        )
    return cache[name]


class _ForwardRelationDescriptor:
    def __init__(self, field_name: str) -> None:
        self.field_name = field_name

    def __get__(self, obj: object, owner: type | None = None) -> Any:  # noqa: ANN401
        if obj is None:
            return self
        return get_loaded_relation(cast(Model, obj), self.field_name)


class _ReverseRelationDescriptor:
    """Descriptor for reverse FK / one-to-one accessors.

    Per the ratified W0-A contract (AGENTS.md §5a — Explicit rejections):

    - **Reverse FK/OTO**: returns an **unbound** ``QuerySet`` filtered by the FK
      column with **no I/O**. A later terminal (``.all(conn)``, ``.get(conn)``)
      still requires an explicit ``ConnectionLike``. This is the only lazy-access
      path that does not raise.
    - **Reverse M2M**: always raises ``FerrumRelationNotLoadedError``. M2M
      requires a through-table join that cannot be expressed as a simple filter
      on an unbound QuerySet without hidden I/O. Use ``prefetch_related()``.

    The unbound QuerySet is filtered by the FK column using the parent's ``id``
    field (``getattr(obj, "id")``). Composite primary keys are not yet supported
    — the accessor reads a single ``id`` attribute and filters by one FK column.
    """

    def __init__(self, meta: ReverseRelationMeta) -> None:
        self._meta = meta

    def __get__(self, obj: object, owner: type | None = None) -> Any:  # noqa: ANN401
        if obj is None:
            return self
        cache = relation_cache(cast(Model, obj))
        if self._meta.accessor in cache:
            return cache[self._meta.accessor]
        if self._meta.kind == "m2m":
            raise FerrumRelationNotLoadedError(
                f"Relation {self._meta.accessor!r} on {type(obj).__name__} is not loaded. "
                "Use prefetch_related() before accessing it. [FERR-Q407]"
            )
        from ferrum.queryset import QuerySet

        related = get_model(self._meta.related_model_name)
        pk_val = getattr(obj, "id", None)
        return QuerySet(related).filter(**{self._meta.fk_column: pk_val})


def resolve_relation(metadata: ModelMetadata, name: str) -> RelationMeta:
    for rel in metadata.relations:
        if rel.field_name == name:
            return rel
    raise FerrumCompileError(
        f"Unknown relation {name!r} on model {metadata.model_name!r}.",
        model=metadata.model_name,
        field=name,
    )


def build_join_ir(
    metadata: ModelMetadata,
    relation_name: str,
    field_index: dict[str, int],
    *,
    join_kind: str = "left",
    project_remote: bool = True,
    remote_field_names: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build the JOIN IR entry for ``select_related()`` or relation-filter lookups.

    Only ForeignKey and OneToOne relations are valid here because they preserve
    one output row per parent row. To-many relations must use prefetching to
    avoid row multiplication and surprising hydration behavior.

    Args:
        join_kind: ``"left"`` (select_related) or ``"inner"`` (relation filters).
        project_remote: When ``True``, SELECT projects ``alias__col`` columns.
        remote_field_names: When set, only these remote fields are included
            (for filter allowlisting on filter-only joins).
    """
    rel = resolve_relation(metadata, relation_name)
    if rel.kind not in ("fk", "one_to_one"):
        raise FerrumCompileError(
            f"select_related() only supports ForeignKey and OneToOne; "
            f"{relation_name!r} is {rel.kind!r}. Use prefetch_related() instead.",
            model=metadata.model_name,
            field=relation_name,
        )
    if rel.db_column is None:
        raise FerrumCompileError(
            f"Relation {relation_name!r} has no backing column.",
            model=metadata.model_name,
            field=relation_name,
        )
    if rel.db_column not in field_index:
        raise FerrumCompileError(
            f"Unknown FK column {rel.db_column!r} for relation {relation_name!r}.",
            model=metadata.model_name,
            field=rel.db_column,
        )
    remote = get_model(rel.to_model)
    remote_meta = remote.get_metadata()
    remote_pk = remote_meta.fields[remote_meta.pk_index]
    remote_fields = [
        {
            "index": i,
            "name": f.name,
            "column": f.column_name,
            "allowed_operators": list(f.allowed_operators),
            "field_type": f.field_type,
        }
        for i, f in enumerate(remote_meta.fields)
        if remote_field_names is None or f.name in remote_field_names
    ]
    alias = relation_name
    return {
        "relation": relation_name,
        "alias": alias,
        "local_field": {"index": field_index[rel.db_column], "name": rel.db_column},
        "remote_table": remote_meta.table_name,
        "remote_pk_column": remote_pk.column_name,
        "remote_fields": remote_fields,
        "join_kind": join_kind,
        "project_remote": project_remote,
    }


def split_joined_row(
    row_dict: dict[str, Any], joins: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Split flat JOIN columns ``alias__col`` into per-relation row dicts."""
    result: dict[str, dict[str, Any]] = {}
    for join in joins:
        alias = join["alias"]
        prefix = f"{alias}__"
        related: dict[str, Any] = {}
        for key, value in row_dict.items():
            if key.startswith(prefix):
                related[key[len(prefix) :]] = value
        result[join["relation"]] = related
    return result


def _split_nested_prefetch(name: str) -> tuple[str, str | None]:
    """Split a prefetch name on the first ``__`` separator.

    Returns ``(first_level, remaining)`` where ``remaining`` is ``None`` when
    the name has no ``__`` (a flat one-level prefetch).

    Examples::

        _split_nested_prefetch("posts")         # → ("posts", None)
        _split_nested_prefetch("posts__tags")   # → ("posts", "tags")
        _split_nested_prefetch("a__b__c")       # → ("a", "b__c")
    """
    if "__" not in name:
        return name, None
    first, _, rest = name.partition("__")
    return first, rest


def resolve_prefetch_name(
    metadata: ModelMetadata, name: str
) -> tuple[str, RelationMeta | ReverseRelationMeta]:
    """Resolve a prefetch name to forward M2M or reverse relation metadata.

    Only the first level of a nested prefetch name is resolved here; the
    remaining levels are resolved recursively in
    :func:`prefetch_related_objects` after the first level is loaded.
    """
    first, _remaining = _split_nested_prefetch(name)
    for rel in metadata.relations:
        if rel.field_name == first:
            if rel.kind in ("fk", "one_to_one"):
                raise FerrumCompileError(
                    f"Use select_related({first!r}) for to-one relations; "
                    "prefetch_related() is for to-many and M2M.",
                    model=metadata.model_name,
                    field=first,
                )
            if rel.kind == "m2m":
                return ("m2m", rel)
    rev = reverse_for(metadata.model_name).get(first)
    if rev is not None:
        return ("reverse", rev)
    raise FerrumCompileError(
        f"Unknown relation {first!r} on model {metadata.model_name!r}.",
        model=metadata.model_name,
        field=first,
    )


async def prefetch_related_objects(
    instances: list[Any],
    model: type[Model],
    prefetch_names: tuple[str, ...],
    conn: ConnectionLike,
) -> None:
    """Run batched prefetch queries and populate instance relation caches.

    Each requested relation performs at most one query per batch, keyed by the
    parent primary-key values already in memory. This avoids N+1 queries while
    keeping to-many loading outside the SELECT compiler's JOIN path.

    Bounded batching: parent IDs are chunked to respect the bound-parameter
    limit (see :data:`_MAX_PREFETCH_PARAMS`), so prefetching on a large parent
    set issues multiple smaller queries rather than one that exceeds the
    database's parameter ceiling.

    Nested prefetch: names containing ``__`` (e.g. ``"posts__tags"``) are
    split into levels. The first level is loaded on the parent instances;
    subsequent levels are loaded recursively on the freshly loaded related
    objects.

    Ordering: reverse-FK rows are ordered by the related model's PK for
    deterministic results. M2M target rows are fetched with ``ORDER BY PK``,
    but per-parent list order follows the through-table link order (Phase 1
    has no ``ORDER BY``), not the target PK order. This does not affect the
    public QuerySet API (which has no prefetch-ordering parameter yet).

    No hidden I/O: this function is only called from ``QuerySet._materialize``
    after an explicit ``all(conn)`` / ``first(conn)`` terminal. Relation
    attribute access on an unloaded instance still raises
    :class:`FerrumRelationNotLoadedError`.
    """
    if not instances or not prefetch_names:
        return
    metadata = model.get_metadata()
    pk_name = metadata.fields[metadata.pk_index].name
    parent_ids = [getattr(inst, pk_name) for inst in instances]
    parent_ids = [pid for pid in parent_ids if pid is not None]
    if not parent_ids:
        return

    # Group prefetch names by first level to avoid loading the same relation
    # twice when multiple nested paths share a prefix.
    first_levels: dict[str, list[str | None]] = {}
    for name in prefetch_names:
        first, remaining = _split_nested_prefetch(name)
        first_levels.setdefault(first, []).append(remaining)

    for first, remainings in first_levels.items():
        kind, meta = resolve_prefetch_name(metadata, first)
        if kind == "m2m":
            await _prefetch_m2m(
                instances,
                metadata,
                cast(RelationMeta, meta),
                first,
                parent_ids,
                pk_name,
                conn,
            )
        elif kind == "reverse":
            await _prefetch_reverse_fk(
                instances,
                metadata,
                cast(ReverseRelationMeta, meta),
                first,
                parent_ids,
                pk_name,
                conn,
            )

        # Handle nested prefetch levels: collect all loaded related objects
        # and recursively prefetch the remaining levels on them.
        nested = [r for r in remainings if r is not None]
        if not nested:
            continue
        related_instances = _collect_loaded_related(instances, first, kind, meta)
        if not related_instances:
            continue
        related_model = _related_model_for(meta)
        await prefetch_related_objects(related_instances, related_model, tuple(nested), conn)


def _collect_loaded_related(
    instances: list[Any],
    name: str,
    kind: str,
    meta: RelationMeta | ReverseRelationMeta,
) -> list[Model]:
    """Collect all related objects loaded under *name* for nested prefetch.

    For reverse FK: each parent's cache holds a list of related objects.
    For M2M: each parent's cache holds a list of related objects.
    For reverse OTO: each parent's cache holds a single object or None.
    """
    result: list[Model] = []
    for inst in instances:
        cache = relation_cache(cast(Model, inst))
        val = cache.get(name)
        if val is None:
            continue
        if isinstance(val, list):
            result.extend(val)
        else:
            result.append(cast(Model, val))
    return result


def _related_model_for(meta: RelationMeta | ReverseRelationMeta) -> type[Model]:
    """Return the model class for the related objects of a prefetch level."""
    if isinstance(meta, ReverseRelationMeta):
        return get_model(meta.related_model_name)
    return get_model(meta.to_model)


async def _prefetch_reverse_fk(
    instances: list[Any],
    metadata: ModelMetadata,
    rev: ReverseRelationMeta,
    name: str,
    parent_ids: list[Any],
    pk_name: str,
    conn: ConnectionLike,
) -> None:
    """Prefetch reverse FK or reverse one-to-one relations via batched queries.

    For reverse FK: stores a ``list`` of related objects on each parent.
    For reverse one-to-one: stores a single related object or ``None``.

    Parent IDs are chunked to respect the bound-parameter limit. Results are
    ordered by the related model's PK for deterministic output.

    All SQL identifiers are quoted from immutable model metadata (never user
    input). All values travel as bound parameters (``$N`` / ``?`` / ``%s``).
    """
    related_model = get_model(rev.related_model_name)
    related_meta = related_model.get_metadata()
    related_pk = related_meta.fields[related_meta.pk_index]
    driver = conn._require_driver()
    table = _quote_identifier(related_meta.table_name, conn.dialect)
    fk_column = _quote_identifier(rev.fk_column, conn.dialect)
    pk_column = _quote_identifier(related_pk.column_name, conn.dialect)

    grouped: dict[Any, list[Any]] = {pid: [] for pid in parent_ids}
    for batch in _chunk_ids(parent_ids):
        placeholders = _placeholders(len(batch), conn.dialect)
        sql = f"SELECT * FROM {table} WHERE {fk_column} IN ({placeholders}) ORDER BY {pk_column}"
        raw_rows = await driver.fetch(sql, *batch)
        for raw in raw_rows:
            row_dict = dict(raw) if hasattr(raw, "keys") else raw
            obj = related_model.model_construct(**_coerce_hydrated_row(related_model, row_dict))
            fk_val = getattr(obj, rev.fk_column, None)
            if fk_val in grouped:
                grouped[fk_val].append(obj)

    if rev.kind == "one_to_one":
        # Reverse one-to-one: store a single object or None per parent.
        for inst in instances:
            vals = grouped.get(getattr(inst, pk_name), [])
            set_relation(inst, name, vals[0] if vals else None)
    else:
        for inst in instances:
            set_relation(inst, name, grouped.get(getattr(inst, pk_name), []))


async def _prefetch_m2m(
    instances: list[Any],
    metadata: ModelMetadata,
    rel: RelationMeta,
    name: str,
    parent_ids: list[Any],
    pk_name: str,
    conn: ConnectionLike,
) -> None:
    """Prefetch many-to-many relations via batched through-table + target queries.

    Issues two phases of bounded queries:

    1. **Through-table query**: fetch ``(owner_id, target_id)`` pairs from the
       join table, chunked to respect the parameter limit.
    2. **Target query**: fetch full target rows by their PKs, chunked to
       respect the parameter limit.

    Target rows are fetched with ``ORDER BY PK`` (Phase 2), but per-parent list
    order follows the through-table link order from Phase 1 (which has no
    ``ORDER BY``), not the target PK order.

    All SQL identifiers are quoted from immutable model metadata (never user
    input). All values travel as bound parameters.
    """
    if rel.through_table is None:
        raise FerrumCompileError(
            f"M2M relation {name!r} missing through_table.",
            model=metadata.model_name,
            field=name,
        )
    target = get_model(rel.to_model)
    target_meta = target.get_metadata()
    target_pk = target_meta.fields[target_meta.pk_index]
    owner_col, target_col = _resolve_through_columns(metadata, rel)
    driver = conn._require_driver()
    owner_ident = _quote_identifier(owner_col, conn.dialect)
    target_ident = _quote_identifier(target_col, conn.dialect)
    through_ident = _quote_identifier(rel.through_table, conn.dialect)
    target_table_ident = _quote_identifier(target_meta.table_name, conn.dialect)
    target_pk_ident = _quote_identifier(target_pk.column_name, conn.dialect)

    # Phase 1: fetch through-table links in bounded batches.
    links: dict[Any, list[Any]] = {pid: [] for pid in parent_ids}
    target_ids: set[Any] = set()
    for batch in _chunk_ids(parent_ids):
        placeholders = _placeholders(len(batch), conn.dialect)
        join_sql = (
            f"SELECT {owner_ident}, {target_ident} FROM {through_ident} "
            f"WHERE {owner_ident} IN ({placeholders})"
        )
        join_rows = await driver.fetch(join_sql, *batch)
        for jr in join_rows:
            row = dict(jr) if hasattr(jr, "keys") else jr
            owner_id = row[owner_col]
            target_id = row[target_col]
            target_ids.add(target_id)
            if owner_id in links:
                links[owner_id].append(target_id)

    if not target_ids:
        for inst in instances:
            set_relation(inst, name, [])
        return

    # Phase 2: fetch target rows in bounded batches.
    tpk = target_pk.name
    target_id_list = list(target_ids)
    by_id: dict[Any, Any] = {}
    for batch in _chunk_ids(target_id_list):
        placeholders = _placeholders(len(batch), conn.dialect)
        target_sql = (
            f"SELECT * FROM {target_table_ident} "
            f"WHERE {target_pk_ident} IN ({placeholders}) "
            f"ORDER BY {target_pk_ident}"
        )
        target_rows = await driver.fetch(target_sql, *batch)
        for raw in target_rows:
            row_dict = dict(raw) if hasattr(raw, "keys") else raw
            obj = target.model_construct(**_coerce_hydrated_row(target, row_dict))
            by_id[getattr(obj, tpk)] = obj

    for inst in instances:
        pid = getattr(inst, pk_name)
        set_relation(
            inst,
            name,
            [by_id[tid] for tid in links.get(pid, []) if tid in by_id],
        )
