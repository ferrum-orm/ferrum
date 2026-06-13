"""Ferrum QuerySet: lazy, chainable, async query builder.

``QuerySet`` accumulates filter/order/limit/offset state and only touches the
database when a terminal coroutine is awaited. The terminal methods delegate
to the connection layer (asyncpg) and to the Rust compiler (ferrum._native).

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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import ferrum.hooks as _hooks
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumDangerApiError,
    FerrumInternalError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    map_db_error,
)

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import Model, ModelMetadata

_M = TypeVar("_M", bound="Model")

# Module-level reference to the native Rust extension.  Absent when the wheel
# has not been built (e.g. unit-test environments without a compiled extension).
_native_ext: types.ModuleType | None = None
with contextlib.suppress(ImportError):
    _native_ext = importlib.import_module("ferrum._native")

# IR version — must stay in sync with ferrum-core IR_VERSION (crates/ferrum-core/src/ir/mod.rs).
_IR_VERSION: int = 1

_EXT_NOT_BUILT_MSG = (
    "ferrum._native extension not built. "
    "Run: maturin develop  (or: uv run maturin develop) [FERR-C001]"
)


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
    return {"type": "text", "value": str(value)}


def _decode_bound_param(param_json: str) -> object:
    """Decode a BindValue JSON string (from ``compile_query``) to a Python value.

    Reverses ``_encode_bind_value`` so that bound parameters can be passed to
    asyncpg. Called on each element of ``compiled["bound_params"]``.
    """
    parsed: dict[str, Any] = json.loads(param_json)
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
    return val


def _hydrate_rows(model: type[_M], rows: list[Any]) -> list[_M]:
    """Convert asyncpg ``Record`` objects to model instances (ADR-003 trusted path).

    Uses ``model_construct`` (skip re-validation) since rows originate from a
    trusted DB source. Custom validators with side-effects do not re-run here —
    document this to callers and offer opt-in full validation if needed.

    W-3 (Wave 3 tracking): This function bypasses ``_native_ext.hydrate_rows``
    Rust validation. When Track B wires ``hydrate_rows`` in ``ferrum-pyo3``, this
    should delegate to ``_native_ext.hydrate_rows(metadata_json, rows_json)`` for
    non-nullable column validation before ``model_construct``. Until then, the
    trusted-source assumption applies: the DB enforces NOT NULL; custom validators
    do not re-run here.
    """
    # Wave 3: delegate to _native_ext.hydrate_rows() once Track B lands so Rust NULL
    # validation runs on live path.
    return [model.model_construct(**dict(row)) for row in rows]


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

    # ------------------------------------------------------------------
    # Chaining methods (return new QuerySet — no I/O, no SQL)
    # ------------------------------------------------------------------

    def filter(self, **kwargs: Any) -> QuerySet[_M]:  # noqa: ANN401
        """Add equality/lookup filter(s). Returns a new QuerySet.

        Uses Django-style ``field__operator=value`` syntax; bare ``field=value``
        is the ``eq`` lookup. Field names are validated against the model
        metadata allowlist at call time (Stage 0 first gate, QUERY_ENGINE.md §6).
        """
        qs = self._clone()
        metadata = self._get_metadata()
        allowed_fields = {f.name for f in metadata.fields} if metadata is not None else set()
        for lookup, value in kwargs.items():
            if "__" in lookup:
                field_name, operator = lookup.rsplit("__", 1)
            else:
                field_name = lookup
                operator = "eq"
            if metadata is not None and field_name not in allowed_fields:
                raise FerrumCompileError(
                    f"Unknown field {field_name!r} on model {metadata.model_name!r}.",
                    model=metadata.model_name,
                    field=field_name,
                )
            qs._filters.append({"field": field_name, "operator": operator, "value": value})
        if kwargs:
            qs._is_filtered = True
        return qs

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

        # SELECT operation: project all fields (projection subset is a Wave 2 concern).
        select_fields = [{"index": i, "name": f.name} for i, f in enumerate(metadata.fields)]
        operation: dict[str, Any] = {"kind": "select", "fields": select_fields}

        # Filters — validate field names and operators against allowlists.
        ir_filters: list[dict[str, Any]] = []
        for flt in self._filters:
            field_name: str = flt["field"]
            operator: str = flt["operator"]
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
            ir_filters.append(
                {
                    "field": {"index": field_index[field_name], "name": field_name},
                    "operator": operator,
                    "value": _encode_bind_value(flt["value"]),
                }
            )

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

        return {
            "version": _IR_VERSION,
            "model_name": metadata.model_name,
            "operation": operation,
            "filters": ir_filters,
            "order_by": ir_order_by,
            "limit": self._limit,
            "offset": self._offset,
        }

    def _compile(self) -> dict[str, Any]:
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
        return self._compile_ir(self._build_ir())

    def _compile_ir(self, ir: dict[str, Any]) -> dict[str, Any]:
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
        ir_json = json.dumps(ir)
        metadata = self._get_metadata()
        metadata_json = metadata.to_metadata_json() if metadata is not None else "{}"
        try:
            return _native_ext.compile_query(metadata_json, ir_json)  # type: ignore[return-value]
        except FerrumCompileError:
            raise
        except RuntimeError as exc:
            # ADR-006: PyO3 raises RuntimeError for FerrumCompileError on the Rust side;
            # remap here so callers always catch FerrumCompileError. Tracked for ADR-006
            # centralized error layer.
            raise FerrumCompileError(str(exc), model=self._model.__name__) from None

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

    async def create(self, conn: Connection, **values: Any) -> _M:  # noqa: ANN401
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
        compiled = self._compile_ir(self._build_insert_ir(values))
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        pool = conn._require_pool()
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
            row = await pool.fetchrow(sql_text, *bound_params)
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
        return self._model.model_construct(**dict(row))

    async def delete(self, conn: Connection | None = None) -> int:
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
        compiled = self._compile_ir(self._build_delete_ir())
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        pool = conn._require_pool()
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
            result: str = await pool.execute(sql_text, *bound_params)
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

    async def danger_delete_all(self, conn: Connection) -> int:
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
        compiled = qs_all._compile_ir(delete_ir)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        pool = conn._require_pool()
        model_name = self._model.__name__
        metadata_all = qs_all._get_metadata()
        table = metadata_all.table_name if metadata_all is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint, model=model_name, operation="delete", table=table
        )
        t0 = time.monotonic()
        try:
            result: str = await pool.execute(sql_text, *bound_params)
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

    async def update(self, conn: Connection | None = None, **assignments: Any) -> int:  # noqa: ANN401
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
        compiled = self._compile_ir(self._build_update_ir(assignments))
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        pool = conn._require_pool()
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
            result: str = await pool.execute(sql_text, *bound_params)
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

    async def danger_update_all(self, conn: Connection, **assignments: Any) -> int:  # noqa: ANN401
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
        compiled = qs_all._compile_ir(update_ir)
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        pool = conn._require_pool()
        model_name = self._model.__name__
        metadata_all = qs_all._get_metadata()
        table = metadata_all.table_name if metadata_all is not None else model_name
        _hooks.query_start(
            fingerprint=fingerprint, model=model_name, operation="update", table=table
        )
        t0 = time.monotonic()
        try:
            result: str = await pool.execute(sql_text, *bound_params)
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

    async def all(self, conn: Connection) -> list[_M]:
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
        compiled = self._compile()
        sql_text: str = compiled["sql_text"]
        bound_params = [_decode_bound_param(p) for p in compiled["bound_params"]]
        fingerprint: str = compiled.get("fingerprint", "")  # type: ignore[assignment]
        pool = conn._require_pool()
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
            rows = await pool.fetch(sql_text, *bound_params)
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
        return _hydrate_rows(self._model, rows)

    async def first(self, conn: Connection) -> _M | None:
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

    async def get(self, conn: Connection, **kwargs: Any) -> _M:  # noqa: ANN401
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
        return results[0]

    async def count(self, conn: Connection) -> int:
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
        compiled = count_qs._compile()
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
        pool = conn._require_pool()
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
            result = await pool.fetchval(count_sql, *bound_params)
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clone(self) -> QuerySet[_M]:
        qs: QuerySet[_M] = QuerySet(self._model)
        qs._filters = list(self._filters)
        qs._order_by = list(self._order_by)
        qs._limit = self._limit
        qs._offset = self._offset
        qs._is_filtered = self._is_filtered
        return qs

    def _get_metadata(self) -> ModelMetadata | None:
        """Return the model's ``ModelMetadata`` if available, else ``None``."""
        return getattr(self._model, "__ferrum_metadata__", None)
