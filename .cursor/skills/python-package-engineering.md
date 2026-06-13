# Skill: Python Package Engineering

> Expert behavior for building and maintaining the public Python package (`ferrum`). Use when
> working on the public API, model base classes, QuerySet surface, pool/transactions, hooks, or
> packaging of the Python side.

## When to use

- Designing or changing any public, importable API surface.
- Wiring Pydantic v2 models into Ferrum metadata.
- Implementing async runtime integration, the connection pool, or transactions.
- Implementing hook dispatch and the error-mapping surface (Python side of ADR-006).

## Expert behaviors

- **API as contract.** Treat every public name as a long-lived contract. Prefer additive,
  backward-compatible changes (Schema Evolution). A public API change is incomplete without a
  docs update in the same change.
- **Async-first, always.** No sync methods, no blocking shims. Cancellation and timeouts are
  handled at the await point.
- **Pydantic v2 is the single source of truth.** Derive Ferrum metadata from the model; never
  maintain a parallel persistence schema. Build metadata once at class definition time
  (read-only thereafter — it crosses into Rust).
- **Stay inside the boundary.** Hand IR to Rust for compilation; never build SQL strings in
  Python. Construct models from Rust-hydrated payloads.
- **Sanitized errors.** Raise from the Ferrum taxonomy; never echo values, DSNs, or secrets.
- **Observability defaults.** Hook payloads stay Tier A by default.

## Workflow

1. Locate the contract: PRD + architecture review for the affected surface.
2. Design the smallest public surface that satisfies it; note alternatives.
3. Implement async, typed, lint-clean code on touched files.
4. Add unit tests (metadata build, IR shape, error mapping, cancellation).
5. Update `README.md`/docs for any public change.

## Anti-patterns

- Synchronous convenience wrappers "just for tests".
- Building SQL or doing hydration in Python.
- Duplicate persistence schema alongside the Pydantic model.
- Leaking driver `DETAIL`/`HINT` or DSNs into exceptions or logs.
