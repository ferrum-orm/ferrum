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
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast
from uuid import UUID

import ferrum.hooks as _hooks
from ferrum.config import resolve_wire_format as _resolve_wire_format
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
    from ferrum.models import Model, ModelMetadata

    # Terminals accept an open Connection or an active Transaction interchangeably:
    # both expose the ``dialect`` / ``_require_driver()`` surface the terminals use.
    ConnectionLike = Connection | Transaction

_M = TypeVar("_M", bound="Model")

# Module-level reference to the native Rust extension.  Absent when the wheel
# has not been built (e.g. unit-test environments without a compiled extension).
_native_ext: types.ModuleType | None = None
with contextlib.suppress(ImportError):
    _native_ext = importlib.import_module("ferrum._native")

# IR version — must stay in sync with ferrum-core IR_VERSION (crates/ferrum-core/src/ir/mod.rs).
_IR_VERSION: int = 3

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
    if isinstance(obj, (datetime, UUID)):
        return str(obj)
    if hasattr(obj, "_mapping"):
        return dict(obj._mapping)
    raise TypeError(f"Object of type {type(obj).__name__} is not msgpack-serializable")


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


class _RowEncoder(json.JSONEncoder):
    """JSON encoder for driver rows and non-JSON-native Python types.

    Used to serialize rows for the Rust ``hydrate_rows`` structural check.
    Complex types (datetime, UUID) are converted to strings — Rust performs
    structural validation (presence, nullability) only; Python retains native
    types for ``model_construct``.
    """

    def default(self, o: Any) -> Any:  # noqa: ANN401
        if isinstance(o, (datetime, UUID)):
            return str(o)
        if hasattr(o, "_mapping"):
            return dict(o._mapping)
        return super().default(o)


def _parse_lookup(lookup: str) -> tuple[str, str]:
    """Split ``field__operator`` lookup syntax into a field name and operator.

    Bare field names are equality lookups. The split is intentionally from the
    right so field names may contain relation-style prefixes in later IR shapes
    without changing the lookup operator rule.
    """
    if "__" in lookup:
        field_name, operator = lookup.rsplit("__", 1)
        return field_name, operator
    return lookup, "eq"


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


def _filter_dict_to_ir(
    flt: dict[str, Any],
    metadata: ModelMetadata,
    field_index: dict[str, int],
) -> dict[str, Any]:
    """Convert one normalized filter dict to a compiler-ready IR leaf."""
    field_name: str = flt["field"]
    operator: str = flt["operator"]
    _validate_lookup(field_name, operator, metadata, field_index=field_index)
    return {
        "field": {"index": field_index[field_name], "name": field_name},
        "operator": operator,
        "value": _encode_bind_value(flt["value"]),
    }


def _kwargs_to_ir_filters(
    kwargs: dict[str, Any],
    metadata: ModelMetadata,
    field_index: dict[str, int],
) -> list[dict[str, Any]]:
    """Convert Django-style keyword lookups into validated predicate leaves."""
    leaves: list[dict[str, Any]] = []
    for lookup, value in kwargs.items():
        field_name, operator = _parse_lookup(lookup)
        _validate_lookup(field_name, operator, metadata, field_index=field_index)
        leaves.append(
            {
                "kind": "filter",
                "filter": {
                    "field": {"index": field_index[field_name], "name": field_name},
                    "operator": operator,
                    "value": _encode_bind_value(value),
                },
            }
        )
    return leaves


def _q_to_predicate(
    q: Q,
    metadata: ModelMetadata,
    field_index: dict[str, int],
) -> dict[str, Any]:
    """Serialize a ``Q`` boolean tree into the IR predicate shape.

    Each leaf is validated through the same metadata allowlist as plain
    ``filter(**kwargs)``. Empty ``Q()`` objects are rejected because compiling an
    empty predicate would make caller intent ambiguous for destructive terminals.
    """

    def walk(node: Q) -> dict[str, Any]:
        children_ir: list[dict[str, Any]] = []
        for child in node.children:
            if isinstance(child, Q):
                children_ir.append(walk(child))
            elif isinstance(child, dict):
                leaves = _kwargs_to_ir_filters(child, metadata, field_index)
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

    return walk(q)


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

    instances = [model.model_construct(**row) for row in row_dicts]
    if deferred:
        for inst in instances:
            object.__setattr__(inst, "__ferrum_deferred__", deferred)
    return instances


class QuerySet(Generic[_M]):
    """Lazy, chainable query builder for a Ferrum model.

    All filter/order/limit/offset methods return a new ``QuerySet`` instance
    (immutable chaining). Terminal coroutines (``all``, ``get``, ``first``,
    ``count``, ``delete``, ``update``) are async and require an active connection.

    ``_build_ir()`` serializes the accumulated state to the ADR-002 v1 IR shape
    (a plain dict) without touching the database or emitting any SQL.
    """

    def __init__(self, model: type[_M]) -> None:
        self._model = model
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
        self._result_type: Literal["models", "values", "values_list"] = "models"
        self._values_flat: bool = False
        self._select_related: tuple[str, ...] = ()
        self._prefetch_related: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Chaining methods (return new QuerySet — no I/O, no SQL)
    # ------------------------------------------------------------------

    def filter(self, *args: Q | dict[str, Any], **kwargs: Any) -> QuerySet[_M]:  # noqa: ANN401
        """Add filter(s), including ``Q`` boolean trees. Returns a new QuerySet.

        Uses Django-style ``field__operator=value`` syntax; bare ``field=value``
        is the ``eq`` lookup. Field names are validated against the model
        metadata allowlist at call time (Stage 0 first gate, QUERY_ENGINE.md §6).
        """
        q = args_to_q(*args, **kwargs)
        if q is None:
            return self._clone()
        qs = self._clone()
        metadata = self._get_metadata()
        if metadata is not None:
            field_index = {f.name: i for i, f in enumerate(metadata.fields)}
            _q_to_predicate(q, metadata, field_index)
        qs._predicate_q = q if qs._predicate_q is None else qs._predicate_q & q
        qs._is_filtered = True
        return qs

    def exclude(self, *args: Q | dict[str, Any], **kwargs: Any) -> QuerySet[_M]:  # noqa: ANN401
        """Exclude rows matching the given lookups (``~Q(...)``)."""
        q = args_to_q(*args, **kwargs)
        if q is None:
            return self._clone()
        return self.filter(~q)

    def distinct(self) -> QuerySet[_M]:
        """Return a QuerySet that emits ``SELECT DISTINCT``."""
        qs = self._clone()
        qs._distinct = True
        return qs

    def only(self, *fields: str) -> QuerySet[_M]:
        """Limit SELECT columns; deferred fields raise on access."""
        qs = self._clone()
        qs._only_fields = fields
        qs._defer_fields = frozenset()
        return qs

    def defer(self, *fields: str) -> QuerySet[_M]:
        """Defer loading of the given fields."""
        qs = self._clone()
        qs._defer_fields = frozenset(fields)
        qs._only_fields = None
        return qs

    def values(self, *fields: str) -> QuerySet[_M]:
        """Return rows as dicts instead of model instances."""
        qs = self._clone()
        qs._result_type = "values"
        if fields:
            qs._only_fields = fields
        return qs

    def values_list(self, *fields: str, flat: bool = False) -> QuerySet[_M]:
        """Return rows as tuples (or a flat list when ``flat=True`` and one field)."""
        qs = self._clone()
        qs._result_type = "values_list"
        qs._values_flat = flat
        if fields:
            qs._only_fields = fields
        return qs

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

    def __getitem__(self, key: slice | int) -> QuerySet[_M]:
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

    def order_by(self, *fields: str) -> QuerySet[_M]:
        """Set ORDER BY. Prefix field with '-' for DESC. Returns a new QuerySet."""
        qs = self._clone()
        for f in fields:
            if f.startswith("-"):
                qs._order_by.append({"field": f[1:], "direction": "desc"})
            else:
                qs._order_by.append({"field": f, "direction": "asc"})
        return qs

    def limit(self, count: int) -> QuerySet[_M]:
        """Set LIMIT. Returns a new QuerySet."""
        qs = self._clone()
        qs._limit = count
        return qs

    def offset(self, count: int) -> QuerySet[_M]:
        """Set OFFSET. Returns a new QuerySet."""
        qs = self._clone()
        qs._offset = count
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
        qs._vector_order_by = {
            "field": {"index": field_index[field], "name": field},
            "metric": metric,
            "value": _encode_bind_value(vector),
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
        if self._predicate_q is not None:
            ir["predicate"] = _q_to_predicate(self._predicate_q, metadata, field_index)
        if self._select_related:
            from ferrum.relations import build_join_ir

            ir["joins"] = [
                build_join_ir(metadata, name, field_index) for name in self._select_related
            ]
        else:
            ir["joins"] = []
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
        ir_values: list[Any] = []
        for name, value in values.items():
            if name not in field_index:
                raise FerrumCompileError(
                    f"Unknown field {name!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=name,
                )
            ir_values.append(
                [{"index": field_index[name], "name": name}, _encode_bind_value(value)]
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
        ir_assignments: list[Any] = []
        for name, value in assignments.items():
            if name not in field_index:
                raise FerrumCompileError(
                    f"Unknown field {name!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=name,
                )
            ir_assignments.append(
                [{"index": field_index[name], "name": name}, _encode_bind_value(value)]
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

    def to_ir_json(self) -> str:
        """Serialize the current QuerySet state to the ADR-002 v1 IR JSON string.

        This is the ``ir_json`` argument for ``ferrum._native.compile_query``.
        Calls ``_build_ir()`` internally, so all Python-side allowlist checks
        fire before serialization.
        """
        return json.dumps(self._build_ir())

    # ------------------------------------------------------------------
    # Danger API guards (AGENTS.md §3 / ARCHITECTURE.md §3.9)
    # ------------------------------------------------------------------

    async def create(self, conn: ConnectionLike, **values: Any) -> _M:  # noqa: ANN401
        """Insert a single row. Returns the hydrated model instance.

        Builds an INSERT IR from ``values``, compiles it through the Rust
        extension, executes ``INSERT … RETURNING *`` via asyncpg ``fetchrow``,
        and constructs the model instance via the ADR-003 trusted hydration path.

        Dispatches Tier A ``query_start`` / ``query_success`` / ``query_failure``
        hook payloads (non-bypassable redaction via ``hooks.dispatch``).

        Args:
            conn: An open ``Connection`` (obtained from ``ferrum.connect()``).
            **values: Field names and their values to insert. Field names are
                validated against the model's allowlist before compilation.

        Raises:
            FerrumConfigError: if the native extension is not built.
            FerrumCompileError: if a field name is not in the model's allowlist.
            FerrumInternalError: if the INSERT returned no row (should not occur
                when the DB is healthy and the table exists).
        """
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
        return self._model.model_construct(**_row_to_dict(row))

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
        """Normalize a bulk-write input object to a mutable field-value dict."""
        if isinstance(obj, dict):
            return dict(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        msg = f"bulk_create() expected model instances or dicts, got {type(obj)!r}."
        raise TypeError(msg)

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
        pk_name = self._pk_field_name(metadata)
        ir_rows: list[list[Any]] = []
        column_order: list[str] | None = None
        for row in rows:
            values = dict(row)
            # Drop auto-generated PK columns with sentinel zero/null values.
            if pk_name in values and values[pk_name] in (0, None, ""):
                values.pop(pk_name, None)
            if column_order is None:
                column_order = sorted(values.keys())
            elif sorted(values.keys()) != column_order:
                raise FerrumCompileError(
                    "bulk_create() rows must share the same field set.",
                    model=metadata.model_name,
                )
            ir_row: list[Any] = []
            for name in column_order:
                if name not in field_index:
                    raise FerrumCompileError(
                        f"Unknown field {name!r} on model {metadata.model_name!r}.",
                        model=metadata.model_name,
                        field=name,
                    )
                ir_row.append(
                    [{"index": field_index[name], "name": name}, _encode_bind_value(values[name])]
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
            if name not in field_index:
                raise FerrumCompileError(
                    f"Unknown field {name!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=name,
                )
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
                    "values": [_encode_bind_value(assignments[name]) for name in field_list],
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
        row_dicts = [self._object_to_row_dict(obj) for obj in objects]
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
                batch_instances = [self._model.model_construct(**_row_to_dict(row)) for row in rows]
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
        rows: list[tuple[Any, dict[str, Any]]] = []
        for obj in objects:
            data = obj.model_dump()
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
        if dialect == "mssql":
            raise FerrumConfigError(
                "upsert()/bulk_upsert() (MERGE) is not supported on the MSSQL backend "
                "in this version. Use separate insert/update calls. [FERR-C001]"
            )
        field_by_name = {f.name: f for f in metadata.fields}
        table = f'"{metadata.table_name}"'

        col_names: list[str] = []
        placeholders: list[str] = []
        bound: list[Any] = []
        for i, (fname, fval) in enumerate(values.items(), start=1):
            col = f'"{field_by_name[fname].column_name}"'
            col_names.append(col)
            placeholders.append(f"${i}")
            bound.append(fval)

        conflict_cols = ", ".join(f'"{field_by_name[cf].column_name}"' for cf in conflict_fields)

        insert_part = (
            f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({', '.join(placeholders)})"
        )

        if update_fields is None:
            # Default: all non-PK, non-conflict fields.
            conflict_set = set(conflict_fields)
            update_fields = [
                f.name
                for f in metadata.fields
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
            if fname not in field_names:
                raise FerrumCompileError(
                    f"Unknown field {fname!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=fname,
                )
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
                if uf not in field_names:
                    raise FerrumCompileError(
                        f"Unknown update field {uf!r} on model {metadata.model_name!r}.",
                        model=metadata.model_name,
                        field=uf,
                    )

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
        return self._model.model_construct(**_row_to_dict(row))

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
                if uf not in field_names:
                    raise FerrumCompileError(
                        f"Unknown update field {uf!r} on model {metadata.model_name!r}.",
                        model=metadata.model_name,
                        field=uf,
                    )

        row_dicts = [self._object_to_row_dict(obj) for obj in objects]
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
                            upserted.append(self._model.model_construct(**_row_to_dict(row)))
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

    # ------------------------------------------------------------------
    # Terminal coroutines (async) — require open Connection
    # ------------------------------------------------------------------

    async def all(self, conn: ConnectionLike) -> list[_M] | list[dict[str, Any]] | list[Any]:
        """Fetch all matching rows and return model instances.

        Compiles the QuerySet IR via the Rust extension, executes the
        parameterized SQL against the pool, and constructs model instances via
        the ADR-003 trusted hydration path (``model_construct``).

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
            raise mapped from None
        duration_ms = (time.monotonic() - t0) * 1000
        _hooks.query_success(
            fingerprint=fingerprint,
            duration_ms=duration_ms,
            row_count=len(rows),
        )
        if self._result_type == "values":
            return [_row_to_dict(row) for row in rows]
        if self._result_type == "values_list":
            names = self._resolve_select_field_names(metadata) if metadata is not None else []
            values_out: list[Any] = []
            for row in rows:
                row_dict = _row_to_dict(row)
                if self._values_flat and len(names) == 1:
                    values_out.append(row_dict[names[0]])
                else:
                    values_out.append(tuple(row_dict.get(name) for name in names))
            return values_out
        deferred = self._deferred_field_names(metadata) if metadata is not None else None
        instances = _hydrate_rows(
            self._model,
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
                    set_relation(inst, rel_name, rel_model.model_construct(**rel_row))
        if self._prefetch_related:
            from ferrum.relations import prefetch_related_objects

            await prefetch_related_objects(instances, self._model, self._prefetch_related, conn)
        return instances

    async def first(self, conn: ConnectionLike) -> _M | None:
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
        return cast(_M, results[0]) if results else None

    async def get(self, conn: ConnectionLike, **kwargs: Any) -> _M:  # noqa: ANN401
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
        qs: QuerySet[_M] = self.filter(**kwargs) if kwargs else self
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
        return cast(_M, results[0])

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

    def _clone(self) -> QuerySet[_M]:
        """Copy accumulated query state for immutable chaining."""
        qs: QuerySet[_M] = QuerySet(self._model)
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
        qs._result_type = self._result_type
        qs._values_flat = self._values_flat
        qs._select_related = self._select_related
        qs._prefetch_related = self._prefetch_related
        return qs

    def _get_metadata(self) -> ModelMetadata | None:
        """Return the model's ``ModelMetadata`` if available, else ``None``."""
        return getattr(self._model, "__ferrum_metadata__", None)
