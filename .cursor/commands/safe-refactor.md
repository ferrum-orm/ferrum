# Command: Safe Refactor

Reusable prompt for refactoring Ferrum without changing behavior or breaking the contract.

## Use when

Improving structure, readability, or performance with **no intended behavior change**.

## Prompt

Refactor this Ferrum code safely. Constraints:

1. **Behavior-preserving.** No change to public API behavior, error taxonomy, or observable
   semantics. If behavior must change, this is not a refactor — switch to design/implement.
2. **Characterize first.** Ensure existing tests cover the behavior you're about to move. If
   coverage is thin, add characterization tests **before** refactoring.
3. **Incremental (Strangler Fig).** Prefer small, reversible steps and wrap-then-replace over
   big-bang rewrites. Keep each step green.
4. **Respect the boundary.** Do not relocate logic across the Python/Rust boundary as part of a
   "cleanup". Python stays async/ergonomic/I/O; Rust stays pure/sync/stateless.
5. **Hold the invariants.** No new SQL strings in Python, no async/I/O/mutable-state in Rust, no
   loosened allowlist or redaction, no new ADR pre-emption.
6. **Minimal diff.** Do not restyle unrelated code. Keep the change reviewable.
7. **Verify.** Tests green before and after; lint/format/type (Python) and `cargo check`/`clippy`
   (Rust) clean on touched code.

## Output

A behavior-preserving, minimal, reviewable refactor with tests proving equivalence and a short
note on what moved and why.
