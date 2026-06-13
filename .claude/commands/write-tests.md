# Command: Write Tests

Reusable prompt for adding the tests a Ferrum change requires. No feature ships without tests.

## Use when

Adding test coverage for new behavior, a bug fix, or an under-tested security path.

## Prompt

Write tests for this Ferrum change. Cover, as applicable:

1. **Behavior.** Happy path and meaningful edge cases for the new/changed behavior.
2. **Security gates (test these explicitly):**
   - Unknown fields / unsupported operators / invalid sort directions fail with structured errors
     **before** SQL emission.
   - No credentials, DSNs, bound values, or row data appear in errors, logs, default hook
     payloads, or migration output.
   - Default observability is Tier A only; Tier B/C do not activate from `DEBUG=1`.
   - PyO3 panics surface as catchable Python exceptions.
   - Migration guards: dry-run required; destructive/non-dev confirmation; unscoped
     delete/update behind danger APIs.
3. **Layering.** Rust unit tests (compilation, allowlist rejection, binding, hydration) live with
   the crate; Python unit tests (metadata, QuerySet→IR, error mapping, async cancellation/timeout);
   integration tests against real PostgreSQL for end-to-end flows.
4. **Determinism.** No wall-clock flakiness or external network beyond provisioned PostgreSQL.
5. **Regression.** For a bug fix, add a test that fails before the fix and passes after.

## Output

Deterministic tests at the right layer that fail meaningfully without the change and pass with it,
runnable on the smallest sufficient scope.
