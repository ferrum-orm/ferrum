# Wave 1 — Architect Gate Verdict

**Status: BLOCKS (pending IR fix agent)**

## Blocking issues

Two wire-format mismatches were found at the Rust↔Python boundary:

### 1. BindValue tagged-union format (ADR-002)
- **Python side** (`_encode_bind_value`) emits `{"text": "hello"}` — a plain single-key dict.
- **Rust side** (`ferrum-core/src/ir/mod.rs`) expects the `serde` adjacently-tagged format: `{"type": "text", "value": "hello"}`.
- Consequence: the Rust `compile_query` call would fail to deserialise any filter value.
- Fix owner: IR fix agent (touching `crates/ferrum-core/src/ir/mod.rs` or aligning the Python encoder).

### 2. ModelMetadata serialisation gap
- `ModelMetadata` has no `to_metadata_json()` / `to_dict()` method; `_compile()` currently passes `"{}"` as a placeholder.
- The Rust `compile_query` signature accepts `(metadata_json: &str, ir_json: &str)`; without real metadata the Rust layer cannot validate against the field allowlist.
- Fix owner: IR fix agent (Wave 2 scope — define serialisation contract in ADR-002 and implement on both sides).

## What is correct

- **Boundary discipline is sound.** Python owns async I/O and GIL-held compile dispatch; Rust is a pure synchronous stateless function. No async leaks into Rust.
- **Security model is correct.** Python-side allowlist checks (field names, operators, sort directions) fire before any cross-boundary call, so the Rust layer is never reached with unsanitised identifiers.
- **Error propagation shape is correct.** `FerrumCompileError` carries structured fields (`model`, `field`, `operator`) without raw PostgreSQL detail or bound values.
- The IR version constant (`_IR_VERSION = 1`) is defined in Python and referenced in Rust via `IR_VERSION`; these must stay in sync but are currently consistent.

## Gate re-pass criteria

1. BindValue serde format aligned between Python and Rust (both sides use the same tagged-union convention).
2. `ModelMetadata.to_metadata_json()` implemented and wired into `QuerySet._compile()`.
3. `test_queryset_ir.py` round-trip tests pass end-to-end.
