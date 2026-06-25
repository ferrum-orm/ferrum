"""Relationship loading, instance cache, and reverse accessors."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from ferrum.connection import ConnectionLike
from ferrum.errors import FerrumCompileError, FerrumRelationNotLoadedError
from ferrum.models import Model
from ferrum.registry import get_model

if TYPE_CHECKING:
    from ferrum.models import Model, ModelMetadata, RelationMeta


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


def register_reverse(*, target_model: str, meta: ReverseRelationMeta) -> None:
    _REVERSE.setdefault(target_model, {})[meta.accessor] = meta
    try:
        cls = get_model(target_model)
    except FerrumCompileError:
        return
    setattr(cls, meta.accessor, _ReverseRelationDescriptor(meta))


def reverse_for(model_name: str) -> dict[str, ReverseRelationMeta]:
    return _REVERSE.get(model_name, {})


def install_relation_descriptors(model_cls: type[Model]) -> None:
    """Attach forward/reverse relation descriptors once per model class."""
    if model_cls in _RELATION_DESCRIPTORS_INSTALLED:
        return
    metadata = model_cls.get_metadata()
    for rel in metadata.relations:
        if rel.kind in ("fk", "one_to_one"):
            setattr(model_cls, rel.field_name, _ForwardRelationDescriptor(rel.field_name))
    for rev in reverse_for(metadata.model_name).values():
        setattr(model_cls, rev.accessor, _ReverseRelationDescriptor(rev))
    _RELATION_DESCRIPTORS_INSTALLED.add(model_cls)


def relation_cache(obj: Model) -> dict[str, Any]:
    cache = object.__getattribute__(obj, "__dict__").get("__ferrum_relations__")
    if cache is None:
        cache = {}
        object.__getattribute__(obj, "__dict__")["__ferrum_relations__"] = cache
    return cache


def set_relation(obj: Model, name: str, value: Any) -> None:  # noqa: ANN401
    relation_cache(obj)[name] = value


def get_loaded_relation(obj: Model, name: str) -> Any:  # noqa: ANN401
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
) -> dict[str, Any]:
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
        {"index": i, "name": f.name, "column": f.column_name}
        for i, f in enumerate(remote_meta.fields)
    ]
    alias = relation_name
    return {
        "relation": relation_name,
        "alias": alias,
        "local_field": {"index": field_index[rel.db_column], "name": rel.db_column},
        "remote_table": remote_meta.table_name,
        "remote_pk_column": remote_pk.column_name,
        "remote_fields": remote_fields,
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


def resolve_prefetch_name(
    metadata: ModelMetadata, name: str
) -> tuple[str, RelationMeta | ReverseRelationMeta]:
    """Resolve a prefetch name to forward M2M or reverse relation metadata."""
    for rel in metadata.relations:
        if rel.field_name == name:
            if rel.kind in ("fk", "one_to_one"):
                raise FerrumCompileError(
                    f"Use select_related({name!r}) for to-one relations; "
                    "prefetch_related() is for to-many and M2M.",
                    model=metadata.model_name,
                    field=name,
                )
            if rel.kind == "m2m":
                return ("m2m", rel)
    rev = reverse_for(metadata.model_name).get(name)
    if rev is not None:
        return ("reverse", rev)
    raise FerrumCompileError(
        f"Unknown relation {name!r} on model {metadata.model_name!r}.",
        model=metadata.model_name,
        field=name,
    )


async def prefetch_related_objects(
    instances: list[Any],
    model: type[Model],
    prefetch_names: tuple[str, ...],
    conn: ConnectionLike,
) -> None:
    """Run batched prefetch queries and populate instance relation caches."""
    if not instances or not prefetch_names:
        return
    metadata = model.get_metadata()
    pk_name = metadata.fields[metadata.pk_index].name
    parent_ids = [getattr(inst, pk_name) for inst in instances]
    parent_ids = [pid for pid in parent_ids if pid is not None]
    if not parent_ids:
        return

    for name in prefetch_names:
        kind, meta = resolve_prefetch_name(metadata, name)
        if kind == "m2m":
            await _prefetch_m2m(
                instances,
                metadata,
                cast(RelationMeta, meta),
                name,
                parent_ids,
                pk_name,
                conn,
            )
        elif kind == "reverse":
            await _prefetch_reverse_fk(
                instances,
                metadata,
                cast(ReverseRelationMeta, meta),
                name,
                parent_ids,
                pk_name,
                conn,
            )


async def _prefetch_reverse_fk(
    instances: list[Any],
    metadata: ModelMetadata,
    rev: ReverseRelationMeta,
    name: str,
    parent_ids: list[Any],
    pk_name: str,
    conn: ConnectionLike,
) -> None:
    related_model = get_model(rev.related_model_name)
    related_meta = related_model.get_metadata()
    driver = conn._require_driver()
    placeholders = ", ".join(f"${i}" for i in range(1, len(parent_ids) + 1))
    sql = f'SELECT * FROM "{related_meta.table_name}" WHERE "{rev.fk_column}" IN ({placeholders})'
    raw_rows = await driver.fetch(sql, *parent_ids)
    grouped: dict[Any, list[Any]] = {pid: [] for pid in parent_ids}
    for raw in raw_rows:
        row_dict = dict(raw) if hasattr(raw, "keys") else raw
        obj = related_model.model_construct(**{k: row_dict[k] for k in row_dict})
        fk_val = getattr(obj, rev.fk_column, None)
        if fk_val in grouped:
            grouped[fk_val].append(obj)
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
    if rel.through_table is None:
        raise FerrumCompileError(
            f"M2M relation {name!r} missing through_table.",
            model=metadata.model_name,
            field=name,
        )
    target = get_model(rel.to_model)
    target_table = target.get_metadata().table_name
    owner_col = f"{metadata.table_name}_id"
    target_col = f"{target_table}_id"
    driver = conn._require_driver()
    placeholders = ", ".join(f"${i}" for i in range(1, len(parent_ids) + 1))
    join_sql = (
        f'SELECT "{owner_col}", "{target_col}" FROM "{rel.through_table}" '
        f'WHERE "{owner_col}" IN ({placeholders})'
    )
    join_rows = await driver.fetch(join_sql, *parent_ids)
    target_ids: set[Any] = set()
    links: dict[Any, list[Any]] = {pid: [] for pid in parent_ids}
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
    tpk = target.get_metadata().fields[target.get_metadata().pk_index].name
    placeholders = ", ".join(f"${i}" for i in range(1, len(target_ids) + 1))
    target_sql = f'SELECT * FROM "{target_table}" WHERE "{tpk}" IN ({placeholders})'
    target_rows = await driver.fetch(target_sql, *list(target_ids))
    by_id = {}
    for raw in target_rows:
        row_dict = dict(raw) if hasattr(raw, "keys") else raw
        obj = target.model_construct(**row_dict)
        by_id[getattr(obj, tpk)] = obj
    for inst in instances:
        pid = getattr(inst, pk_name)
        set_relation(
            inst,
            name,
            [by_id[tid] for tid in links.get(pid, []) if tid in by_id],
        )
