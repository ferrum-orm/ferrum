"""Type stub for the compiled Rust extension ``ferrum._native``.

This stub is hand-maintained and checked by ty in CI. It must stay in sync
with ``crates/ferrum-pyo3/src/lib.rs``. Integration tests exercise the real
extension to catch stub drift.

PEP 561 marker: ``python/ferrum/py.typed`` (present, empty — the marker file
itself is the signal; contents are not specified by PEP 561).
"""

from __future__ import annotations

from typing import TypedDict

__all__ = [
    "CompiledQuery",
    "CompiledQueryMsgpack",
    "FerrumCompileError",
    "FerrumHydrationError",
    "FerrumInternalError",
    "compile_query",
    "compile_query_msgpack",
    "hydrate_rows",
    "hydrate_rows_msgpack",
    "plan_migration",
]

class CompiledQuery(TypedDict):
    """Return type of :func:`compile_query` (JSON wire format).

    Keys:
        sql_text: Parameterized SQL (``$1``/``$2`` for PostgreSQL, ``?`` for
            MySQL/SQLite/MSSQL; MSSQL uses ``[bracket]`` quoting,
            ``OUTPUT INSERTED.*`` for returning, and ``OFFSET/FETCH`` pagination).
        bound_params: JSON-encoded bound values in placeholder order.
            Never logged in Tier A hooks.
        param_type_summary: Tier A observability summary (type names only;
            no values).
        fingerprint: Stable FNV-1a hash of the SQL shape.
        operation: Python routing key — one of ``"select"``, ``"insert"``,
            ``"update"``, ``"delete"``, ``"bulk_insert"``, ``"bulk_update"``,
            ``"bulk_delete"``. Python uses this to route to the correct
            asyncpg call (``fetch`` for select/insert/update with returning,
            ``execute`` for delete or update-without-return).
    """

    sql_text: str
    bound_params: list[str]
    param_type_summary: list[str]
    fingerprint: str
    operation: str

class CompiledQueryMsgpack(TypedDict):
    """Return type of :func:`compile_query_msgpack` (MessagePack wire format).

    Identical to :class:`CompiledQuery` except ``bound_params`` is a single
    MessagePack blob (the NAMED encoder, so the tagged ``BindValue`` enum
    round-trips as a map that ``msgpack.unpackb`` reads).
    """

    sql_text: str
    bound_params: bytes
    param_type_summary: list[str]
    fingerprint: str
    operation: str

class FerrumInternalError(RuntimeError):
    """A Rust panic crossed the PyO3 boundary (sanitized; no addresses/paths)."""

class FerrumCompileError(RuntimeError):
    """IR compilation failed: unknown field, unsupported operator, IR version mismatch,
    or malformed JSON input."""

class FerrumHydrationError(RuntimeError):
    """Row hydration failed: missing required column, type mismatch, or malformed JSON."""

def compile_query(metadata_json: str, ir_json: str, dialect: str = "postgres") -> CompiledQuery:
    """Compile a ``QuerySetIR`` (JSON) against model metadata (JSON).

    ``dialect`` is one of ``postgres``, ``mysql``, ``sqlite``, ``mssql``.

    Returns a :class:`CompiledQuery` dict with keys:
        sql_text: str — parameterized SQL (``$1``/``$2`` for PostgreSQL, ``?``
            for MySQL/SQLite/MSSQL; MSSQL uses ``[bracket]`` quoting,
            ``OUTPUT INSERTED.*`` for returning, and ``OFFSET/FETCH`` pagination)
        bound_params: list[str] — JSON-encoded bound values in placeholder order
        param_type_summary: list[str] — Tier A observability summary (no values)
        fingerprint: str — stable FNV-1a hash of the SQL shape
        operation: str — Python routing key (``"select"``/``"insert"``/...)

    Raises:
        FerrumCompileError: IR validation failed or JSON is malformed.
        FerrumInternalError: Rust panic (should never occur in normal use).
    """

def hydrate_rows(metadata_json: str, rows_json: str) -> list[dict[str, object]]:
    """Hydrate a batch of DB-origin rows against model metadata.

    ``rows_json`` must be a JSON array of objects mapping column names to values.
    Returns a list of dicts (one per row) ready for ``model_construct(**row)``.

    Raises:
        FerrumHydrationError: Required column missing/null, or JSON is malformed.
        FerrumInternalError: Rust panic (should never occur in normal use).
    """

def compile_query_msgpack(
    metadata_mp: bytes, ir_mp: bytes, dialect: str = "postgres"
) -> CompiledQueryMsgpack:
    """Compile a ``QuerySetIR`` from MessagePack-encoded metadata and IR.

    Identical semantics to :func:`compile_query` but ``metadata_mp`` and
    ``ir_mp`` are MessagePack bytes. ``bound_params`` is returned as a single
    MessagePack blob (``bytes``) — the NAMED encoder, so the tagged ``BindValue``
    enum round-trips as a map that ``msgpack.unpackb`` reads. Other keys
    (``sql_text``, ``param_type_summary``, ``fingerprint``, ``operation``) are
    native dict values.

    Raises:
        FerrumCompileError: IR invalid or MessagePack input malformed.
        FerrumInternalError: Rust panic or bound_params encode failure.
    """

def hydrate_rows_msgpack(metadata_mp: bytes, rows_mp: bytes) -> list[dict[str, object]]:
    """Hydrate DB-origin rows from MessagePack-encoded metadata and rows.

    Identical semantics to :func:`hydrate_rows` but accepts MessagePack bytes.

    Raises:
        FerrumHydrationError: Required column missing/null, or input malformed.
        FerrumInternalError: Rust panic (should never occur in normal use).
    """

def plan_migration() -> None:
    """Plan a schema migration (Wave 4 — not yet implemented).

    Raises:
        NotImplementedError: Always; migration planning is not yet implemented.
    """
