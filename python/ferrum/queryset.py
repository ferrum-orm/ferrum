"""Ferrum QuerySet: lazy, chainable, async query builder.

``QuerySet`` accumulates filter/order/limit/offset state and only touches the
database when a terminal coroutine is awaited. The terminal methods delegate
to the connection driver layer and to the Rust compiler (ferrum._native).

Design constraints:
- No SQL building here. QuerySet only builds the IR dict.
- Danger API guards live here: ``delete()`` and ``update()`` without a filter
  raise ``FerrumDangerApiError``; callers must use ``danger_delete_all()`` /
  ``danger_update_all()`` explicitly (AGENTS.md §3).
- This module must NOT import ``ferrum.cli`` or ``ferrum.contrib`` (enforced by
  import-linter contract in CI).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import time
import types
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, Literal, Self, TypeVar, cast, overload
from uuid import UUID

import ferrum.hooks as _hooks
from ferrum.config import resolve_wire_format as _resolve_wire_format
from ferrum.drivers.protocol import _compiled_query
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumDangerApiError,
    FerrumInternalError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    map_db_error,
    map_native_error,
)
from ferrum.expressions import Q, args_to_q

if TYPE_CHECKING:
    from ferrum.connection import Connection, Transaction
    from ferrum.models import FieldMeta, Model, ModelMetadata

    # Terminals accept an open Connection or an active Transaction interchangeably:
    # both expose the ``dialect`` / ``_require_driver()`` surface the terminals use.
    ConnectionLike = Connection | Transaction

_M = TypeVar("_M", bound="Model")
_P = TypeVar("_P", bound="Model")
# Row type produced by a queryset's terminals. Bound per subclass: ``_M`` for the
# model queryset, ``dict``/``tuple``/``Any`` for the value variants.
_R = TypeVar("_R")

# Module-level reference to the native Rust extension.  Absent when the wheel
# has not been built (e.g. unit-test environments without a compiled extension).
_native_ext: types.ModuleType | None = None
with contextlib.suppress(ImportError):
    _native_ext = importlib.import_module("ferrum._native")

# IR version — must stay in sync with ferrum-core IR_VERSION (crates/ferrum-core/src/ir/mod.rs).
_IR_VERSION: int = 4

AggregateFunction = Literal["count", "sum", "avg", "min", "max"]
DateTruncGranularity = Literal["minute", "hour", "day", "week", "month", "quarter", "year"]
HavingOperator = Literal["eq", "ne", "gt", "gte", "lt", "lte"]


@dataclass(frozen=True, slots=True)
class Aggregate:
    """Typed aggregate expression used by :meth:`QuerySet.aggregate`.

    Field and filter references are resolved through model metadata; this object
    cannot carry SQL fragments or output identifiers.
    """

    function: AggregateFunction
    field: str | None = None
    filter: Q | dict[str, Any] | None = None

    @classmethod
    def count(
        cls, field: str | None = None, *, filter: Q | dict[str, Any] | None = None
    ) -> Aggregate:
        return cls("count", field, filter)

    @classmethod
    def sum(cls, field: str, *, filter: Q | dict[str, Any] | None = None) -> Aggregate:
        return cls("sum", field, filter)

    @classmethod
    def avg(cls, field: str, *, filter: Q | dict[str, Any] | None = None) -> Aggregate:
        return cls("avg", field, filter)

    @classmethod
    def min(cls, field: str, *, filter: Q | dict[str, Any] | None = None) -> Aggregate:
        return cls("min", field, filter)

    @classmethod
    def max(cls, field: str, *, filter: Q | dict[str, Any] | None = None) -> Aggregate:
        return cls("max", field, filter)


# Maps QuerySet ``mode=`` kwargs to filter lookup operators and IR ``TextSearchMode`` tags.
_TEXT_SEARCH_MODES: dict[str, tuple[str, str]] = {
    "plain": ("match", "plain"),
    "phrase": ("match_phrase", "phrase"),
    "websearch": ("match_websearch", "websearch"),
    "boolean": ("match_boolean", "boolean"),
}

_EXT_NOT_BUILT_MSG = (
    "ferrum._native extension not built. "
    "Run: maturin develop  (or: uv run maturin develop) [FERR-C001]"
)

# Wire format for the Python↔Rust boundary, resolved once at import so the hot
# query path never re-reads config. "json" (default) or "msgpack".
_WIRE_FORMAT: str = _resolve_wire_format()

# msgpack is an optional dependency; imported lazily on first use only when the
# msgpack wire format is active (mirrors the driver lazy-import pattern).
_msgpack_mod: types.ModuleType | None = None


def _require_msgpack() -> types.ModuleType:
    """Return the ``msgpack`` module or raise an install-hint error (Pattern C)."""
    global _msgpack_mod
    if _msgpack_mod is None:
        try:
            _msgpack_mod = importlib.import_module("msgpack")
        except ImportError as exc:
            raise FerrumConfigError(
                "MessagePack wire format selected (FERRUM_WIRE_FORMAT=msgpack or "
                "[ferrum] wire_format) but the 'msgpack' package is not installed. "
                "Install with: uv add 'ferrum-orm[msgpack]' [FERR-C001]"
            ) from exc
    return _msgpack_mod


def _msgpack_row_default(obj: Any) -> Any:  # noqa: ANN401
    """``msgpack.packb`` ``default`` hook mirroring ``_RowEncoder`` conversions."""
    if hasattr(obj, "_mapping"):
        return dict(obj._mapping)
    if isinstance(obj, (bytearray, memoryview)):
        return bytes(obj)
    # Structural-only copy for the Rust presence/nullability check; the real
    # value keeps its native type for model_construct, so str() here is lossless.
    return str(obj)


def _encode_vector_literal(vector: list[float]) -> str:
    """Encode a float list as a pgvector text literal (``[f,f,...]``).

    Bound as ``text`` with an SQL ``::vector`` cast — asyncpg ``float[]``
    binding raises ``DataError`` against pgvector columns.
    """
    return "[" + ",".join(str(float(v)) for v in vector) + "]"


def _encode_bind_value(value: object) -> dict[str, object]:
    """Encode a Python value as an IR BindValue tagged-union dict.

    Format uses adjacent tagging matching Rust's ``#[serde(tag = "type", content = "value")]``:
    ``{"type": "text", "value": "hello"}``, ``{"type": "int", "value": 42}``.
    ``BindValue::Null`` carries no ``value`` key: ``{"type": "null"}``.

    ``date`` and ``time`` have no corresponding Rust ``BindValue`` variant in v1;
    they fall through to the ``text`` fallback via ``str()``.

    isinstance ordering is significant:
    - ``bool`` must precede ``int`` (bool is a subclass of int).
    - ``datetime`` must precede any date-like check (datetime subclasses date).
    """
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": list(value)}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, UUID):
        return {"type": "text", "value": str(value)}
    if isinstance(value, list):
        if not value:
            # Empty list: default to text_array; asyncpg infers type from column.
            return {"type": "text_array", "value": []}
        # Check element type to select array variant.
        first = next((v for v in value if v is not None), None)
        if first is not None and isinstance(first, (int, float)) and not isinstance(first, bool):  # noqa: SIM102
            # pgvector float array (used by nearest_to) or int/float array column
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
                # Distinguish int_array vs float_array by element type
                if all(isinstance(v, int) and not isinstance(v, bool) for v in value):
                    return {"type": "int_array", "value": [cast(int, v) for v in value]}
                floats: list[float] = [cast(float, v) for v in value]
                return {"type": "float_array", "value": floats}
        if first is not None and isinstance(first, UUID):
            return {"type": "text_array", "value": [str(v) for v in value]}
        # Default: text array (covers list[str] and mixed/unknown types)
        strs = [str(v) if not isinstance(v, str) else v for v in value]
        return {"type": "text_array", "value": strs}
    return {"type": "text", "value": str(value)}


def _prepare_field_value(field: Any, value: object) -> object:  # noqa: ANN401
    """Normalize a Python value for the field's database representation."""
    if value is None:
        return value
    if field.field_type == "json":
        return json.dumps(value, default=str, separators=(",", ":"))
    if field.field_type == "vector" and isinstance(value, (list, tuple)):
        # asyncpg has no built-in codec for the pgvector ``vector`` type and
        # falls back to text, so a bound ``list[float]`` raises DataError
        # ("expected str, got list"). Bind the pgvector text literal instead —
        # the same encoding ``nearest_to()`` uses on the read path.
        return _encode_vector_literal([float(v) for v in cast(list[Any], value)])
    return value


def _encode_field_bind_value(field: Any, value: object) -> dict[str, object]:  # noqa: ANN401
    """Encode a value after applying field-aware database normalization."""
    return _encode_bind_value(_prepare_field_value(field, value))


def _encode_filter_bind_value(
    field: FieldMeta,
    operator: str,
    value: object,
) -> dict[str, object]:
    """Encode a lookup value according to its field and operator."""
    if field.field_type == "json" and operator not in {"has_key", "has_any_keys"}:
        return _encode_field_bind_value(field, value)
    return _encode_bind_value(value)


def _decode_bound_param(param: str | dict[str, Any]) -> object:
    """Decode one compiled BindValue to a Python value for the driver.

    Reverses ``_encode_bind_value``. Accepts either a JSON string (the
    ``compile_query`` JSON path) or an already-unpacked tagged dict (the
    ``compile_query_msgpack`` path, where ``bound_params`` is a single
    MessagePack blob unpacked to a list of dicts). Called on each element of
    ``compiled["bound_params"]``.
    """
    parsed: dict[str, Any] = json.loads(param) if isinstance(param, str) else param
    typ: str = parsed["type"]
    if typ == "null":
        return None
    val = parsed["value"]
    if typ == "bool":
        return bool(val)
    if typ == "int":
        return int(val)
    if typ == "float":
        return float(val)
    if typ == "text":
        return str(val)
    if typ == "bytes":
        return bytes(int(b) for b in val)
    if typ == "datetime":
        try:
            return datetime.fromisoformat(str(val))
        except (ValueError, TypeError):
            return str(val)
    if typ == "float_array":
        return [float(v) for v in val]
    if typ == "text_array":
        return [str(v) for v in val]
    if typ == "int_array":
        return [int(v) for v in val]
    return val


def _echo_compiled(
    conn: ConnectionLike,
    *,
    sql: str,
    bound_params: list[Any],
    compiled: dict[str, Any],
    model: str,
    operation: str,
    duration_ms: float | None = None,
    row_count: int | None = None,
    status: str = "ok",
) -> None:
    """Best-effort SQL echo for local debugging (never raises)."""
    from ferrum.echo import echo_sql

    echo_sql(
        sql=sql,
        bound_params=bound_params,
        param_type_summary=compiled.get("param_type_summary"),
        model=model,
        operation=operation,
        duration_ms=duration_ms,
        row_count=row_count,
        status=status,
        conn_echo=getattr(conn, "_echo", None),
    )


def _row_to_dict(row: Any) -> dict[str, Any]:  # noqa: ANN401
    """Convert a driver row (Record, sqlite3.Row, dict) to a plain dict."""
    if isinstance(row, dict):
        return row
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if hasattr(row, "keys"):
        # .keys() is required: iterating an asyncpg Record (or sqlite3.Row) yields
        # column *values*, not names, so `for k in row` would build a broken dict.
        return {k: row[k] for k in row.keys()}  # noqa: SIM118
    return dict(row)


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


class _RowEncoder(json.JSONEncoder):
    """JSON encoder for driver rows and non-JSON-native Python types.

    Used to serialize rows for the Rust ``hydrate_rows`` structural check.
    Non-JSON-native scalars are converted to strings — Rust performs
    structural validation (presence, nullability) only; Python retains native
    types for ``model_construct``.
    """

    def default(self, o: Any) -> Any:  # noqa: ANN401
        if hasattr(o, "_mapping"):
            return dict(o._mapping)
        if isinstance(o, (bytes, bytearray, memoryview)):
            return list(bytes(o))
        # Structural-only copy for the Rust presence/nullability check; the real
        # value keeps its native type for model_construct, so str() here is lossless.
        return str(o)


def _parse_lookup(lookup: str) -> tuple[str, str]:
    """Split ``field__operator`` lookup syntax into a field name and operator.

    Bare field names are equality lookups. Relation paths (``team__slug``) are
    handled by :func:`_resolve_lookup` before this helper is used for the base
    (non-relation) case — the split is intentionally from the right so the
    trailing segment is the operator.
    """
    if "__" in lookup:
        field_name, operator = lookup.rsplit("__", 1)
        return field_name, operator
    return lookup, "eq"


def _normalize_null_lookup(operator: str, value: object) -> tuple[str, object]:
    """Map Django-style ``__is_null=True/False`` onto ``is_null`` / ``is_not_null``.

    The SQL emitter treats ``is_null`` / ``is_not_null`` as nullary operators and
    ignores the bound value. Without this rewrite, ``field__is_null=False`` would
    still emit ``IS NULL``.
    """
    if operator == "is_null":
        if value is False:
            return "is_not_null", None
        return "is_null", None
    if operator == "is_not_null":
        if value is False:
            return "is_null", None
        return "is_not_null", None
    return operator, value


def _fk_oto_relations(metadata: ModelMetadata) -> dict[str, Any]:
    """Return ``{relation_name: RelationMeta}`` for forward FK / OneToOne only."""
    return {r.field_name: r for r in metadata.relations if r.kind in ("fk", "one_to_one")}


def _validate_lookup(
    field_name: str,
    operator: str,
    metadata: ModelMetadata,
    *,
    field_index: dict[str, int],
) -> None:
    """Validate a filter field/operator pair against immutable model metadata.

    This is the Python-side Stage 0 SQL safety gate: unknown field names and
    unsupported operators fail before an IR reaches Rust and before any SQL can
    be emitted. Values are deliberately not inspected here because they travel
    separately as bound ``BindValue`` payloads.
    """
    if field_name not in field_index:
        raise FerrumCompileError(
            f"Unknown field {field_name!r} on model {metadata.model_name!r}.",
            model=metadata.model_name,
            field=field_name,
        )
    allowed_ops = metadata.fields[field_index[field_name]].allowed_operators
    if operator not in allowed_ops:
        raise FerrumCompileError(
            f"Operator {operator!r} is not supported for field {field_name!r} "
            f"on model {metadata.model_name!r}.",
            model=metadata.model_name,
            field=field_name,
            operator=operator,
        )


_FTS_OPS = frozenset({"match", "match_phrase", "match_websearch", "match_boolean"})


def _resolve_lookup(
    lookup: str,
    metadata: ModelMetadata,
    field_index: dict[str, int],
) -> tuple[str, str, str | None, dict[str, int], ModelMetadata]:
    """Resolve a Django-style lookup into base or one-level relation form.

    Returns ``(field_name, operator, join_alias, target_field_index, target_meta)``.
    ``join_alias`` is ``None`` for base-table lookups.
    """
    relations = _fk_oto_relations(metadata)
    if "__" in lookup:
        first, rest = lookup.split("__", 1)
        if first in relations:
            from ferrum.registry import get_model

            rel = relations[first]
            remote = get_model(rel.to_model)
            remote_meta = remote.get_metadata()
            remote_index = {f.name: i for i, f in enumerate(remote_meta.fields)}
            if "__" in rest:
                remote_field, operator = rest.rsplit("__", 1)
                if remote_field not in remote_index:
                    # Could be multi-hop (team__owner__name) — not supported in v1.
                    if rest.split("__", 1)[0] in _fk_oto_relations(remote_meta):
                        raise FerrumCompileError(
                            f"Nested relation lookups are not supported "
                            f"({lookup!r}); use one level (e.g. 'team__slug').",
                            model=metadata.model_name,
                            field=lookup,
                        )
                    raise FerrumCompileError(
                        f"Unknown field {remote_field!r} on related model "
                        f"{remote_meta.model_name!r}.",
                        model=metadata.model_name,
                        field=lookup,
                    )
                _validate_lookup(remote_field, operator, remote_meta, field_index=remote_index)
                if operator in _FTS_OPS:
                    raise FerrumCompileError(
                        "Full-text operators are not supported on relation lookups.",
                        model=metadata.model_name,
                        field=lookup,
                        operator=operator,
                    )
                return remote_field, operator, first, remote_index, remote_meta
            # ``relation__field`` → equality on remote field.
            if rest not in remote_index:
                raise FerrumCompileError(
                    f"Unknown field {rest!r} on related model {remote_meta.model_name!r}.",
                    model=metadata.model_name,
                    field=lookup,
                )
            _validate_lookup(rest, "eq", remote_meta, field_index=remote_index)
            return rest, "eq", first, remote_index, remote_meta

    field_name, operator = _parse_lookup(lookup)
    _validate_lookup(field_name, operator, metadata, field_index=field_index)
    return field_name, operator, None, field_index, metadata


def _filter_dict_to_ir(
    flt: dict[str, Any],
    metadata: ModelMetadata,
    field_index: dict[str, int],
) -> dict[str, Any]:
    """Convert one normalized filter dict to a compiler-ready IR leaf."""
    field_name: str = flt["field"]
    operator, value = _normalize_null_lookup(flt["operator"], flt["value"])
    _validate_lookup(field_name, operator, metadata, field_index=field_index)
    return {
        "field": {"index": field_index[field_name], "name": field_name},
        "operator": operator,
        "value": _encode_filter_bind_value(
            metadata.fields[field_index[field_name]],
            operator,
            value,
        ),
    }


def _kwargs_to_ir_filters(
    kwargs: dict[str, Any],
    metadata: ModelMetadata,
    field_index: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    """Convert Django-style keyword lookups into validated predicate leaves.

    Returns ``(leaves, filter_joins)`` where ``filter_joins`` maps relation
    alias → set of remote field names needed for allowlisting.
    """
    leaves: list[dict[str, Any]] = []
    filter_joins: dict[str, set[str]] = {}
    for lookup, value in kwargs.items():
        field_name, operator, join_alias, target_index, target_meta = _resolve_lookup(
            lookup, metadata, field_index
        )
        # Validate against the caller's operator (``is_null``), then rewrite
        # ``is_null=False`` → ``is_not_null`` for the SQL emitter.
        operator, value = _normalize_null_lookup(operator, value)
        filt: dict[str, Any] = {
            "field": {"index": target_index[field_name], "name": field_name},
            "operator": operator,
            "value": _encode_filter_bind_value(
                target_meta.fields[target_index[field_name]],
                operator,
                value,
            ),
        }
        if join_alias is not None:
            filt["join_alias"] = join_alias
            filter_joins.setdefault(join_alias, set()).add(field_name)
        leaves.append({"kind": "filter", "filter": filt})
    return leaves, filter_joins


def _q_to_predicate(
    q: Q,
    metadata: ModelMetadata,
    field_index: dict[str, int],
) -> tuple[dict[str, Any], dict[str, set[str]]]:
    """Serialize a ``Q`` boolean tree into the IR predicate shape.

    Each leaf is validated through the same metadata allowlist as plain
    ``filter(**kwargs)``. Empty ``Q()`` objects are rejected because compiling an
    empty predicate would make caller intent ambiguous for destructive terminals.

    Returns ``(predicate_ir, filter_joins)`` where ``filter_joins`` maps relation
    alias → remote field names referenced by relation lookups.
    """
    filter_joins: dict[str, set[str]] = {}

    def walk(node: Q) -> dict[str, Any]:
        children_ir: list[dict[str, Any]] = []
        for child in node.children:
            if isinstance(child, Q):
                children_ir.append(walk(child))
            elif isinstance(child, dict):
                leaves, joins = _kwargs_to_ir_filters(child, metadata, field_index)
                for alias, fields in joins.items():
                    filter_joins.setdefault(alias, set()).update(fields)
                if len(leaves) == 1:
                    children_ir.append(leaves[0])
                else:
                    children_ir.append({"kind": "and", "children": leaves})
            else:
                msg = f"Unsupported Q child type: {type(child)!r}."
                raise TypeError(msg)
        if not children_ir:
            raise FerrumCompileError(
                f"Empty Q object on model {metadata.model_name!r}.",
                model=metadata.model_name,
            )
        if len(children_ir) == 1:
            inner = children_ir[0]
        else:
            inner = {"kind": node.connector, "children": children_ir}
        if node.negated:
            return {"kind": "not", "child": inner}
        return inner

    return walk(q), filter_joins


def _hydrate_rows(
    model: type[_M],
    rows: list[Any],
    *,
    fingerprint: str = "",
    deferred: frozenset[str] | None = None,
) -> list[_M]:
    """Convert DB rows to model instances (ADR-003 trusted path).

    When the native extension is available, delegates structural validation
    (non-nullable column checks) to ``_native_ext.hydrate_rows()`` before
    calling ``model_construct``. Rust validation runs on a JSON-serialized copy
    of the rows so Python retains native types (datetime, UUID, etc.) for the
    actual ``model_construct`` call.

    Uses ``model_construct`` (skip re-validation) since rows originate from a
    trusted DB source. Custom validators with side-effects do not re-run here.

    On hydration failure:
    - Dispatches a Tier A ``hydration_failure`` hook.
    - Raises ``FerrumHydrationError`` (remapped via ``map_native_error``).
    """
    row_dicts = [_row_to_dict(row) for row in rows]

    if _native_ext is not None:
        try:
            metadata = model.get_metadata() if hasattr(model, "get_metadata") else None
        except Exception:
            metadata = None

        if metadata is not None:
            try:
                if _WIRE_FORMAT == "msgpack":
                    msgpack = _require_msgpack()
                    meta_mp = msgpack.packb(metadata.to_metadata_dict(), use_bin_type=True)
                    rows_mp = msgpack.packb(
                        row_dicts, default=_msgpack_row_default, use_bin_type=True
                    )
                    _native_ext.hydrate_rows_msgpack(meta_mp, rows_mp)
                else:
                    metadata_json = metadata.to_metadata_json()
                    rows_json = json.dumps(row_dicts, cls=_RowEncoder)
                    _native_ext.hydrate_rows(metadata_json, rows_json)
            except Exception as exc:
                mapped = map_native_error(exc, _native_mod=_native_ext)
                _hooks.hydration_failure(
                    fingerprint=fingerprint,
                    failure_category=type(mapped).__name__,
                    model=model.__name__,
                )
                raise mapped from exc

    instances = [model.model_construct(**_coerce_hydrated_row(model, row)) for row in row_dicts]
    if deferred:
        for inst in instances:
            object.__setattr__(inst, "__ferrum_deferred__", deferred)
    return instances


class _QuerySetBase(Generic[_R]):
    """Row-type-generic base for all Ferrum querysets.

    Holds the shared, model-agnostic query state (filters, ordering, limit,
    offset, select/defer projection, relation hints), the IR builder, the Rust
    ``_compile`` boundary, and the shared async terminals (``all``, ``first``,
    ``get``, ``count``, ``exists``). Chaining methods return ``Self`` so a chain
    preserves the concrete subclass type.

    Row shaping is delegated to the overridable ``_materialize`` hook so each
    concrete subclass (``QuerySet``, ``ValuesQuerySet``, ``ValuesListQuerySet``,
    ``FlatValuesListQuerySet``) produces its own precise result type.

    ``_build_ir()`` serializes the accumulated state to the ADR-002 v1 IR shape
    (a plain dict) without touching the database or emitting any SQL.
    """

    def __init__(self, model: type[Model]) -> None:
        self._model: type[Model] = model
        self._filters: list[dict[str, Any]] = []
        self._order_by: list[dict[str, Any]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._is_filtered: bool = False
        self._vector_order_by: dict[str, Any] | None = None
        self._text_rank_by: dict[str, Any] | None = None
        self._predicate_q: Q | None = None
        self._distinct: bool = False
        self._only_fields: tuple[str, ...] | None = None
        self._defer_fields: frozenset[str] = frozenset()
        self._select_related: tuple[str, ...] = ()
        self._prefetch_related: tuple[str, ...] = ()
        self._aggregate_groups: list[dict[str, str]] = []
        self._having: list[dict[str, Any]] = []
        # When set by ``QuerySet.project()``, rows hydrate into this model while
        # IR compilation continues against ``_model`` (source table / filters).
        self._hydrate_model: type[Model] | None = None

    # ------------------------------------------------------------------
    # Chaining methods (return a new queryset of the same type — no I/O, no SQL)
    # ------------------------------------------------------------------

    def filter(self, *args: Q | dict[str, Any], **kwargs: Any) -> Self:  # noqa: ANN401
        """Add filter(s), including ``Q`` boolean trees. Returns a new QuerySet.

        Uses Django-style ``field__operator=value`` syntax; bare ``field=value``
        is the ``eq`` lookup. One-level relation lookups
        (``team__slug=...`` / ``Q(team__slug=...)``) auto-INNER-JOIN the related
        table. Field names are validated against the model metadata allowlist at
        call time (Stage 0 first gate, QUERY_ENGINE.md §6).
        """
        q = args_to_q(*args, **kwargs)
        if q is None:
            return self._clone()
        qs = self._clone()
        metadata = self._get_metadata()
        if metadata is not None:
            field_index = {f.name: i for i, f in enumerate(metadata.fields)}
            _q_to_predicate(q, metadata, field_index)  # Stage-0 validate only
        qs._predicate_q = q if qs._predicate_q is None else qs._predicate_q & q
        qs._is_filtered = True
        return qs

    def exclude(self, *args: Q | dict[str, Any], **kwargs: Any) -> Self:  # noqa: ANN401
        """Exclude rows matching the given lookups (``~Q(...)``)."""
        q = args_to_q(*args, **kwargs)
        if q is None:
            return self._clone()
        return self.filter(~q)

    def distinct(self) -> Self:
        """Return a QuerySet that emits ``SELECT DISTINCT``."""
        qs = self._clone()
        qs._distinct = True
        return qs

    def only(self, *fields: str) -> Self:
        """Limit SELECT columns; deferred fields raise on access."""
        qs = self._clone()
        qs._only_fields = fields
        qs._defer_fields = frozenset()
        return qs

    def defer(self, *fields: str) -> Self:
        """Defer loading of the given fields."""
        qs = self._clone()
        qs._defer_fields = frozenset(fields)
        qs._only_fields = None
        return qs

    def __getitem__(self, key: slice | int) -> Self:
        """Return a sliced QuerySet using offset/limit shorthand.

        ``qs[10:20]`` is equivalent to ``qs.offset(10).limit(10)`` and remains
        lazy. Integer indexing is intentionally unsupported because it would
        imply immediate I/O or surprising ``LIMIT 1`` semantics.
        """
        if isinstance(key, slice):
            qs = self
            start = key.start if key.start is not None else 0
            stop = key.stop
            if key.start is not None:
                qs = qs.offset(start)
            if stop is not None:
                limit = stop - start if key.start is not None else stop
                qs = qs.limit(limit)
            return qs
        msg = "QuerySet indices must be slices."
        raise TypeError(msg)

    def order_by(self, *fields: str) -> Self:
        """Set ORDER BY. Prefix field with '-' for DESC. Returns a new QuerySet."""
        qs = self._clone()
        for f in fields:
            if f.startswith("-"):
                qs._order_by.append({"field": f[1:], "direction": "desc"})
            else:
                qs._order_by.append({"field": f, "direction": "asc"})
        return qs

    def limit(self, count: int) -> Self:
        """Set LIMIT. Returns a new QuerySet."""
        qs = self._clone()
        qs._limit = count
        return qs

    def offset(self, count: int) -> Self:
        """Set OFFSET. Returns a new QuerySet."""
        qs = self._clone()
        qs._offset = count
        return qs

    def group_by(self, *fields: str) -> Self:
        """Add metadata-allowlisted fields to an aggregate GROUP BY."""
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        field_names = {field.name for field in metadata.fields}
        qs = self._clone()
        used_labels = {group["label"] for group in qs._aggregate_groups}
        for field in fields:
            if field not in field_names:
                raise FerrumCompileError(
                    f"Unknown field {field!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=field,
                )
            if field in used_labels:
                raise FerrumCompileError(
                    f"Duplicate aggregate result key {field!r}.",
                    model=metadata.model_name,
                    field=field,
                )
            qs._aggregate_groups.append({"kind": "field", "field": field, "label": field})
            used_labels.add(field)
        return qs

    def date_trunc(
        self,
        field: str,
        granularity: DateTruncGranularity,
        *,
        alias: str = "bucket",
    ) -> Self:
        """Add a fixed DATE_TRUNC bucket to an aggregate GROUP BY."""
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        field_index = {item.name: index for index, item in enumerate(metadata.fields)}
        if field not in field_index:
            raise FerrumCompileError(
                f"Unknown field {field!r} on model {metadata.model_name!r}.",
                model=metadata.model_name,
                field=field,
            )
        if metadata.fields[field_index[field]].field_type not in ("date", "datetime"):
            raise FerrumCompileError(
                f"date_trunc() requires a date or datetime field; got {field!r}.",
                model=metadata.model_name,
                field=field,
                operator="date_trunc",
            )
        allowed: tuple[DateTruncGranularity, ...] = (
            "minute",
            "hour",
            "day",
            "week",
            "month",
            "quarter",
            "year",
        )
        if granularity not in allowed:
            raise FerrumCompileError(
                f"Unsupported date_trunc granularity {granularity!r}.",
                model=metadata.model_name,
                field=field,
                operator="date_trunc",
            )
        if not alias or alias in {group["label"] for group in self._aggregate_groups}:
            raise FerrumCompileError(
                f"Duplicate or empty aggregate result key {alias!r}.",
                model=metadata.model_name,
            )
        qs = self._clone()
        qs._aggregate_groups.append(
            {
                "kind": "date_trunc",
                "field": field,
                "granularity": granularity,
                "label": alias,
            }
        )
        return qs

    def having(self, **conditions: Any) -> Self:  # noqa: ANN401
        """Add bound HAVING comparisons against aggregate result keys."""
        qs = self._clone()
        for lookup, value in conditions.items():
            alias, operator = _parse_lookup(lookup)
            if operator not in ("eq", "ne", "gt", "gte", "lt", "lte"):
                raise FerrumCompileError(
                    f"Unsupported HAVING operator {operator!r}.",
                    model=self._model.__name__,
                    operator=operator,
                )
            qs._having.append({"alias": alias, "operator": operator, "value": value})
        return qs

    # ------------------------------------------------------------------
    # IR builder (no I/O, no SQL — QUERY_ENGINE.md §6 Stage 0)
    # ------------------------------------------------------------------

    def _build_ir(self) -> dict[str, Any]:
        """Serialize current QuerySet state to the ADR-002 v1 IR dict.

        Validates field names and operators against the model's ``ModelMetadata``
        allowlist. Raises ``FerrumCompileError`` for unknown fields or unsupported
        operators **before** any SQL is produced.

        Returns a plain Python dict matching the JSON-serializable IR shape::

            {
                "version": 1,
                "model_name": "User",
                "operation": {"kind": "select", "fields": [{"index": 0, "name": "id"}]},
                "filters": [...],
                "order_by": [...],
                "limit": 10,
                "offset": null,
            }
        """
        metadata: ModelMetadata | None = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata. "
                "Ensure it defines at least one field.",
                model=self._model.__name__,
            )

        field_index: dict[str, int] = {f.name: i for i, f in enumerate(metadata.fields)}

        select_names = self._resolve_select_field_names(metadata)
        for name in select_names:
            if name not in field_index:
                raise FerrumCompileError(
                    f"Unknown field {name!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=name,
                )
        select_fields = [{"index": field_index[name], "name": name} for name in select_names]
        operation: dict[str, Any] = {"kind": "select", "fields": select_fields}

        # Filters — validate field names and operators against allowlists.
        ir_filters: list[dict[str, Any]] = []
        for flt in self._filters:
            ir_filters.append(_filter_dict_to_ir(flt, metadata, field_index))

        # Order by — validate field names and sort directions against allowlists.
        ir_order_by: list[dict[str, Any]] = []
        for ord_item in self._order_by:
            field_name = ord_item["field"]
            direction: str = ord_item["direction"]
            if field_name not in field_index:
                raise FerrumCompileError(
                    f"Unknown field {field_name!r} in order_by on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=field_name,
                )
            if direction not in metadata.allowed_sort_directions:
                raise FerrumCompileError(
                    f"Invalid sort direction {direction!r}.",
                    model=metadata.model_name,
                    field=field_name,
                )
            ir_order_by.append(
                {
                    "field": {"index": field_index[field_name], "name": field_name},
                    "direction": direction,
                }
            )

        ir: dict[str, Any] = {
            "version": _IR_VERSION,
            "model_name": metadata.model_name,
            "operation": operation,
            "filters": ir_filters,
            "order_by": ir_order_by,
            "limit": self._limit,
            "offset": self._offset,
            "distinct": self._distinct,
            "exists": False,
        }
        filter_joins: dict[str, set[str]] = {}
        if self._predicate_q is not None:
            predicate_ir, filter_joins = _q_to_predicate(self._predicate_q, metadata, field_index)
            ir["predicate"] = predicate_ir

        from ferrum.relations import build_join_ir

        joins: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in self._select_related:
            joins.append(
                build_join_ir(
                    metadata,
                    name,
                    field_index,
                    join_kind="left",
                    project_remote=True,
                )
            )
            seen.add(name)
        for alias, remote_fields in filter_joins.items():
            if alias in seen:
                # Already LEFT JOINed via select_related; WHERE still applies.
                continue
            joins.append(
                build_join_ir(
                    metadata,
                    alias,
                    field_index,
                    join_kind="inner",
                    project_remote=False,
                    remote_field_names=frozenset(remote_fields),
                )
            )
        ir["joins"] = joins
        if self._vector_order_by is not None:
            ir["vector_order_by"] = self._vector_order_by
        if self._text_rank_by is not None:
            ir["text_rank_by"] = self._text_rank_by
        return ir

    def _build_exists_ir(self) -> dict[str, Any]:
        """Build IR for ``exists()`` — ``SELECT EXISTS(subquery)``."""
        ir = self._build_ir()
        ir["exists"] = True
        return ir

    def _resolve_select_field_names(self, metadata: ModelMetadata) -> list[str]:
        """Return the model field names that should appear in the SELECT list."""
        all_names = [f.name for f in metadata.fields]
        if self._only_fields is not None:
            return list(self._only_fields)
        if self._defer_fields:
            return [name for name in all_names if name not in self._defer_fields]
        return all_names

    def _deferred_field_names(self, metadata: ModelMetadata) -> frozenset[str] | None:
        """Return field names that should raise on attribute access after hydration."""
        if self._only_fields is not None:
            loaded = frozenset(self._only_fields)
            return frozenset(f.name for f in metadata.fields if f.name not in loaded)
        if self._defer_fields:
            return frozenset(self._defer_fields)
        return None

    def _compile(self, *, dialect: str = "postgres") -> dict[str, Any]:
        """Compile the validated IR through the native Rust extension.

        Calls ``_build_ir()`` first so that all Python-side allowlist checks
        (field names, operators, sort directions) fire before the Rust layer is
        invoked.  Any ``FerrumCompileError`` raised by either layer propagates
        directly to the caller.

        Raises:
            FerrumConfigError: if the ``ferrum._native`` wheel has not been built.
            FerrumCompileError: for unknown fields / operators (Python guard) or
                any additional validation the Rust compiler applies.
        """
        # ADR-006: _native.FerrumCompileError (RuntimeError) != ferrum.errors.FerrumCompileError
        # (FerrumError). Centralized remapping tracked for Wave 3/ADR-006.
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        return self._compile_ir(self._build_ir(), dialect=dialect)

    def _compile_ir(self, ir: dict[str, Any], *, dialect: str = "postgres") -> dict[str, Any]:
        """Invoke the native Rust compiler on a pre-built IR dict.

        Unlike ``_compile()``, accepts any IR dict (select/insert/update/delete)
        rather than always building a SELECT from ``_build_ir()``. The caller is
        responsible for constructing a valid IR and running Python-side allowlist
        checks before calling this method.

        Raises:
            FerrumConfigError: if the ``ferrum._native`` wheel has not been built.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        metadata = self._get_metadata()
        if _WIRE_FORMAT == "msgpack":
            return self._compile_ir_msgpack(ir, metadata, dialect=dialect)
        ir_json = json.dumps(ir)
        metadata_json = metadata.to_metadata_json() if metadata is not None else "{}"
        try:
            return _native_ext.compile_query(metadata_json, ir_json, dialect)  # type: ignore[return-value]
        except Exception as exc:
            raise map_native_error(exc, _native_mod=_native_ext) from exc

    def _compile_ir_msgpack(
        self,
        ir: dict[str, Any],
        metadata: ModelMetadata | None,
        *,
        dialect: str,
    ) -> dict[str, Any]:
        """Compile via the MessagePack boundary, normalizing ``bound_params``.

        ``compile_query_msgpack`` returns ``bound_params`` as a single
        MessagePack blob (the NAMED encoder, so tagged ``BindValue`` dicts
        round-trip). It is unpacked here into a list of tagged dicts so callers'
        ``_decode_bound_param`` consumes both wire formats identically.
        """
        msgpack = _require_msgpack()
        assert _native_ext is not None  # guarded by caller  # noqa: S101
        metadata_dict = metadata.to_metadata_dict() if metadata is not None else {}
        meta_mp = msgpack.packb(metadata_dict, use_bin_type=True)
        ir_mp = msgpack.packb(ir, use_bin_type=True)
        try:
            compiled: dict[str, Any] = _native_ext.compile_query_msgpack(meta_mp, ir_mp, dialect)
        except Exception as exc:
            raise map_native_error(exc, _native_mod=_native_ext) from exc
        compiled["bound_params"] = msgpack.unpackb(compiled["bound_params"], raw=False)
        return compiled

    def to_ir_json(self) -> str:
        """Serialize the current QuerySet state to the ADR-002 v1 IR JSON string.

        This is the ``ir_json`` argument for ``ferrum._native.compile_query``.
        Calls ``_build_ir()`` internally, so all Python-side allowlist checks
        fire before serialization.
        """
        return json.dumps(self._build_ir())

    # ------------------------------------------------------------------
    # Terminal coroutines (async) — require open Connection
    # ------------------------------------------------------------------

    async def all(self, conn: ConnectionLike) -> list[_R]:
        """Fetch all matching rows and return them shaped for this queryset.

        Compiles the QuerySet IR via the Rust extension, executes the
        parameterized SQL against the pool, and delegates row shaping to
        :meth:`_materialize` (model hydration for ``QuerySet`` — the ADR-003
        trusted ``model_construct`` path — or dict/tuple/scalar shaping for the
        value querysets).

        Dispatches Tier A ``query_start`` / ``query_success`` / ``query_failure``
        hook payloads (non-bypassable redaction via ``hooks.dispatch``).

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).

        Raises:
            FerrumConfigError: if the native extension is not built.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        metadata = self._get_metadata()
        compiled = self._compile(dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="select",
            table=table,
        )
        t0 = time.monotonic()
        try:
            rows = await driver.fetch(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "select"})
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            _echo_compiled(
                conn,
                sql=sql_text,
                bound_params=bound_params,
                compiled=compiled,
                model=model_name,
                operation="select",
                duration_ms=duration_ms,
                status="error",
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=len(rows),
        )
        _echo_compiled(
            conn,
            sql=sql_text,
            bound_params=bound_params,
            compiled=compiled,
            model=model_name,
            operation="select",
            duration_ms=duration_ms,
            row_count=len(rows),
        )
        return await self._materialize(rows, metadata, conn, fingerprint)

    @contextlib.asynccontextmanager
    async def stream(
        self,
        conn: ConnectionLike,
        *,
        chunk_size: int = 1000,
    ) -> AsyncGenerator[AsyncIterator[list[_R]], None]:
        """Stream matching rows as bounded, materialized chunks.

        The context pins the underlying cursor resources until exit. Early loop
        termination, exceptions, and cancellation deterministically close the
        cursor. ``prefetch_related()`` is rejected because relationship batching
        across independently consumed chunks would change its query semantics.
        """
        if self._prefetch_related:
            raise FerrumCompileError(
                "stream() cannot be combined with prefetch_related(); "
                "consume all() or load related rows explicitly.",
                model=self._model.__name__,
            )
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1.")
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        metadata = self._get_metadata()
        compiled = self._compile(dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(param) for param in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        opaque = _compiled_query(sql_text, bound_params)
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="select_stream",
            table=table,
        )
        started = time.monotonic()
        row_count = 0

        async with conn.stream_compiled(opaque, chunk_size=chunk_size) as raw_chunks:

            async def materialized_chunks() -> AsyncGenerator[list[_R], None]:
                nonlocal row_count
                while True:
                    try:
                        rows = await anext(raw_chunks)
                    except StopAsyncIteration:
                        return
                    except Exception as exc:
                        duration_ms = (time.monotonic() - started) * 1000
                        mapped = map_db_error(
                            exc, context={"model": model_name, "operation": "select_stream"}
                        )
                        _hooks.query_failure(
                            fingerprint=fingerprint,
                            duration_ms=duration_ms,
                            failure_category=type(mapped).__name__,
                        )
                        _echo_compiled(
                            conn,
                            sql=sql_text,
                            bound_params=bound_params,
                            compiled=compiled,
                            model=model_name,
                            operation="select_stream",
                            duration_ms=duration_ms,
                            status="error",
                        )
                        raise mapped from None
                    chunk = await self._materialize(list(rows), metadata, conn, fingerprint)
                    row_count += len(chunk)
                    yield chunk

            chunks = materialized_chunks()
            try:
                yield chunks
            finally:
                await chunks.aclose()

        duration_ms = (time.monotonic() - started) * 1000
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=row_count,
        )
        _echo_compiled(
            conn,
            sql=sql_text,
            bound_params=bound_params,
            compiled=compiled,
            model=model_name,
            operation="select_stream",
            duration_ms=duration_ms,
            row_count=row_count,
        )

    async def _materialize(
        self,
        rows: list[Any],
        metadata: ModelMetadata | None,
        conn: ConnectionLike,
        fingerprint: str,
    ) -> list[_R]:
        """Shape raw driver rows into this queryset's concrete result type.

        Overridden by each concrete subclass. Never called on ``_QuerySetBase``
        itself (it is abstract in practice — no public constructor path yields a
        bare base instance).
        """
        raise NotImplementedError  # pragma: no cover — concrete subclasses override

    async def first(self, conn: ConnectionLike) -> _R | None:
        """Fetch the first matching row, or ``None`` if no rows match.

        Applies ``LIMIT 1`` to avoid fetching unnecessary rows.

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).

        Raises:
            FerrumConfigError: if the native extension is not built.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        results = await self.limit(1).all(conn)
        return results[0] if results else None

    async def get(self, conn: ConnectionLike, **kwargs: Any) -> _R:  # noqa: ANN401
        """Fetch exactly one matching row, applying optional extra filters.

        Returns the single matching model instance.

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).
            **kwargs: Additional filter lookups (same syntax as ``filter()``).

        Raises:
            FerrumConfigError: if the native extension is not built.
            FerrumNotFoundError: if no rows match.
            FerrumMultipleObjectsError: if more than one row matches.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        qs: Self = self.filter(**kwargs) if kwargs else self
        # Fetch at most 2 rows: enough to detect "multiple objects" without
        # pulling the full result set.
        results = await qs.limit(2).all(conn)
        model_name = self._model.__name__
        if len(results) == 0:
            raise FerrumNotFoundError(f"{model_name} matching query does not exist. [FERR-Q404]")
        if len(results) > 1:
            raise FerrumMultipleObjectsError(
                f"get() returned more than one {model_name}. "
                "Use filter() to narrow the query. [FERR-Q405]"
            )
        return results[0]

    async def count(self, conn: ConnectionLike) -> int:
        """Return the count of rows matching the current filters.

        Rewrites the compiled SELECT to ``SELECT COUNT(*) FROM ...`` so that
        LIMIT/OFFSET are not applied and no row hydration is needed.

        Dispatches Tier A ``query_start`` / ``query_success`` / ``query_failure``
        hook payloads (non-bypassable redaction via ``hooks.dispatch``).

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).

        Raises:
            FerrumConfigError: if the native extension is not built.
            FerrumInternalError: if the compiler emits an unexpected SQL shape
                that prevents the COUNT(*) rewrite (W-1 guard).
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        # Compile without LIMIT/OFFSET — count operates on the full filter set.
        count_qs = self._clone()
        count_qs._limit = None
        count_qs._offset = None
        compiled = count_qs._compile(dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        # Rewrite the SELECT projection to COUNT(*).  The emitter always emits
        # ``SELECT {cols} FROM {table} ...``; the first " FROM " token separates
        # the projection from the rest of the statement.  Column/table names from
        # ModelMetadata cannot contain " FROM " so this split is safe (SQL-1).
        # W-1: wrap ValueError to surface compiler shape changes as FerrumInternalError.
        try:
            from_idx = sql_text.index(" FROM ")
        except ValueError as exc:
            raise FerrumInternalError(
                "Internal error: SQL compiler emitted an unexpected shape for "
                "count() rewrite (no ' FROM ' token found). [FERR-E500]"
            ) from exc
        count_sql = "SELECT COUNT(*)" + sql_text[from_idx:]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        metadata = self._get_metadata()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="count",
            table=table,
        )
        t0 = time.monotonic()
        try:
            result = await driver.fetchval(count_sql, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "count"})
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        count_val = int(result or 0)
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=count_val,
        )
        return count_val

    async def exists(self, conn: ConnectionLike) -> bool:
        """Return whether any row matches without hydrating rows.

        The compiler emits an ``EXISTS`` operation rather than fetching a row and
        discarding it, so this terminal is the cheapest presence check and still
        emits Tier A hook payloads only.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        metadata = self._get_metadata()
        compiled = self._compile_ir(self._build_exists_ir(), dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="exists",
            table=table,
        )
        t0 = time.monotonic()
        try:
            result = await driver.fetchval(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "exists"})
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=1 if result else 0,
        )
        return bool(result)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clone(self) -> Self:
        """Copy accumulated query state for immutable chaining.

        Uses ``type(self)`` so the concrete subclass (model / values / values-list
        / flat) is preserved across a chain, then copies shared state via
        :meth:`_copy_state_into`.
        """
        qs = type(self)(self._model)
        self._copy_state_into(qs)
        return qs

    def _copy_state_into(self, qs: _QuerySetBase[Any]) -> None:
        """Copy accumulated shared query state into another queryset instance.

        Used by :meth:`_clone` and by ``QuerySet.values()`` / ``values_list()``
        when transferring state to a sibling result-type queryset.
        """
        qs._filters = list(self._filters)
        qs._order_by = list(self._order_by)
        qs._limit = self._limit
        qs._offset = self._offset
        qs._is_filtered = self._is_filtered
        qs._vector_order_by = (
            dict(self._vector_order_by) if self._vector_order_by is not None else None
        )
        qs._text_rank_by = dict(self._text_rank_by) if self._text_rank_by is not None else None
        qs._predicate_q = self._predicate_q
        qs._distinct = self._distinct
        qs._only_fields = self._only_fields
        qs._defer_fields = self._defer_fields
        qs._select_related = self._select_related
        qs._prefetch_related = self._prefetch_related
        qs._hydrate_model = self._hydrate_model
        qs._aggregate_groups = [dict(group) for group in self._aggregate_groups]
        qs._having = [dict(condition) for condition in self._having]

    def _get_metadata(self) -> ModelMetadata | None:
        """Return the model's ``ModelMetadata`` if available, else ``None``."""
        return getattr(self._model, "__ferrum_metadata__", None)


class QuerySet(_QuerySetBase[_M], Generic[_M]):
    """Lazy, chainable query builder for a Ferrum model.

    The model-facing queryset. Terminals return model instances
    (``all -> list[_M]``, ``first -> _M | None``, ``get -> _M``); ``values()`` /
    ``values_list()`` switch to the value-shaped sibling querysets. All
    chaining methods return a new instance (immutable chaining).
    """

    # Narrow the base ``_model`` (``type[Model]``) to the concrete model type so
    # ``create``/``bulk_*`` return precise ``_M`` instances.
    _model: type[_M]

    def __init__(self, model: type[_M]) -> None:
        super().__init__(model)

    # ------------------------------------------------------------------
    # Model-only chaining (return QuerySet[_M])
    # ------------------------------------------------------------------

    def select_related(self, *relations: str) -> QuerySet[_M]:
        """Eager-load to-one relations via JOIN (ForeignKey / OneToOne)."""
        qs = self._clone()
        metadata = self._get_metadata()
        if metadata is not None:
            for name in relations:
                from ferrum.relations import resolve_relation

                rel = resolve_relation(metadata, name)
                if rel.kind not in ("fk", "one_to_one"):
                    raise FerrumCompileError(
                        f"select_related() only supports ForeignKey and OneToOne; "
                        f"{name!r} is {rel.kind!r}. Use prefetch_related() instead.",
                        model=metadata.model_name,
                        field=name,
                    )
        qs._select_related = qs._select_related + relations
        return qs

    def prefetch_related(self, *relations: str) -> QuerySet[_M]:
        """Eager-load to-many / M2M / reverse FK via batched queries."""
        qs = self._clone()
        metadata = self._get_metadata()
        if metadata is not None:
            from ferrum.relations import resolve_prefetch_name

            for name in relations:
                resolve_prefetch_name(metadata, name)
        qs._prefetch_related = qs._prefetch_related + relations
        return qs

    def nearest_to(
        self,
        field: str,
        vector: list[float],
        *,
        metric: Literal["l2", "cosine", "inner_product"] = "l2",
    ) -> QuerySet[_M]:
        """Order results by vector distance (pgvector KNN).

        Appends a ``vector_order_by`` node to the IR, compiled to
        ``ORDER BY col <-> $n`` (or ``<=>`` / ``<#>`` for other metrics).
        """
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        field_index = {f.name: i for i, f in enumerate(metadata.fields)}
        if field not in field_index:
            raise FerrumCompileError(
                f"Unknown field {field!r} on model {metadata.model_name!r}.",
                model=metadata.model_name,
                field=field,
            )
        field_meta = metadata.fields[field_index[field]]
        if field_meta.field_type != "vector":
            raise FerrumCompileError(
                f"nearest_to() requires a vector field; {field!r} is {field_meta.field_type!r}.",
                model=metadata.model_name,
                field=field,
            )
        qs = self._clone()
        # Text literal + SQL ``::vector`` cast (see ferrum-sql emit). Avoids
        # asyncpg binding ``list[float]`` as ``float[]`` (DataError on pgvector).
        qs._vector_order_by = {
            "field": {"index": field_index[field], "name": field},
            "metric": metric,
            "value": _encode_bind_value(_encode_vector_literal(vector)),
        }
        return qs

    def rank_by(
        self,
        field: str,
        query: str,
        *,
        mode: Literal["plain", "phrase", "websearch", "boolean"] = "plain",
    ) -> QuerySet[_M]:
        """Order results by full-text relevance (``text_rank_by`` IR node)."""
        if mode not in _TEXT_SEARCH_MODES:
            raise FerrumCompileError(
                f"Invalid text search mode {mode!r}.",
                model=self._model.__name__,
            )
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        field_index = {f.name: i for i, f in enumerate(metadata.fields)}
        if field not in field_index:
            raise FerrumCompileError(
                f"Unknown field {field!r} on model {metadata.model_name!r}.",
                model=metadata.model_name,
                field=field,
            )
        field_meta = metadata.fields[field_index[field]]
        if field_meta.field_type not in ("tsvector", "text"):
            raise FerrumCompileError(
                f"rank_by() requires a full-text field; {field!r} is {field_meta.field_type!r}.",
                model=metadata.model_name,
                field=field,
            )
        if self._vector_order_by is not None:
            raise FerrumCompileError(
                "Cannot combine nearest_to() and rank_by() on the same QuerySet.",
                model=metadata.model_name,
            )
        _, ir_mode = _TEXT_SEARCH_MODES[mode]
        qs = self._clone()
        qs._text_rank_by = {
            "field": {"index": field_index[field], "name": field},
            "query": _encode_bind_value(query),
            "mode": ir_mode,
        }
        return qs

    def search(
        self,
        query: str,
        *,
        field: str,
        mode: Literal["plain", "phrase", "websearch", "boolean"] = "plain",
    ) -> QuerySet[_M]:
        """Filter and rank by full-text relevance on ``field``."""
        if mode not in _TEXT_SEARCH_MODES:
            raise FerrumCompileError(
                f"Invalid text search mode {mode!r}.",
                model=self._model.__name__,
            )
        operator, _ = _TEXT_SEARCH_MODES[mode]
        return self.filter(**{f"{field}__{operator}": query}).rank_by(field, query, mode=mode)

    # ------------------------------------------------------------------
    # Result-shape switches (return value-typed sibling querysets)
    # ------------------------------------------------------------------

    def project(self, model: type[_P]) -> QuerySet[_P]:
        """Hydrate rows into a different model that maps to the same table.

        Typical use: query a wide model (e.g. ``Ticket`` with a vector column)
        and return a narrower read model (e.g. ``TicketRead``)::

            await (
                Ticket.objects.filter(...)
                .nearest_to("summary_embedding", vec)
                .project(TicketRead)
                .all(conn)
            )

        SELECT columns are restricted to fields shared by the source and target
        models. Filters / ``nearest_to`` / joins continue to compile against the
        source model. The target model must declare the same ``table`` name.
        """
        source_meta = self._get_metadata()
        if source_meta is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        target_meta = getattr(model, "__ferrum_metadata__", None)
        if target_meta is None:
            raise FerrumCompileError(
                f"Model {model.__name__!r} has no metadata.",
                model=model.__name__,
            )
        if source_meta.table_name != target_meta.table_name:
            raise FerrumCompileError(
                f"project() requires the same table; "
                f"{source_meta.model_name!r} maps to {source_meta.table_name!r}, "
                f"{target_meta.model_name!r} maps to {target_meta.table_name!r}.",
                model=source_meta.model_name,
            )
        source_names = {f.name for f in source_meta.fields}
        shared = tuple(f.name for f in target_meta.fields if f.name in source_names)
        if not shared:
            raise FerrumCompileError(
                f"project({model.__name__!r}) shares no fields with {source_meta.model_name!r}.",
                model=source_meta.model_name,
            )
        qs = self._clone()
        qs._hydrate_model = model
        qs._only_fields = shared
        # Type checkers: result terminals return ``model`` instances.
        return cast(QuerySet[_P], qs)

    def values(self, *fields: str) -> ValuesQuerySet:
        """Return rows as dicts instead of model instances."""
        qs = ValuesQuerySet(self._model)
        self._copy_state_into(qs)
        if fields:
            qs._only_fields = fields
        return qs

    @overload
    def values_list(self, *fields: str, flat: Literal[True]) -> FlatValuesListQuerySet: ...
    @overload
    def values_list(self, *fields: str, flat: Literal[False] = False) -> ValuesListQuerySet: ...

    def values_list(
        self, *fields: str, flat: bool = False
    ) -> ValuesListQuerySet | FlatValuesListQuerySet:
        """Return rows as tuples (or flat scalars when ``flat=True``)."""
        target: ValuesListQuerySet | FlatValuesListQuerySet = (
            FlatValuesListQuerySet(self._model) if flat else ValuesListQuerySet(self._model)
        )
        self._copy_state_into(target)
        if fields:
            target._only_fields = fields
        return target

    def _build_aggregate_ir(
        self, expressions: dict[str, Aggregate]
    ) -> tuple[dict[str, Any], list[str]]:
        """Build typed aggregate IR and its user-facing result-key order."""
        if not expressions:
            raise FerrumCompileError(
                "aggregate() requires at least one named expression.",
                model=self._model.__name__,
            )
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        if (
            self._select_related
            or self._vector_order_by is not None
            or self._text_rank_by is not None
        ):
            raise FerrumCompileError(
                "aggregate() cannot be combined with joins or ranking.",
                model=metadata.model_name,
            )
        if self._order_by or self._distinct:
            raise FerrumCompileError(
                "aggregate() cannot be combined with order_by() or distinct().",
                model=metadata.model_name,
            )
        field_index = {field.name: index for index, field in enumerate(metadata.fields)}
        result_keys = [group["label"] for group in self._aggregate_groups]
        duplicate = set(result_keys) & set(expressions)
        if duplicate:
            raise FerrumCompileError(
                f"Aggregate result keys collide with grouping keys: {sorted(duplicate)!r}.",
                model=metadata.model_name,
            )

        groups: list[dict[str, Any]] = []
        for group in self._aggregate_groups:
            field = group["field"]
            node: dict[str, Any] = {
                "kind": group["kind"],
                "field": {"index": field_index[field], "name": field},
            }
            if group["kind"] == "date_trunc":
                node["granularity"] = group["granularity"]
            groups.append(node)

        aggregates: list[dict[str, Any]] = []
        numeric_types = {"int", "big_int", "float", "decimal"}
        unsupported_ordered_types = {
            "json",
            "bytes",
            "vector",
            "array_text",
            "array_uuid",
            "array_int",
            "array_float",
            "tsvector",
        }
        for alias, expression in expressions.items():
            if not isinstance(expression, Aggregate):
                raise TypeError(f"aggregate expression {alias!r} must be an Aggregate descriptor")
            field_ref: dict[str, Any] | None = None
            if expression.field is not None:
                if expression.field not in field_index:
                    raise FerrumCompileError(
                        f"Unknown field {expression.field!r} on model {metadata.model_name!r}.",
                        model=metadata.model_name,
                        field=expression.field,
                    )
                index = field_index[expression.field]
                field_meta = metadata.fields[index]
                if (
                    expression.function in ("sum", "avg")
                    and field_meta.field_type not in numeric_types
                ):
                    raise FerrumCompileError(
                        f"{expression.function}() requires a numeric field.",
                        model=metadata.model_name,
                        field=expression.field,
                        operator=expression.function,
                    )
                if (
                    expression.function in ("min", "max")
                    and field_meta.field_type in unsupported_ordered_types
                ):
                    raise FerrumCompileError(
                        f"{expression.function}() does not support this field type.",
                        model=metadata.model_name,
                        field=expression.field,
                        operator=expression.function,
                    )
                field_ref = {"index": index, "name": expression.field}
            elif expression.function != "count":
                raise FerrumCompileError(
                    f"{expression.function}() requires a field.",
                    model=metadata.model_name,
                    operator=expression.function,
                )

            aggregate_node: dict[str, Any] = {
                "function": expression.function,
                "field": field_ref,
            }
            if expression.filter is not None:
                predicate_q = (
                    expression.filter
                    if isinstance(expression.filter, Q)
                    else args_to_q(expression.filter)
                )
                if predicate_q is None:
                    raise FerrumCompileError(
                        "Filtered aggregate requires a non-empty predicate.",
                        model=metadata.model_name,
                    )
                predicate, joins = _q_to_predicate(predicate_q, metadata, field_index)
                if joins:
                    raise FerrumCompileError(
                        "Filtered aggregates do not support relation lookups.",
                        model=metadata.model_name,
                    )
                aggregate_node["filter"] = predicate
            aggregates.append(aggregate_node)

        aggregate_indices = {alias: index for index, alias in enumerate(expressions)}
        having: list[dict[str, Any]] = []
        for condition in self._having:
            alias = condition["alias"]
            if alias not in aggregate_indices:
                raise FerrumCompileError(
                    f"Unknown aggregate result key {alias!r} in having().",
                    model=metadata.model_name,
                    field=alias,
                )
            having.append(
                {
                    "aggregate_index": aggregate_indices[alias],
                    "operator": condition["operator"],
                    "value": _encode_bind_value(condition["value"]),
                }
            )

        ir = self._build_ir()
        if ir["joins"]:
            raise FerrumCompileError(
                "aggregate() does not support relation-filter joins.",
                model=metadata.model_name,
            )
        ir["operation"] = {"kind": "select", "fields": []}
        ir["aggregation"] = {"groups": groups, "aggregates": aggregates, "having": having}
        return ir, result_keys + list(expressions)

    async def aggregate(
        self, conn: ConnectionLike, **expressions: Aggregate
    ) -> list[dict[str, Any]]:
        """Execute a scalar or grouped aggregate and return structured dict rows."""
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        ir, result_keys = self._build_aggregate_ir(expressions)
        compiled = self._compile_ir(ir, dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(param) for param in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        metadata = self._get_metadata()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="aggregate",
            table=table,
        )
        started = time.monotonic()
        try:
            rows = await conn._require_driver().fetch(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "aggregate"})
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        output: list[dict[str, Any]] = []
        for row in rows:
            raw = _row_to_dict(row)
            values = [raw[f"group_{index}"] for index in range(len(self._aggregate_groups))]
            values.extend(raw[f"agg_{index}"] for index in range(len(expressions)))
            output.append(dict(zip(result_keys, values, strict=True)))
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=(time.monotonic() - started) * 1000,
            row_count=len(output),
        )
        return output

    # ------------------------------------------------------------------
    # Write IR builders (model-only)
    # ------------------------------------------------------------------

    def _build_insert_ir(self, values: dict[str, Any]) -> dict[str, Any]:
        """Build an INSERT IR dict from the provided field values.

        Validates field names against the model metadata allowlist before
        producing the IR — unknown fields raise ``FerrumCompileError`` (QE-1).

        The IR shape matches ``ferrum_core::ir::Operation::Insert``:
        ``{"kind": "insert", "values": [[field_ref, bind_value], ...]}``.
        """
        metadata: ModelMetadata | None = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata. "
                "Ensure it defines at least one field.",
                model=self._model.__name__,
            )
        field_index: dict[str, int] = {f.name: i for i, f in enumerate(metadata.fields)}
        field_by_name = {f.name: f for f in metadata.fields}
        ir_values: list[Any] = []
        for name, value in values.items():
            self._validate_write_field(metadata, name, api="create()")
            ir_values.append(
                [
                    {"index": field_index[name], "name": name},
                    _encode_field_bind_value(field_by_name[name], value),
                ]
            )
        return {
            "version": _IR_VERSION,
            "model_name": metadata.model_name,
            "operation": {"kind": "insert", "values": ir_values},
            "filters": [],
            "order_by": [],
            "limit": None,
            "offset": None,
        }

    def _build_update_ir(self, assignments: dict[str, Any]) -> dict[str, Any]:
        """Build an UPDATE IR dict from the provided assignments.

        Delegates filter/order-by validation to ``_build_ir()`` (SELECT path),
        then replaces the operation with ``{"kind": "update", "assignments": ...}``.
        Assignment field names are validated against the model metadata allowlist.

        Clears ``limit``/``offset``/``order_by`` — these are not applicable for
        UPDATE statements.
        """
        select_ir = self._build_ir()
        metadata: ModelMetadata | None = self._get_metadata()
        if metadata is None:  # pragma: no cover  (guarded by _build_ir already)
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        field_index: dict[str, int] = {f.name: i for i, f in enumerate(metadata.fields)}
        field_by_name = {f.name: f for f in metadata.fields}
        ir_assignments: list[Any] = []
        for name, value in assignments.items():
            self._validate_write_field(metadata, name, api="update()")
            ir_assignments.append(
                [
                    {"index": field_index[name], "name": name},
                    _encode_field_bind_value(field_by_name[name], value),
                ]
            )
        select_ir["operation"] = {"kind": "update", "assignments": ir_assignments}
        select_ir["order_by"] = []
        select_ir["limit"] = None
        select_ir["offset"] = None
        return select_ir

    def _build_delete_ir(self) -> dict[str, Any]:
        """Build a DELETE IR dict using the current filters.

        Delegates filter validation to ``_build_ir()`` (SELECT path), then
        replaces the operation with ``{"kind": "delete"}``.

        Clears ``limit``/``offset``/``order_by`` — not applicable for DELETE.
        """
        select_ir = self._build_ir()
        select_ir["operation"] = {"kind": "delete"}
        select_ir["order_by"] = []
        select_ir["limit"] = None
        select_ir["offset"] = None
        return select_ir

    # ------------------------------------------------------------------
    # Danger API guards (AGENTS.md §3 / ARCHITECTURE.md §3.9)
    # ------------------------------------------------------------------

    async def create(
        self,
        conn: ConnectionLike,
        _obj: _M | dict[str, Any] | None = None,
        **values: Any,  # noqa: ANN401
    ) -> _M:
        """Insert a single row. Returns the hydrated model instance.

        Accepts either a model instance / dict positionally, or keyword field
        values — not both::

            await User.objects.create(conn, user)               # instance form
            await User.objects.create(conn, {"email": email})   # dict form
            await User.objects.create(conn, email=email)        # kwargs form

        The instance/dict form mirrors ``bulk_create()`` semantics: values come
        from ``model_dump()`` and an auto-generated primary key carrying a
        sentinel value (``0`` / ``None`` / ``""``) is omitted so the database
        default runs. The kwargs form inserts exactly the given values (no
        sentinel dropping). The passed instance is never mutated — the return
        value is a new instance hydrated from ``RETURNING *``.

        Builds an INSERT IR from the values, compiles it through the Rust
        extension, executes ``INSERT … RETURNING *`` via asyncpg ``fetchrow``,
        and constructs the model instance via the ADR-003 trusted hydration path.

        Dispatches Tier A ``query_start`` / ``query_success`` / ``query_failure``
        hook payloads (non-bypassable redaction via ``hooks.dispatch``).

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).
            _obj: A model instance or dict of field values. Named with a leading
                underscore so it can never collide with a model field passed via
                ``**values`` (Pydantic forbids underscore-prefixed field names).
            **values: Field names and their values to insert. Field names are
                validated against the model's allowlist before compilation.

        Raises:
            FerrumConfigError: if the native extension is not built.
            FerrumCompileError: if a field name is not in the model's allowlist,
                if both an instance/dict and keyword values are given, or if the
                instance carries deferred fields.
            FerrumInternalError: if the INSERT returned no row (should not occur
                when the DB is healthy and the table exists).
        """
        if _obj is not None:
            if values:
                raise FerrumCompileError(
                    "create() accepts either a model instance/dict or keyword values, not both.",
                    model=self._model.__name__,
                )
            row_metadata = self._get_metadata()
            if row_metadata is not None:
                row = self._object_to_insert_row_dict(_obj, row_metadata)
                row = self._drop_auto_pk_sentinel(row, row_metadata)
            else:
                row = self._object_to_row_dict(_obj)
            values = row
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "create() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        metadata = self._get_metadata()
        compiled = self._compile_ir(self._build_insert_ir(values), dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="insert",
            table=table,
        )
        t0 = time.monotonic()
        try:
            row = await driver.fetchrow(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "insert"})
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        if row is None:
            raise FerrumInternalError(
                "INSERT returned no row despite RETURNING clause. [FERR-E500]"
            )
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=1,
        )
        return self._model.model_construct(**_coerce_hydrated_row(self._model, _row_to_dict(row)))

    def _pk_field_name(self, metadata: ModelMetadata) -> str:
        """Return the name of the *first* PK field (backward-compat single-PK helper)."""
        for f in metadata.fields:
            if f.pk:
                return f.name
        return metadata.fields[0].name if metadata.fields else "id"

    def _pk_field_names(self, metadata: ModelMetadata) -> list[str]:
        """Return names of *all* PK fields in definition order."""
        pk_names = [f.name for f in metadata.fields if f.pk]
        return pk_names if pk_names else [metadata.fields[0].name] if metadata.fields else ["id"]

    def _object_to_row_dict(self, obj: _M | dict[str, Any]) -> dict[str, Any]:
        """Normalize a write-input object to a mutable field-value dict.

        Rejects instances carrying deferred fields: ``model_dump()`` bypasses
        the deferred-field access guard and would silently write class defaults
        for columns that were never loaded.
        """
        if isinstance(obj, dict):
            return dict(obj)
        if hasattr(obj, "model_dump"):
            deferred = getattr(obj, "__ferrum_deferred__", None)
            if deferred:
                raise FerrumCompileError(
                    f"Cannot write an instance of model {type(obj).__name__!r} with "
                    f"deferred fields {sorted(deferred)!r}; load the full row first.",
                    model=type(obj).__name__,
                )
            return obj.model_dump()
        msg = f"Expected a model instance or dict, got {type(obj)!r}."
        raise TypeError(msg)

    def _object_to_insert_row_dict(
        self,
        obj: _M | dict[str, Any],
        metadata: ModelMetadata,
    ) -> dict[str, Any]:
        """Normalize insert input, omitting generated model-instance fields.

        Explicit dict keys still reach the write-field validator so attempts to
        assign a generated/read-only field fail instead of being silently lost.
        """
        row = self._object_to_row_dict(obj)
        if isinstance(obj, dict):
            return row
        return {name: value for name, value in row.items() if name in metadata.writable_field_names}

    def _validate_write_field(
        self,
        metadata: ModelMetadata,
        name: str,
        *,
        api: str,
    ) -> None:
        """Require a known, writable metadata field before constructing write IR."""
        all_field_names = {field.name for field in metadata.fields}
        if name not in all_field_names:
            raise FerrumCompileError(
                f"Unknown field {name!r} on model {metadata.model_name!r}.",
                model=metadata.model_name,
                field=name,
            )
        if name not in metadata.writable_field_names:
            raise FerrumCompileError(
                f"{api} cannot assign generated/read-only field {name!r} "
                f"on model {metadata.model_name!r}.",
                model=metadata.model_name,
                field=name,
            )

    def _drop_auto_pk_sentinel(
        self, values: dict[str, Any], metadata: ModelMetadata
    ) -> dict[str, Any]:
        """Drop the auto-generated PK column when it carries a sentinel value.

        A sentinel value (``0`` / ``None`` / ``""``) on the first PK field means
        "let the database default generate this". Raises a structured error when
        nothing is left to insert, so an empty INSERT never reaches SQL emission.
        """
        out = dict(values)
        pk_name = self._pk_field_name(metadata)
        if pk_name in out and out[pk_name] in (0, None, ""):
            out.pop(pk_name, None)
        if not out:
            raise FerrumCompileError(
                f"Insert on model {metadata.model_name!r} requires at least one field "
                "value after dropping auto-generated primary-key sentinel values.",
                model=metadata.model_name,
            )
        return out

    def _build_bulk_insert_ir(
        self,
        rows: list[dict[str, Any]],
        *,
        returning: bool,
    ) -> dict[str, Any]:
        """Build BulkInsert IR after validating row shape and field names.

        All rows in a single compiled statement must share the same field set so
        the Rust compiler can emit one column list and one repeated VALUES shape.
        Auto-generated primary keys with empty sentinel values are omitted to let
        database defaults run.
        """
        metadata: ModelMetadata | None = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        if not rows:
            raise FerrumCompileError(
                f"bulk_create() requires at least one row on model {metadata.model_name!r}.",
                model=metadata.model_name,
            )
        field_index: dict[str, int] = {f.name: i for i, f in enumerate(metadata.fields)}
        field_by_name = {f.name: f for f in metadata.fields}
        ir_rows: list[list[Any]] = []
        column_order: list[str] | None = None
        for row in rows:
            values = self._drop_auto_pk_sentinel(row, metadata)
            if column_order is None:
                column_order = sorted(values.keys())
            elif sorted(values.keys()) != column_order:
                raise FerrumCompileError(
                    "bulk_create() rows must share the same field set.",
                    model=metadata.model_name,
                )
            ir_row: list[Any] = []
            for name in column_order:
                self._validate_write_field(metadata, name, api="bulk_create()")
                ir_row.append(
                    [
                        {"index": field_index[name], "name": name},
                        _encode_field_bind_value(field_by_name[name], values[name]),
                    ]
                )
            ir_rows.append(ir_row)
        return {
            "version": _IR_VERSION,
            "model_name": metadata.model_name,
            "operation": {"kind": "bulk_insert", "rows": ir_rows, "returning": returning},
            "filters": [],
            "order_by": [],
            "limit": None,
            "offset": None,
        }

    def _build_bulk_update_ir(
        self,
        rows: list[tuple[Any, dict[str, Any]]],
        fields: Sequence[str],
    ) -> dict[str, Any]:
        """Build a BulkUpdate IR.

        ``rows`` is a list of ``(pk_values, assignments)`` where ``pk_values`` is
        either a scalar (single-PK) or a list/tuple of values (composite PK).
        """
        metadata: ModelMetadata | None = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        if not rows:
            raise FerrumCompileError(
                f"bulk_update() requires at least one row on model {metadata.model_name!r}.",
                model=metadata.model_name,
            )
        field_index: dict[str, int] = {f.name: i for i, f in enumerate(metadata.fields)}
        field_by_name = {f.name: f for f in metadata.fields}
        pk_names = self._pk_field_names(metadata)
        for pk_name in pk_names:
            if pk_name not in field_index:
                raise FerrumCompileError(
                    f"Model {metadata.model_name!r} has no primary key field {pk_name!r}.",
                    model=metadata.model_name,
                )
        field_list = list(fields)
        if not field_list:
            raise FerrumCompileError(
                "bulk_update() requires at least one field.",
                model=metadata.model_name,
            )
        for name in field_list:
            self._validate_write_field(metadata, name, api="bulk_update()")
        ir_pk_fields = [{"index": field_index[pk_name], "name": pk_name} for pk_name in pk_names]
        ir_fields = [{"index": field_index[name], "name": name} for name in field_list]
        ir_rows: list[dict[str, Any]] = []
        for pk_val, assignments in rows:
            # Normalize pk_val: scalar for single-PK, sequence for composite PK.
            if len(pk_names) == 1:
                pk_values_encoded = [_encode_bind_value(pk_val)]
            else:
                if isinstance(pk_val, (list, tuple)) and len(pk_val) == len(pk_names):
                    pk_values_encoded = [_encode_bind_value(v) for v in pk_val]
                else:
                    raise FerrumCompileError(
                        f"bulk_update() composite PK requires {len(pk_names)} values, "
                        f"got {pk_val!r}.",
                        model=metadata.model_name,
                    )
            ir_rows.append(
                {
                    "pk_values": pk_values_encoded,
                    "values": [
                        _encode_field_bind_value(field_by_name[name], assignments[name])
                        for name in field_list
                    ],
                }
            )
        return {
            "version": _IR_VERSION,
            "model_name": metadata.model_name,
            "operation": {
                "kind": "bulk_update",
                "pk_fields": ir_pk_fields,
                "fields": ir_fields,
                "rows": ir_rows,
            },
            "filters": [],
            "order_by": [],
            "limit": None,
            "offset": None,
        }

    def _build_bulk_delete_ir(self, ids: Sequence[Any]) -> dict[str, Any]:
        """Build a BulkDelete IR.

        For composite PKs, each element of ``ids`` must be a sequence of values
        matching the model's ``pk_fields`` order.  For single-PK models a plain
        scalar is accepted (backward compat).
        """
        metadata: ModelMetadata | None = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        if not ids:
            raise FerrumCompileError(
                f"bulk_delete() requires at least one id on model {metadata.model_name!r}.",
                model=metadata.model_name,
            )
        field_index: dict[str, int] = {f.name: i for i, f in enumerate(metadata.fields)}
        pk_names = self._pk_field_names(metadata)
        ir_pk_fields = [{"index": field_index[pk_name], "name": pk_name} for pk_name in pk_names]

        # Encode each id as a list of BindValues (length == len(pk_names)).
        encoded_ids: list[list[Any]] = []
        for pk_val in ids:
            if len(pk_names) == 1:
                encoded_ids.append([_encode_bind_value(pk_val)])
            else:
                if isinstance(pk_val, (list, tuple)) and len(pk_val) == len(pk_names):
                    encoded_ids.append([_encode_bind_value(v) for v in pk_val])
                else:
                    raise FerrumCompileError(
                        f"bulk_delete() composite PK requires {len(pk_names)} values per id, "
                        f"got {pk_val!r}.",
                        model=metadata.model_name,
                    )

        return {
            "version": _IR_VERSION,
            "model_name": metadata.model_name,
            "operation": {
                "kind": "bulk_delete",
                "pk_fields": ir_pk_fields,
                "ids": encoded_ids,
            },
            "filters": [],
            "order_by": [],
            "limit": None,
            "offset": None,
        }

    async def bulk_create(
        self,
        conn: ConnectionLike,
        objects: Sequence[_M | dict[str, Any]],
        *,
        batch_size: int = 1000,
        returning: bool = True,
    ) -> list[_M] | int:
        """Insert many rows in batched multi-value INSERT statements.

        Args:
            conn: Open ``Connection`` or active ``Transaction``.
            objects: Model instances or field dicts to insert.
            batch_size: Maximum rows per compiled INSERT statement.
            returning: When ``True`` (default), return hydrated instances via
                ``INSERT … RETURNING``. When ``False``, return the total inserted
                row count.

        Raises:
            FerrumConfigError: if the native extension is not built.
            FerrumCompileError: for unknown fields or inconsistent row shapes.

        Notes:
            Batching reduces round-trips but each batch is still compiled through
            the same IR path as single-row inserts, preserving identifier
            allowlisting and bound-parameter handling.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "bulk_create() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        if batch_size < 1:
            raise FerrumConfigError("batch_size must be at least 1. [FERR-C001]")
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        row_dicts = [self._object_to_insert_row_dict(obj, metadata) for obj in objects]
        if not row_dicts:
            return [] if returning else 0

        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        created: list[_M] = []
        total = 0

        for start in range(0, len(row_dicts), batch_size):
            batch = row_dicts[start : start + batch_size]
            compiled = self._compile_ir(
                self._build_bulk_insert_ir(batch, returning=returning),
                dialect=conn.dialect,
            )
            sql_text: str = compiled["sql_text"]
            bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
            fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
            _hooks.query_start(
                fingerprint=fingerprint,
                model=model_name,
                operation="bulk_insert",
                table=table,
            )
            t0 = time.monotonic()
            try:
                if returning:
                    rows = await driver.fetch(sql_text, *bound_params)
                else:
                    result: str = await driver.execute(sql_text, *bound_params)
                    parts = result.split() if result else []
                    try:
                        total += int(parts[2]) if len(parts) > 2 else len(batch)
                    except ValueError:
                        total += len(batch)
                    rows = []
            except Exception as exc:
                duration_ms = (time.monotonic() - t0) * 1000
                mapped = map_db_error(
                    exc, context={"model": model_name, "operation": "bulk_insert"}
                )
                _hooks.query_failure(
                    fingerprint=fingerprint,
                    duration_ms=duration_ms,
                    failure_category=type(mapped).__name__,
                )
                raise mapped from None
            duration_ms = (time.monotonic() - t0) * 1000
            if returning:
                batch_instances = [
                    self._model.model_construct(
                        **_coerce_hydrated_row(self._model, _row_to_dict(row))
                    )
                    for row in rows
                ]
                created.extend(batch_instances)
                _hooks.query_success(
                    fingerprint=fingerprint,
                    duration_ms=duration_ms,
                    row_count=len(batch_instances),
                )
            else:
                _hooks.query_success(
                    fingerprint=fingerprint,
                    duration_ms=duration_ms,
                    row_count=len(batch),
                )

        return created if returning else total

    async def bulk_update(
        self,
        conn: ConnectionLike,
        objects: Sequence[_M],
        fields: Sequence[str],
        *,
        batch_size: int = 1000,
    ) -> int:
        """Update many rows by primary key in batched statements.

        Each object must carry a populated primary-key value. Only ``fields`` are
        written; other columns are left unchanged.

        Returns the total affected row count (sum of per-batch driver counts).

        Composite primary keys are encoded in ``ModelMetadata.pk_fields`` order.
        Empty input is a no-op, while an empty ``fields`` sequence is rejected by
        the IR builder because it cannot express a meaningful UPDATE.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "bulk_update() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        if batch_size < 1:
            raise FerrumConfigError("batch_size must be at least 1. [FERR-C001]")
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        pk_names = self._pk_field_names(metadata)
        field_list = list(fields)
        for name in field_list:
            self._validate_write_field(metadata, name, api="bulk_update()")
        rows: list[tuple[Any, dict[str, Any]]] = []
        for obj in objects:
            data = self._object_to_row_dict(obj)
            for pk_name in pk_names:
                if pk_name not in data:
                    raise FerrumCompileError(
                        f"bulk_update() object missing primary key field {pk_name!r}.",
                        model=metadata.model_name,
                        field=pk_name,
                    )
            # For single-PK: pass scalar; for composite PK: pass tuple.
            if len(pk_names) == 1:
                pk_val: Any = data[pk_names[0]]
            else:
                pk_val = tuple(data[pk_name] for pk_name in pk_names)
            assignments = {name: data[name] for name in field_list}
            rows.append((pk_val, assignments))
        if not rows:
            return 0

        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name
        total_updated = 0

        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            compiled = self._compile_ir(
                self._build_bulk_update_ir(batch, field_list),
                dialect=conn.dialect,
            )
            sql_text: str = compiled["sql_text"]
            bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
            fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
            _hooks.query_start(
                fingerprint=fingerprint,
                model=model_name,
                operation="bulk_update",
                table=table,
            )
            t0 = time.monotonic()
            try:
                result: str = await driver.execute(sql_text, *bound_params)
            except Exception as exc:
                duration_ms = (time.monotonic() - t0) * 1000
                mapped = map_db_error(
                    exc, context={"model": model_name, "operation": "bulk_update"}
                )
                _hooks.query_failure(
                    fingerprint=fingerprint,
                    duration_ms=duration_ms,
                    failure_category=type(mapped).__name__,
                )
                raise mapped from None
            duration_ms = (time.monotonic() - t0) * 1000
            parts = result.split() if result else []
            try:
                total_updated += int(parts[1]) if len(parts) > 1 else len(batch)
            except ValueError:
                total_updated += len(batch)
            _hooks.query_success(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                row_count=len(batch),
            )
        return total_updated

    async def bulk_delete(
        self,
        conn: ConnectionLike,
        ids: Sequence[Any],
        *,
        batch_size: int = 1000,
    ) -> int:
        """Delete rows by primary-key values in batched ``DELETE … IN (…)`` statements.

        Returns the total deleted row count.

        For composite primary keys, each element in ``ids`` must be a sequence in
        ``ModelMetadata.pk_fields`` order. Empty input is a no-op, not an
        unscoped table delete.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "bulk_delete() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        if batch_size < 1:
            raise FerrumConfigError("batch_size must be at least 1. [FERR-C001]")
        if not ids:
            return 0

        metadata = self._get_metadata()
        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        id_list = list(ids)
        total_deleted = 0

        for start in range(0, len(id_list), batch_size):
            batch = id_list[start : start + batch_size]
            compiled = self._compile_ir(
                self._build_bulk_delete_ir(batch),
                dialect=conn.dialect,
            )
            sql_text: str = compiled["sql_text"]
            bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
            fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
            _hooks.query_start(
                fingerprint=fingerprint,
                model=model_name,
                operation="bulk_delete",
                table=table,
            )
            t0 = time.monotonic()
            try:
                result: str = await driver.execute(sql_text, *bound_params)
            except Exception as exc:
                duration_ms = (time.monotonic() - t0) * 1000
                mapped = map_db_error(
                    exc, context={"model": model_name, "operation": "bulk_delete"}
                )
                _hooks.query_failure(
                    fingerprint=fingerprint,
                    duration_ms=duration_ms,
                    failure_category=type(mapped).__name__,
                )
                raise mapped from None
            duration_ms = (time.monotonic() - t0) * 1000
            parts = result.split() if result else []
            try:
                total_deleted += int(parts[1]) if len(parts) > 1 else len(batch)
            except ValueError:
                total_deleted += len(batch)
            _hooks.query_success(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                row_count=len(batch),
            )
        return total_deleted

    # ------------------------------------------------------------------
    # Upsert API (PostgreSQL ON CONFLICT … DO UPDATE / DO NOTHING)
    # ------------------------------------------------------------------

    def _build_upsert_sql(
        self,
        metadata: ModelMetadata,
        values: dict[str, Any],
        *,
        conflict_fields: list[str],
        update_fields: list[str] | None,
        returning: bool,
        dialect: str = "postgres",
    ) -> tuple[str, list[Any]]:
        """Build an upsert SQL string and bound-parameter list for a single row.

        Security invariants:
        - All SQL identifiers (table, column names) are double-quoted and sourced
          exclusively from ``ModelMetadata`` — never from raw user input.
        - All values travel as ``$N`` positional parameters — never interpolated.
        - ``conflict_fields`` and ``update_fields`` are validated against the
          metadata allowlist before this method is called.

        Upsert is PostgreSQL ``ON CONFLICT`` only. The thin-parity backends
        (MySQL, SQLite, MSSQL) raise rather than emit a non-portable statement.
        """
        if dialect in ("mysql", "sqlite", "mssql"):
            raise FerrumConfigError(
                f"upsert()/bulk_upsert() is not supported on the {dialect!r} backend. "
                "Ferrum upsert requires PostgreSQL ON CONFLICT syntax. "
                "Use separate insert/update calls on non-PostgreSQL backends. [FERR-C001]"
            )
        field_by_name = {f.name: f for f in metadata.fields}
        for field_name in values:
            self._validate_write_field(metadata, field_name, api="upsert()")
        if update_fields is not None:
            for field_name in update_fields:
                self._validate_write_field(metadata, field_name, api="upsert()")
        table = f'"{metadata.table_name}"'

        col_names: list[str] = []
        placeholders: list[str] = []
        bound: list[Any] = []
        for i, (fname, fval) in enumerate(values.items(), start=1):
            col = f'"{field_by_name[fname].column_name}"'
            col_names.append(col)
            placeholders.append(f"${i}")
            bound.append(_prepare_field_value(field_by_name[fname], fval))

        conflict_cols = ", ".join(f'"{field_by_name[cf].column_name}"' for cf in conflict_fields)

        insert_part = (
            f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({', '.join(placeholders)})"
        )

        if update_fields is None:
            # Default: all non-PK, non-conflict fields.
            conflict_set = set(conflict_fields)
            update_fields = [
                f.name
                for f in metadata.writable_fields
                if not f.pk and f.name not in conflict_set and f.name in values
            ]

        if not update_fields:
            conflict_clause = f"ON CONFLICT ({conflict_cols}) DO NOTHING"
        else:
            set_parts = [
                f'"{field_by_name[uf].column_name}" = EXCLUDED."{field_by_name[uf].column_name}"'
                for uf in update_fields
            ]
            conflict_clause = f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {', '.join(set_parts)}"

        sql = f"{insert_part} {conflict_clause}"
        if returning:
            ret_cols = ", ".join(f'"{f.column_name}"' for f in metadata.fields)
            sql += f" RETURNING {ret_cols}"
        return sql, bound

    async def upsert(
        self,
        conn: ConnectionLike,
        *,
        conflict_fields: list[str],
        update_fields: list[str] | None = None,
        returning: bool = True,
        **values: Any,  # noqa: ANN401
    ) -> _M | None:
        """Insert a row or update it on conflict (``INSERT … ON CONFLICT … DO UPDATE``).

        Args:
            conn: An open ``Connection`` or active ``Transaction``.
            conflict_fields: Field names that form the conflict target. Must be in
                the model's metadata allowlist (validated before SQL emission).
            update_fields: Fields to update on conflict. Defaults to all non-PK,
                non-conflict fields present in ``values``. Pass ``[]`` for
                ``DO NOTHING`` semantics.
            returning: When ``True`` (default), return the upserted model instance.
                When ``False``, return ``None``.
            **values: Field names and values to insert. All names are validated
                against the model's metadata allowlist.

        Returns:
            The upserted model instance when ``returning=True``, else ``None``.

        Raises:
            FerrumCompileError: for unknown field names or invalid conflict targets.
            FerrumConfigError: if not connected.
        """
        if conn is None:
            raise FerrumConfigError(
                "upsert() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        field_names = {f.name for f in metadata.fields}
        # Validate all field names in values against the allowlist.
        for fname in values:
            self._validate_write_field(metadata, fname, api="upsert()")
        # Validate conflict_fields against the allowlist.
        for cf in conflict_fields:
            if cf not in field_names:
                raise FerrumCompileError(
                    f"Unknown conflict field {cf!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=cf,
                )
        # Validate update_fields if explicitly provided.
        if update_fields is not None:
            for uf in update_fields:
                self._validate_write_field(metadata, uf, api="upsert()")

        sql, bound = self._build_upsert_sql(
            metadata,
            values,
            conflict_fields=conflict_fields,
            update_fields=update_fields,
            returning=returning,
            dialect=conn.dialect,
        )

        driver = conn._require_driver()
        model_name = self._model.__name__
        _hooks.query_start(
            fingerprint="",
            model=model_name,
            operation="upsert",
            table=metadata.table_name,
        )
        t0 = time.monotonic()
        try:
            if returning:
                row = await driver.fetchrow(sql, *bound)
            else:
                await driver.execute(sql, *bound)
                row = None
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "upsert"})
            _hooks.query_failure(
                fingerprint="",
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        _hooks.query_success(fingerprint="", duration_ms=duration_ms, row_count=1 if row else 0)
        if not returning or row is None:
            return None
        return self._model.model_construct(**_coerce_hydrated_row(self._model, _row_to_dict(row)))

    async def bulk_upsert(
        self,
        conn: ConnectionLike,
        objects: Sequence[_M | dict[str, Any]],
        *,
        conflict_fields: list[str],
        update_fields: list[str] | None = None,
        batch_size: int = 1000,
        returning: bool = False,
    ) -> list[_M] | int:
        """Upsert many rows in batched ``INSERT … ON CONFLICT`` statements.

        Args:
            conn: An open ``Connection`` or active ``Transaction``.
            objects: Model instances or field dicts to upsert.
            conflict_fields: Field names forming the conflict target (allowlist-validated).
            update_fields: Fields to update on conflict. Defaults to all non-PK,
                non-conflict fields.  Pass ``[]`` for ``DO NOTHING``.
            batch_size: Maximum rows per statement (default 1000).
            returning: When ``True``, return hydrated instances. When ``False``
                (default), return total upserted row count.

        Returns:
            List of model instances when ``returning=True``, else int row count.

        Raises:
            FerrumCompileError: for unknown field or conflict target names.
            FerrumConfigError: if not connected.
        """
        if conn is None:
            raise FerrumConfigError(
                "bulk_upsert() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        if batch_size < 1:
            raise FerrumConfigError("batch_size must be at least 1. [FERR-C001]")
        if not objects:
            return [] if returning else 0

        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {self._model.__name__!r} has no metadata.",
                model=self._model.__name__,
            )
        field_names = {f.name for f in metadata.fields}
        for cf in conflict_fields:
            if cf not in field_names:
                raise FerrumCompileError(
                    f"Unknown conflict field {cf!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=cf,
                )
        if update_fields is not None:
            for uf in update_fields:
                self._validate_write_field(metadata, uf, api="bulk_upsert()")

        row_dicts = [self._object_to_insert_row_dict(obj, metadata) for obj in objects]
        driver = conn._require_driver()
        model_name = self._model.__name__
        upserted: list[_M] = []
        total = 0

        for start in range(0, len(row_dicts), batch_size):
            batch = row_dicts[start : start + batch_size]
            for values in batch:
                sql, bound = self._build_upsert_sql(
                    metadata,
                    values,
                    conflict_fields=conflict_fields,
                    update_fields=update_fields,
                    returning=returning,
                    dialect=conn.dialect,
                )
                _hooks.query_start(
                    fingerprint="",
                    model=model_name,
                    operation="upsert",
                    table=metadata.table_name,
                )
                t0 = time.monotonic()
                try:
                    if returning:
                        row = await driver.fetchrow(sql, *bound)
                        if row is not None:
                            upserted.append(
                                self._model.model_construct(
                                    **_coerce_hydrated_row(self._model, _row_to_dict(row))
                                )
                            )
                    else:
                        await driver.execute(sql, *bound)
                        total += 1
                except Exception as exc:
                    duration_ms = (time.monotonic() - t0) * 1000
                    mapped = map_db_error(
                        exc, context={"model": model_name, "operation": "bulk_upsert"}
                    )
                    _hooks.query_failure(
                        fingerprint="",
                        duration_ms=duration_ms,
                        failure_category=type(mapped).__name__,
                    )
                    raise mapped from None
                duration_ms = (time.monotonic() - t0) * 1000
                _hooks.query_success(fingerprint="", duration_ms=duration_ms, row_count=1)

        return upserted if returning else total

    def _check_write_scope(self, api: str) -> None:
        """Reject queryset state that UPDATE/DELETE cannot honor.

        ``limit``/``offset`` and join/ranking state are silently absent from the
        write IR; letting them through would change which rows are affected
        (e.g. ``filter(...)[:10].delete()`` deleting every matching row). Fail
        loudly before compilation instead.
        """
        if self._limit is not None or self._offset is not None:
            raise FerrumCompileError(
                f"{api} cannot be used on a sliced QuerySet; "
                "LIMIT/OFFSET do not apply to UPDATE/DELETE.",
                model=self._model.__name__,
            )
        if self._select_related:
            raise FerrumCompileError(
                f"{api} cannot be used with select_related(); joins do not apply to UPDATE/DELETE.",
                model=self._model.__name__,
            )
        metadata = self._get_metadata()
        if self._predicate_q is not None and metadata is not None:
            field_index = {f.name: i for i, f in enumerate(metadata.fields)}
            _, filter_joins = _q_to_predicate(self._predicate_q, metadata, field_index)
            if filter_joins:
                raise FerrumCompileError(
                    f"{api} cannot be used with relation lookups "
                    f"({', '.join(sorted(filter_joins))}); "
                    "JOINs do not apply to UPDATE/DELETE.",
                    model=self._model.__name__,
                )
        if self._vector_order_by is not None or self._text_rank_by is not None:
            raise FerrumCompileError(
                f"{api} cannot be used with nearest_to()/rank_by()/search(); "
                "ranking does not apply to UPDATE/DELETE.",
                model=self._model.__name__,
            )
        if self._aggregate_groups or self._having:
            raise FerrumCompileError(
                f"{api} cannot be used with group_by()/date_trunc()/having().",
                model=self._model.__name__,
            )

    async def delete(self, conn: ConnectionLike | None = None) -> int:
        """Delete filtered rows. Returns the row count.

        Requires at least one filter. Use ``danger_delete_all()`` for an
        unscoped delete.

        The filter guard fires before any connection or compilation work so that
        ``delete()`` raises ``FerrumDangerApiError`` even when ``conn`` is
        omitted — keeping the error ergonomics predictable.

        Dispatches Tier A ``query_start`` / ``query_success`` / ``query_failure``
        hook payloads (non-bypassable redaction via ``hooks.dispatch``).

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).

        Raises:
            FerrumDangerApiError: if called without any filter.
            FerrumConfigError: if the native extension is not built.
        """
        if not self._is_filtered:
            raise FerrumDangerApiError(
                "Refusing unscoped delete(). Use QuerySet.danger_delete_all() "
                "to explicitly delete all rows in the table."
            )
        self._check_write_scope("delete()")
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "delete() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        metadata = self._get_metadata()
        compiled = self._compile_ir(self._build_delete_ir(), dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="delete",
            table=table,
        )
        t0 = time.monotonic()
        try:
            result: str = await driver.execute(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "delete"})
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        # asyncpg execute() returns a status string like "DELETE 3".
        parts = result.split() if result else []
        try:
            row_count = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            row_count = 0
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=row_count,
        )
        return row_count

    async def danger_delete_all(self, conn: ConnectionLike) -> int:
        """Delete ALL rows in the table without a filter.

        This is an explicit escape hatch. Prefer ``filter(...).delete()`` for
        scoped deletes. This method name is intentionally verbose to prevent
        accidental use.

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).

        Raises:
            FerrumConfigError: if the native extension is not built.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "danger_delete_all() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        qs_all: QuerySet[_M] = QuerySet(self._model)
        delete_ir = qs_all._build_delete_ir()
        delete_ir["operation"]["danger"] = True  # bypass Rust MissingFilter for danger API
        compiled = qs_all._compile_ir(delete_ir, dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        model_name = self._model.__name__
        metadata_all = qs_all._get_metadata()
        table = metadata_all.table_name if metadata_all is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint, model=model_name, operation="delete", table=table
        )
        t0 = time.monotonic()
        try:
            result: str = await driver.execute(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc)
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        parts = result.split() if result else []
        try:
            row_count = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            row_count = 0
        _hooks.query_success(fingerprint=fingerprint, duration_ms=duration_ms, row_count=row_count)
        return row_count

    async def update(self, conn: ConnectionLike | None = None, **assignments: Any) -> int:  # noqa: ANN401
        """Update filtered rows. Returns the row count.

        Requires at least one filter. Use ``danger_update_all()`` for an
        unscoped update.

        The filter guard fires before any connection or compilation work so that
        ``update()`` raises ``FerrumDangerApiError`` even when ``conn`` is
        omitted — keeping the error ergonomics predictable.

        Dispatches Tier A ``query_start`` / ``query_success`` / ``query_failure``
        hook payloads (non-bypassable redaction via ``hooks.dispatch``).

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).
            **assignments: Field-name = new-value pairs to set. Field names are
                validated against the model's allowlist.

        Raises:
            FerrumDangerApiError: if called without any filter.
            FerrumConfigError: if the native extension is not built.
            FerrumCompileError: if an assignment field is not in the model's allowlist.
        """
        if not self._is_filtered:
            raise FerrumDangerApiError(
                "Refusing unscoped update(). Use QuerySet.danger_update_all() "
                "to explicitly update all rows in the table."
            )
        self._check_write_scope("update()")
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "update() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        metadata = self._get_metadata()
        compiled = self._compile_ir(self._build_update_ir(assignments), dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="update",
            table=table,
        )
        t0 = time.monotonic()
        try:
            result: str = await driver.execute(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc, context={"model": model_name, "operation": "update"})
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        # asyncpg execute() returns a status string like "UPDATE 3".
        parts = result.split() if result else []
        try:
            row_count = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            row_count = 0
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=row_count,
        )
        return row_count

    async def update_returning(
        self,
        conn: ConnectionLike | None = None,
        **assignments: Any,  # noqa: ANN401
    ) -> list[dict[str, Any]]:
        """Atomically update filtered rows and return their post-update values.

        This is the compare-and-set primitive: encode the expected state in
        ``filter()`` and inspect the returned list. An empty list means another
        writer won or no row matched. The existing unscoped-update and write
        scope gates are preserved.
        """
        if not self._is_filtered:
            raise FerrumDangerApiError(
                "Refusing unscoped update_returning(). Add a filter; "
                "there is no unscoped returning danger API."
            )
        self._check_write_scope("update_returning()")
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "update_returning() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        metadata = self._get_metadata()
        compiled = self._compile_ir(self._build_update_ir(assignments), dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(param) for param in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        model_name = self._model.__name__
        table = metadata.table_name if metadata is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint,
            model=model_name,
            operation="update_returning",
            table=table,
        )
        started = time.monotonic()
        try:
            rows = await conn._require_driver().fetch(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000
            mapped = map_db_error(
                exc, context={"model": model_name, "operation": "update_returning"}
            )
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        output = [_row_to_dict(row) for row in rows]
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=(time.monotonic() - started) * 1000,
            row_count=len(output),
        )
        return output

    async def update_instance(
        self,
        conn: ConnectionLike,
        obj: _M,
        *,
        fields: Sequence[str] | None = None,
    ) -> int:
        """Persist one instance's field values to its row, targeted by primary key.

        The singular counterpart of ``bulk_update()``::

            user = await User.objects.filter(email=email).first(conn)
            user.active = False
            count = await User.objects.update_instance(conn, user, fields=["active"])
            if count == 0:
                ...  # row was deleted concurrently (stale instance)

        Values come from ``model_dump()`` (same as the bulk paths), the WHERE
        clause is the instance's primary key (composite PKs supported), and the
        call delegates to ``filter(pk=...).update(conn, ...)`` — inheriting the
        allowlist validation, bound parameters, Tier A hooks, and error mapping.

        Prefer an explicit ``fields=[...]`` subset: ``fields=None`` writes every
        non-PK column and is last-writer-wins against concurrent updates.

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).
            obj: The model instance to persist. Never mutated.
            fields: Field names to update. ``None`` updates all non-PK fields.

        Returns:
            The number of rows updated: ``1``, or ``0`` when no row matches the
            primary key (missing or concurrently deleted — caller decides).

        Raises:
            FerrumCompileError: if called on a filtered QuerySet, if a primary
                key value is missing or a sentinel (``0``/``None``/``""``), if
                ``fields`` is empty, unknown, contains a PK field, or overlaps
                the instance's deferred fields.
        """
        model_name = self._model.__name__
        if self._is_filtered:
            raise FerrumCompileError(
                "update_instance() must be called on an unfiltered QuerySet — it "
                "targets the row by primary key. Use filter(...).update(conn, ...) "
                "for filtered updates.",
                model=model_name,
            )
        metadata = self._get_metadata()
        if metadata is None:
            raise FerrumCompileError(
                f"Model {model_name!r} has no metadata.",
                model=model_name,
            )
        field_index = {f.name: i for i, f in enumerate(metadata.fields)}
        pk_names = self._pk_field_names(metadata)
        deferred = frozenset(getattr(obj, "__ferrum_deferred__", None) or ())
        deferred_pks = sorted(set(pk_names) & deferred)
        if deferred_pks:
            raise FerrumCompileError(
                f"update_instance() requires loaded primary-key fields; "
                f"{deferred_pks!r} are deferred on model {metadata.model_name!r}.",
                model=metadata.model_name,
            )
        if fields is None:
            if deferred:
                raise FerrumCompileError(
                    "update_instance() without fields=[...] cannot be used on an "
                    f"instance with deferred fields {sorted(deferred)!r}; pass an "
                    "explicit fields subset.",
                    model=metadata.model_name,
                )
            field_list = [f.name for f in metadata.writable_fields if f.name not in pk_names]
        else:
            field_list = list(dict.fromkeys(fields))
            if not field_list:
                raise FerrumCompileError(
                    "update_instance() requires at least one field.",
                    model=metadata.model_name,
                )
            for name in field_list:
                if name not in field_index:
                    raise FerrumCompileError(
                        f"Unknown field {name!r} on model {metadata.model_name!r}.",
                        model=metadata.model_name,
                        field=name,
                    )
                if name in pk_names:
                    raise FerrumCompileError(
                        f"update_instance() cannot assign primary-key field {name!r}; "
                        "primary keys identify the target row.",
                        model=metadata.model_name,
                        field=name,
                    )
                self._validate_write_field(metadata, name, api="update_instance()")
                if name in deferred:
                    raise FerrumCompileError(
                        f"update_instance() cannot write deferred field {name!r}; "
                        "it was never loaded on this instance.",
                        model=metadata.model_name,
                        field=name,
                    )
        row = obj.model_dump(include=set(field_list) | set(pk_names))
        pk_map: dict[str, Any] = {}
        for pk_name in pk_names:
            pk_value = row.get(pk_name)
            if pk_value in (0, None, ""):
                raise FerrumCompileError(
                    f"update_instance() requires a primary-key value for field "
                    f"{pk_name!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=pk_name,
                )
            pk_map[pk_name] = pk_value
        assignments = {name: row[name] for name in field_list}
        target: QuerySet[_M] = QuerySet(self._model).filter(**pk_map)
        return await target.update(conn, **assignments)

    async def danger_update_all(self, conn: ConnectionLike, **assignments: Any) -> int:  # noqa: ANN401
        """Update ALL rows in the table without a filter.

        This is an explicit escape hatch. Prefer ``filter(...).update()`` for
        scoped updates. This method name is intentionally verbose.

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).
            **assignments: Field-name = new-value pairs to set.

        Raises:
            FerrumConfigError: if the native extension is not built.
            FerrumCompileError: if an assignment field is not in the model's allowlist.
        """
        if _native_ext is None:
            raise FerrumConfigError(_EXT_NOT_BUILT_MSG)
        if conn is None:
            raise FerrumConfigError(
                "danger_update_all() requires an active Connection. "
                "Obtain one from ferrum.connect(). [FERR-C001]"
            )
        qs_all: QuerySet[_M] = QuerySet(self._model)
        update_ir = qs_all._build_update_ir(assignments)
        update_ir["operation"]["danger"] = True  # bypass Rust MissingFilter for danger API
        compiled = qs_all._compile_ir(update_ir, dialect=conn.dialect)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        driver = conn._require_driver()
        model_name = self._model.__name__
        metadata_all = qs_all._get_metadata()
        table = metadata_all.table_name if metadata_all is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint, model=model_name, operation="update", table=table
        )
        t0 = time.monotonic()
        try:
            result: str = await driver.execute(sql_text, *bound_params)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            mapped = map_db_error(exc)
            _hooks.query_failure(
                fingerprint=fingerprint,
                duration_ms=duration_ms,
                failure_category=type(mapped).__name__,
            )
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        parts = result.split() if result else []
        try:
            row_count = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            row_count = 0
        _hooks.query_success(fingerprint=fingerprint, duration_ms=duration_ms, row_count=row_count)
        return row_count

    async def _materialize(
        self,
        rows: list[Any],
        metadata: ModelMetadata | None,
        conn: ConnectionLike,
        fingerprint: str,
    ) -> list[_M]:
        """Hydrate rows into model instances (ADR-003 trusted path).

        Applies ``select_related`` JOIN splitting and ``prefetch_related``
        batched loading — the model-only tail of the previous ``all()``.
        """
        hydrate_model = self._hydrate_model if self._hydrate_model is not None else self._model
        deferred = self._deferred_field_names(metadata) if metadata is not None else None
        # When projecting onto a narrower model, only() already excludes source-only
        # columns — the deferred set is for the source SELECT shape and must not
        # mark target fields as deferred.
        if self._hydrate_model is not None:
            deferred = None
        instances = _hydrate_rows(
            hydrate_model,
            rows,
            fingerprint=fingerprint,
            deferred=deferred,
        )
        if self._select_related and metadata is not None:
            from ferrum.relations import build_join_ir, set_relation, split_joined_row

            field_index = {f.name: i for i, f in enumerate(metadata.fields)}
            joins = [build_join_ir(metadata, n, field_index) for n in self._select_related]
            for inst, row in zip(instances, rows, strict=True):
                row_dict = _row_to_dict(row)
                related = split_joined_row(row_dict, joins)
                for rel_name, rel_row in related.items():
                    if not rel_row or all(v is None for v in rel_row.values()):
                        set_relation(inst, rel_name, None)
                        continue
                    rel_meta = next(r for r in metadata.relations if r.field_name == rel_name)
                    from ferrum.registry import get_model

                    rel_model = get_model(rel_meta.to_model)
                    set_relation(
                        inst,
                        rel_name,
                        rel_model.model_construct(**_coerce_hydrated_row(rel_model, rel_row)),
                    )
        if self._prefetch_related:
            from ferrum.relations import prefetch_related_objects

            await prefetch_related_objects(instances, self._model, self._prefetch_related, conn)
        # ``project()`` returns ``QuerySet[_P]`` via cast; hydration may target a
        # different same-table model than ``_M`` at the type level.
        return cast(list[_M], instances)


class ValuesQuerySet(_QuerySetBase[dict[str, Any]]):
    """QuerySet variant whose terminals return plain ``dict`` rows.

    Produced by ``QuerySet.values(...)``. Each row is the driver row converted to
    a ``dict[str, Any]`` (only the selected columns when ``values(*fields)`` was
    given). ``all()`` returns ``list[dict[str, Any]]``.
    """

    async def _materialize(
        self,
        rows: list[Any],
        metadata: ModelMetadata | None,
        conn: ConnectionLike,
        fingerprint: str,
    ) -> list[dict[str, Any]]:
        return [_row_to_dict(row) for row in rows]


class ValuesListQuerySet(_QuerySetBase[tuple[Any, ...]]):
    """QuerySet variant whose terminals return ``tuple`` rows.

    Produced by ``QuerySet.values_list(...)`` (without ``flat=True``). Tuple
    element order follows the resolved SELECT field order. ``all()`` returns
    ``list[tuple[Any, ...]]``.
    """

    async def _materialize(
        self,
        rows: list[Any],
        metadata: ModelMetadata | None,
        conn: ConnectionLike,
        fingerprint: str,
    ) -> list[tuple[Any, ...]]:
        names = self._resolve_select_field_names(metadata) if metadata is not None else []
        return [tuple(_row_to_dict(row).get(name) for name in names) for row in rows]


class FlatValuesListQuerySet(_QuerySetBase[Any]):
    """QuerySet variant whose terminals return flat scalars.

    Produced by ``QuerySet.values_list(..., flat=True)``. When exactly one field
    is selected each row is the bare scalar value; if more than one field is
    present the row falls back to a tuple (preserving the pre-split behavior).
    ``all()`` returns ``list[Any]``.
    """

    async def _materialize(
        self,
        rows: list[Any],
        metadata: ModelMetadata | None,
        conn: ConnectionLike,
        fingerprint: str,
    ) -> list[Any]:
        names = self._resolve_select_field_names(metadata) if metadata is not None else []
        out: list[Any] = []
        for row in rows:
            row_dict = _row_to_dict(row)
            if len(names) == 1:
                out.append(row_dict[names[0]])
            else:
                out.append(tuple(row_dict.get(name) for name in names))
        return out
