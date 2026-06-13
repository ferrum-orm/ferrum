"""Type stub for the compiled Rust extension ``ferrum._native``.

This stub is hand-maintained and checked by mypy in CI. It must stay in sync
with ``crates/ferrum-pyo3/src/lib.rs``. Integration tests exercise the real
extension to catch stub drift.
"""

class FerrumInternalError(RuntimeError):
    """A Rust panic crossed the PyO3 boundary (sanitized; no addresses/paths)."""

class FerrumCompileError(RuntimeError):
    """IR compilation failed: unknown field, unsupported operator, or IR version mismatch."""

def compile_query(metadata_json: str, ir_json: str) -> dict[str, object]:
    """Compile a ``QuerySetIR`` (JSON) against model metadata (JSON).

    Returns a dict with keys:
        sql_text: str — parameterized SQL ($1, $2, …)
        bound_params: list[str] — JSON-encoded bound values in placeholder order
        param_type_summary: list[str] — Tier A observability summary

    Raises:
        FerrumCompileError: IR validation failed.
        FerrumInternalError: Rust panic (should never occur in normal use).
    """
    ...
