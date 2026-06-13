# Command: Design Feature

Reusable prompt for producing an architecture-aligned design before any implementation.

## Use when

You are asked to design a new Ferrum feature or capability and need a reviewable design that
respects the contract and does not pre-empt open ADRs.

## Prompt

You are designing a feature for Ferrum (async Python ORM, Rust core, Pydantic v2, PostgreSQL).
Before proposing code:

1. **Ground in the contract.** Read the relevant parts of `.claude/docs/PRODUCT_REQUIREMENTS.md`
   and `.claude/docs/ARCHITECTURE.md`. State which requirement this serves.
2. **Place it on the boundary.** Decide what lives in Python (ergonomics, async, I/O,
   orchestration) vs Rust (pure compilation/hydration). Justify the split.
3. **Define the public surface.** Async API signatures, model/QuerySet impact, and the IR changes
   (if any). Keep identifiers/values out-of-band.
4. **State data flow and state ownership.** Where state lives; what crosses the boundary.
5. **Call out security gates.** SQL allowlist safety, credential handling, Tier A observability,
   error taxonomy, migration guards — whichever apply.
6. **Document alternatives + tradeoffs.** Use design lenses by name (Blast Radius, Schema
   Evolution, CAP, YAGNI, Least Astonishment).
7. **Check ADRs.** If the design depends on ADR-001..006, surface the dependency; do not hard-code
   an undecided choice.
8. **List the tests** the implementation must include.

## Output

A design note with: requirement link, boundary placement, public API, IR delta, data flow,
security requirements, alternatives + rationale, ADR dependencies, and required tests. **No
production code.**
