# Command: Review Code

Reusable prompt for reviewing a Ferrum change against the contract and security gates.

## Use when

Reviewing a diff/PR for Ferrum before it merges.

## Prompt

Review this change as a senior Ferrum engineer. Check, in order:

1. **Contract fit.** Does it serve approved scope and respect `AGENTS.md` and
   named reviews? Does it contradict a resolved ADR or pre-empt reopened ADR-004
   or a new ADR? If so, request changes.
2. **Boundary discipline.** Python stays async/ergonomic/I/O; Rust stays pure/sync/stateless. No
   SQL strings in Python; no async/I/O/mutable-state in Rust.
3. **SQL safety.** No user input in identifier/value positions; identifiers allowlisted; values
   bound; bad fields/operators/sort directions fail before SQL emission.
4. **Secrets & observability.** No credentials/DSNs/bound values/row data in errors, logs, hooks,
   or migration output. Default hook payloads are Tier A only; Tier B/C are explicit opt-ins.
5. **Errors.** Mapped to the sanitized taxonomy; actionable without reading source; PyO3 panics
   catchable.
6. **Migrations (if touched).** Dry-run mandatory; destructive/non-dev confirmation; unscoped
   delete/update behind danger APIs.
7. **Tests.** New behavior + security paths covered; regression test for any bug fix.
8. **Docs.** Public API change reflected in README/docs in the same change.
9. **Diff hygiene.** Minimal, scoped; lint/format/type clean (Python); `cargo check`/`clippy`
   clean (Rust).

Cite design lenses by name where relevant (Blast Radius, Schema Evolution, Least Astonishment).

## Output

A review verdict (approve / request changes) with specific, actionable findings.
Require SecurityEngineer approval for SQL compilation, migration apply,
errors/redaction, auth/secrets, RLS/admin GUCs, or schema selection.
