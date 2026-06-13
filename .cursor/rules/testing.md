# Rule: Testing

> **No ORM feature ships without tests.** A feature without tests is not done. See `AGENTS.md` §2,
> §3, §7, §8.

## Hard constraints

- **New behavior → new tests** in the same diff. **Bug fix → regression test** that fails before
  and passes after.
- **Security paths are test-covered** (release-qualification gates):
  - User input is never interpolated into SQL — bad fields/operators/sort directions fail with
    structured errors **before** SQL emission. Test the rejection paths.
  - No credentials, DSNs, bound values, or row data appear in default hook payloads, errors,
    logs, or migration output. Test the redaction.
  - Default observability is **Tier A only**; Tier B/C require explicit Ferrum opt-in and never
    activate from `DEBUG=1`. Test that defaults stay Tier A.
  - PyO3 panics surface as catchable Python exceptions. Test that they are catchable.
  - Migrations: dry-run mandatory; destructive/non-dev applies require confirmation; unscoped
    `delete()`/`update()` require the named danger API. Test the guards.

## Test layering

- **Rust unit tests** live with the crate: compilation correctness, allowlist rejection,
  parameter binding, hydration shape.
- **Python unit tests:** model definition → metadata, QuerySet → IR, error taxonomy mapping,
  hook payload redaction, async cancellation/timeout behavior.
- **Integration tests** against a real PostgreSQL (the only supported DB): end-to-end query,
  transaction, and migration dry-run/apply flows.

## Conventions

- Tests are deterministic; no reliance on wall-clock timing or external network beyond the
  provisioned PostgreSQL.
- Prefer the smallest verification that proves the change; run touched-area tests over full-repo
  runs unless scope warrants.
- Async tests use the configured async test harness; assert cancellation and timeout semantics
  explicitly where relevant.

## Definition of done (testing)

- [ ] Behavior and security-relevant paths are covered.
- [ ] Regression test added for any bug fix.
- [ ] Tests are deterministic and run green locally on touched scope.
