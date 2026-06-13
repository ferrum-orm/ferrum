# Rule: PyO3 Boundary

> Constraints for the Rust↔Python boundary. The boundary is a typed, versioned, serializable
> contract. See `AGENTS.md` §4 and ADR-002 / ADR-003 / ADR-006.

## The contract

- The IR crossing the boundary is **typed, versioned, and serializable**. Values are carried
  **out-of-band from identifiers** so parameterization and allowlisting are structural, not
  conventional.
- The IR contract shape and version stability are governed by **ADR-002** — do not change the
  contract shape unilaterally; surface it as an ADR question.

## Execution model

- The Rust compile call is **synchronous and holds the GIL**. Compilation is CPU-bound and
  sub-millisecond. Do **not** release/reacquire the GIL for it, and do **not** put cancellable
  waiting inside Rust.
- All cancellation, timeout, and retry handling lives in **Python at the driver await point**.

## Error and panic mapping

- Build the extension with `panic = "unwind"`. Wrap the boundary so a Rust panic surfaces as a
  **catchable Python exception**, never a process abort.
- Error payloads carry **structured fields** (model, field, operator, category). Never leak
  formatted trace blobs, memory addresses, or local filesystem paths to Python.
- Mapping to Ferrum's sanitized exception taxonomy is centralized (**ADR-006**); do not scatter
  ad-hoc error translation.

## Hydration

- Rust constructs typed payloads from **trusted DB-origin rows**.
- Default hydration uses the Pydantic v2 **construct-without-revalidate** fast path (the DB
  already enforced types). The trusted-source assumption and the custom-validator caveat are
  governed by **ADR-003** — document the assumption wherever hydration is wired.

## Packaging

- The bridge is **PyO3 + maturin**. Packaging targets and the CI wheel matrix (abi3,
  cibuildwheel) are governed by **ADR-005** — do not hard-code a matrix that pre-empts it.

## Definition of done (boundary)

- [ ] IR remains typed/versioned/serializable; identifiers and values stay out-of-band.
- [ ] Synchronous, GIL-holding compile; no cancellable waiting in Rust.
- [ ] Panics and errors are catchable, structured, and sanitized.
- [ ] Any contract/hydration/packaging change that touches ADR-002/003/005 is surfaced, not assumed.
