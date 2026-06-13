# Command: Implement Feature

Reusable prompt for implementing a feature against an approved design, with tests and docs.

## Use when

A design exists (or the change is small and unambiguous) and you are ready to write code.

## Prompt

You are implementing a Ferrum feature. Implementation must not proceed past an undecided ADR or
bypass architecture review.

1. **Confirm approval.** There is an approved design/architecture for this area, or the change is
   trivially scoped. If not, stop and flag it.
2. **Respect the boundary.** Python: async, typed, Pydantic-v2-native, no SQL strings, no sync
   API. Rust: pure, synchronous, stateless, allowlist-validated, no I/O.
3. **Keep the diff minimal.** Smallest change that satisfies the task and its tests; do not
   restyle working code.
4. **Safety first.** No user input in SQL positions; values bound only; identifiers allowlisted;
   errors sanitized (no values/DSNs/secrets/row data); hook payloads stay Tier A.
5. **Tests in the same diff.** Cover new behavior and security-relevant paths; add a regression
   test for any bug fixed.
6. **Docs in the same diff.** Update README/docs for any public API change.
7. **Verify the smallest sufficient scope.** Touched-file lint/format/type (Python) and
   `cargo check`/`clippy` (Rust); run the relevant tests.

## Output

A minimal, tested, documented change that honors every constraint in `AGENTS.md` §2 and the
security gates in §3, with a short summary of what changed and what was verified.
