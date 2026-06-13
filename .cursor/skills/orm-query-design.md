# Skill: ORM Query Design

> Expert behavior for designing the QuerySet surface and its IR. Use when adding or changing
> query operators, filters, ordering, projections, or the QuerySet → IR translation.

## When to use

- Designing the public QuerySet API (filter/exclude/order_by/values/etc.).
- Defining how QuerySet operations lower into the typed IR consumed by Rust.
- Adding a new operator, lookup, or sort capability.

## Expert behaviors

- **Django-inspired ergonomics, async-first.** Familiar, chainable, lazy QuerySet semantics, but
  every terminal operation is awaitable. Least Astonishment: behave as a Django/Pydantic
  developer expects.
- **Allowlist-driven.** Every field, operator, and sort direction is validated against
  model-metadata allowlists. Unknown/unsupported inputs fail with structured errors **before** IR
  is finalized and before SQL is emitted.
- **IR is the boundary.** QuerySet builds a typed, versioned IR (ADR-002); it never builds SQL.
  Identifiers and values are kept out-of-band.
- **No escape hatches.** No `extra()`, no raw fragments, no user-supplied templates, no
  production-exposed query inspection.
- **Lazy + predictable.** Query construction is pure and side-effect-free until awaited.

## Workflow

1. Specify the public surface and its semantics (with examples) before implementing.
2. Define how it lowers to IR fields; confirm against the IR contract.
3. Implement validation against allowlists first; emit structured errors for bad input.
4. Add tests: QuerySet → IR shape, allowlist rejection, async terminal behavior.
5. Update docs/README for the new public surface.

## Anti-patterns

- Letting a field/operator name reach SQL without allowlist validation.
- Eager execution or hidden I/O during query construction.
- Adding speculative operators v0.1 does not need (YAGNI).
- Inspecting/exposing compiled SQL in production paths.
