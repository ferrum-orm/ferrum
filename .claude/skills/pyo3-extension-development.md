# Skill: PyO3 Extension Development

> Expert behavior for the Rust↔Python boundary built with PyO3 + maturin. Use when wiring the IR
> across the boundary, mapping errors/panics, or building/packaging the extension.

## When to use

- Defining or evolving the typed, versioned IR contract that crosses the boundary (ADR-002).
- Mapping Rust `Result::Err` and panics to catchable Python exceptions (ADR-006).
- Wiring hydration (construct-without-revalidate) at the boundary (ADR-003).
- Configuring maturin builds and the wheel matrix (ADR-005).

## Expert behaviors

- **Out-of-band identifiers and values.** The IR carries identifiers and values separately so
  parameterization and allowlisting are structural, not conventional. Keep them separated end to
  end.
- **Synchronous, GIL-holding compile.** Do not release/reacquire the GIL for the sub-millisecond
  compile; do not put cancellable waiting in Rust. Cancellation/timeouts stay in Python.
- **Catchable, sanitized failures.** Build with `panic = "unwind"`. Map errors/panics to the
  Ferrum exception taxonomy with structured fields (model, field, operator, category). Never leak
  trace blobs, memory addresses, or local paths.
- **Trusted-source hydration.** Default to Pydantic v2 construct-without-revalidate for DB-origin
  rows; document the trusted-source assumption and custom-validator caveat where wired.
- **abi3 + maturin.** Prefer abi3 wheels per the packaging ADR; keep the build reproducible.

## Workflow

1. Confirm the IR version and shape (ADR-002) before changing the boundary.
2. Implement the boundary function: typed input, structured output, error mapping.
3. Add tests proving panics surface as catchable Python exceptions and errors carry fields.
4. Validate the maturin build locally; confirm no boundary regression.

## Anti-patterns

- Releasing the GIL or awaiting inside the compile call.
- Convention-based parameterization (string interpolation) instead of structural out-of-band
  values.
- Propagating raw panic messages, addresses, or paths to Python.
- Changing the IR contract shape without an ADR.
