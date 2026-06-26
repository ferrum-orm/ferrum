"""Type stub for the compiled Rust extension ``ferrum._native``.

This stub is hand-maintained and checked by ty in CI. It must stay in sync
with ``crates/ferrum-pyo3/src/lib.rs``. Integration tests exercise the real
extension to catch stub drift.
"""

class FerrumInternalError(RuntimeError):
    """A Rust panic crossed the PyO3 boundary (sanitized; no addresses/paths)."""

class FerrumCompileError(RuntimeError):
    """IR compilation failed: unknown field, unsupported operator, IR version mismatch,
    or malformed JSON input."""

class FerrumHydrationError(RuntimeError):
    """Row hydration failed: missing required column, type mismatch, or malformed JSON."""

def compile_query(metadata_json: str, ir_json: str, dialect: str = "postgres") -> dict[str, object]:
    """Compile a ``QuerySetIR`` (JSON) against model metadata (JSON).

    ``dialect`` is one of ``postgres``, ``mysql``, ``sqlite``, ``mssql``.

    Returns a dict with keys:
        sql_text: str — parameterized SQL ($1/$2 for PostgreSQL, ? for
            MySQL/SQLite/MSSQL; MSSQL uses [bracket] quoting, OUTPUT INSERTED.*
            for returning, and OFFSET/FETCH pagination)
        bound_params: list[str] — JSON-encoded bound values in placeholder order
        param_type_summary: list[str] — Tier A observability summary (no values)
        fingerprint: str — stable FNV-1a hash of the SQL shape

    Raises:
        FerrumCompileError: IR validation failed or JSON is malformed.
        FerrumInternalError: Rust panic (should never occur in normal use).
    """
    ...

def hydrate_rows(metadata_json: str, rows_json: str) -> list[dict[str, object]]:
    """Hydrate a batch of DB-origin rows against model metadata.

    ``rows_json`` must be a JSON array of objects mapping column names to values.
    Returns a list of dicts (one per row) ready for ``model_construct(**row)``.

    Raises:
        FerrumHydrationError: Required column missing/null, or JSON is malformed.
        FerrumInternalError: Rust panic (should never occur in normal use).
    """
    ...

def compile_query_msgpack(
    metadata_mp: bytes, ir_mp: bytes, dialect: str = "postgres"
) -> dict[str, object]:
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
    ...

def hydrate_rows_msgpack(metadata_mp: bytes, rows_mp: bytes) -> list[dict[str, object]]:
    """Hydrate DB-origin rows from MessagePack-encoded metadata and rows.

    Identical semantics to :func:`hydrate_rows` but accepts MessagePack bytes.

    Raises:
        FerrumHydrationError: Required column missing/null, or input malformed.
        FerrumInternalError: Rust panic (should never occur in normal use).
    """
    ...

def plan_migration() -> None:
    """Plan a schema migration (Wave 4 — not yet implemented).

    Raises:
        NotImplementedError: Always; migration planning is not yet implemented.
    """
    ...
