# Rule: Rust

> Constraints for the Rust core crate. Rust owns performance-critical internals only and is a
> **pure, synchronous, stateless** compiler/codec. See `AGENTS.md` §2 and §4.

## Scope ownership

Rust owns exactly two responsibilities:

1. **Compilation:** `(&Metadata, QuerySetIR) → { sql_text, bound_params, param_type_summary }`.
2. **Hydration:** `raw DB rows → typed payload` for Python to construct models from.

Rust does **not** own: async, I/O, networking, connection pooling, transactions, retries,
cancellation, timeouts, or any stateful per-request behavior.

## Hard constraints

- **Pure and synchronous.** Compilation is a pure function over `(&Metadata, QuerySetIR)`
  producing fresh owned output per call. No `async`, no runtime, no global mutable state.
- **No per-request mutable shared state.** Model metadata is built once at class-definition time
  and is read-only thereafter. Do not cache request-scoped state across calls.
- **Off the I/O path.** Never block on, wait for, or poll I/O inside Rust. The compile call holds
  the GIL and is sub-millisecond; do not release/reacquire the GIL for it.
- **Allowlist-structural safety.** Identifiers are validated against metadata allowlists inside
  the compiler; values are emitted only as bound parameters carried out-of-band from identifiers.
  Unknown fields / unsupported operators / invalid sort directions fail with structured errors
  **before** SQL is emitted.
- **No panics across the boundary as aborts.** Build with `panic = "unwind"`; the PyO3 wrapper
  maps `Result::Err` and panics to catchable Python exceptions. Error payloads carry structured
  fields (model, field, operator, category) — never formatted trace blobs, memory addresses, or
  local paths.

## Style and tooling

- Idiomatic Rust; prefer borrowing over cloning where it does not complicate ownership.
- Errors via `Result` + a typed error enum; no `unwrap()`/`expect()` on boundary-reachable paths.
- `cargo check` and `clippy` must pass clean for touched code; no new warnings.
- Rust unit tests live with the crate; compilation correctness and allowlist rejection are tested.

## Definition of done (Rust)

- [ ] Pure, synchronous, stateless; no async or I/O introduced.
- [ ] Allowlist rejection paths covered by unit tests.
- [ ] `cargo check` + `clippy` clean on touched code.
- [ ] Errors map to structured fields; no panics that would abort the process.
