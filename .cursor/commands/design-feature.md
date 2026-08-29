# Command: Design Feature

Reusable prompt for producing an architecture-aligned design before any implementation.

## Use when

You are asked to design a new Ferrum feature or capability and need a reviewable design that
respects resolved contracts and does not pre-empt reopened or new ADRs.

## Prompt

You are designing a feature for Ferrum (async Python ORM, Rust core, Pydantic v2, PostgreSQL).
Before proposing code:

1. **Ground in the contract.** Read `AGENTS.md`, `README.md`, `CHANGELOG.md`,
   relevant source/tests, and the approved plan. Do not require absent documents.
2. **Place it on the boundary.** Decide what lives in Python (ergonomics, async, I/O,
   orchestration) vs Rust (pure compilation/hydration). Justify the split.
3. **Define the public surface.** Async API signatures, model/QuerySet impact, and the IR changes
   (if any). Keep identifiers/values out-of-band.
4. **State data flow and state ownership.** Where state lives; what crosses the boundary.
5. **Call out security gates.** SQL allowlist safety, credential handling, Tier A observability,
   error taxonomy, migration guards — whichever apply.
6. **Document alternatives + tradeoffs.** Use design lenses by name (Blast Radius, Schema
   Evolution, CAP, YAGNI, Least Astonishment).
7. **Check ADRs.** Honor resolved ADRs; do not pre-empt reopened ADR-004 or a new
   ADR without ChiefArchitect ratification.
8. **List the tests** the implementation must include.

## Output

A design note with: requirement link, boundary placement, public API, IR delta, data flow,
security requirements, alternatives + rationale, ADR dependencies, and required tests. **No
production code.**
