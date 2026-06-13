# Skill: Rust Core Engineering

> Expert behavior for the Rust compiler/codec crate. Use when working on QuerySet IR → SQL
> compilation, parameter binding, allowlist enforcement, or row hydration in Rust.

## When to use

- Implementing or changing the `(&Metadata, QuerySetIR) → { sql_text, bound_params,
  param_type_summary }` compiler.
- Implementing hydration of trusted DB rows into typed payloads.
- Enforcing identifier allowlists and operator/sort-direction validation inside Rust.

## Expert behaviors

- **Purity is the contract.** Compilation is a pure function over `(&Metadata, QuerySetIR)`
  producing fresh owned output. No async, no I/O, no global mutable state, no request-scoped
  caching.
- **Structural safety.** Identifiers are validated against the metadata allowlist; values are
  emitted only as bound parameters carried out-of-band from identifiers. Reject unknown fields,
  unsupported operators, and invalid sort directions with structured errors **before** emitting
  SQL.
- **GIL-aware.** The compile call holds the GIL and is sub-millisecond. Do not release/reacquire
  it; do not introduce cancellable waiting.
- **Errors, not panics.** Use `Result` + a typed error enum on boundary-reachable paths. Reserve
  panics for true invariant violations; the boundary maps them to catchable Python exceptions
  (`panic = "unwind"`).
- **Idiomatic and lean.** Borrow over clone where it doesn't complicate ownership; keep
  allocations predictable. `cargo check` + `clippy` clean, no new warnings.

## Workflow

1. Confirm the IR contract (ADR-002) for the shape you consume.
2. Implement the pure transform with explicit allowlist checks first.
3. Add Rust unit tests: happy path, allowlist rejection, parameter binding, hydration shape.
4. Verify `cargo check`/`clippy`; ensure no panic-as-abort paths reachable from the boundary.

## Anti-patterns

- Any `async`, runtime, or I/O in the crate.
- Per-request mutable shared state or cross-call caching.
- `unwrap()`/`expect()` on boundary-reachable paths.
- Emitting identifiers as string-interpolated SQL instead of validated, structural output.
