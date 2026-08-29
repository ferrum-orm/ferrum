---
task_id: w1-d-error-taxonomy
wave: wave-1
owner: production-readiness-executor
status: in_progress
run_id: 20260821T093500Z
shared_path_lease: null
dependencies: []
owned_paths:
  - python/ferrum/errors.py
  - python/ferrum/hooks.py
  - crates/ferrum-pyo3/
  - tests/python/unit/test_errors.py
  - tests/python/unit/test_hooks.py
  - tests/python/unit/test_boundary.py
  - tests/python/security/test_credential_safety.py
  - tests/python/integration/test_hooks_integration.py
  - tests/python/integration/test_constraints.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: true
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: exception fields and hook payloads must not leak DETAIL/HINT/secrets/row data
architecture_review: true
product_review: false
code_review: true
---

# Task: Error taxonomy and diagnostics

## Specify

### Problem

Ratified §5a requires structured `sqlstate` and `category` on every mapped
Ferrum exception. Today SQLSTATE appears only as migration message text;
`map_db_error` otherwise folds to `type(exc).__name__`. Retry and Tier-A hooks
cannot consume a stable taxonomy. DETAIL/HINT/bound values/row data/DSNs must
never appear in any field or default hook payload.

### Scope

`errors.py`, PyO3 panic/error mapping, Tier-A hook category fields, and owned
unit/security/integration tests.

### Non-goals

No retry-policy rewrite (W1-B consumes this taxonomy later). No pool stats
(W1-E). Do not edit `README.md` / `CHANGELOG.md` / `__init__.py` (exceptions
are already exported; record changelog bullets in the log). Do not add DETAIL
as an opt-in field.

### Invariants and failure modes

Sanctioned safe fields only: `sqlstate`, `category`, `constraint`,
`model`/`operation`. Closed category enum. PyO3 panics stay catchable.
Concurrent mapping must not log secrets. Failover/admin-shutdown and pool
exhaustion must not leak DSNs. Original Ferrum exception remains the public
boundary; chaining policy stays safe.

### Acceptance criteria

- Every mapped exception class has structured `sqlstate` and `category`.
- Map integrity, schema, serialization, deadlock, lock timeout, query
  cancellation, pool exhaustion, failover/admin shutdown, invalid transaction
  state, and connection classes.
- Tier-A hooks receive category without bound values.
- Security tests with secrets and row data in PostgreSQL DETAIL/HINT prove they
  never escape messages, attributes, or default hooks.

## Plan

Promote `sqlstate`/`category` onto the mapped exception types; keep
`map_db_error` / `map_native_error` as the single redaction boundary; thread
category into Tier-A payloads. Implement the already-ratified §5a field set —
do not invent extra fields. ChiefArchitect confirms the closed category enum
matches §5a. SecurityEngineer and CodeReviewer required.

## Tasks

1. Inventory current exception classes and mapping branches.
2. Add structured `sqlstate`/`category` (closed enum) to every mapped class.
3. Map the required PostgreSQL/driver classes; preserve Ferrum public types.
4. Feed category into Tier-A hooks; prove bound values absent.
5. Security tests with DETAIL/HINT containing secrets and row data.
6. Focused tests plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260821T093500Z` with exclusive owned
paths and no shared-path lease. Implement the Tasks section.

## Validation contract

Focused unit/security/hooks tests plus live constraint-error mapping against
PostgreSQL, then `mise run ci-local`. Record exception `repr`/attributes (not
just pytest pass).

## Independent verification contract

Verifier raises mapped errors with planted DETAIL/HINT secrets and confirms
they are absent from every field, message, and default hook. Named gates:
ChiefArchitect, SecurityEngineer, CodeReviewer `decision: approved`.
ProductManager `not_required`.

## Revert contract

Revert only owned errors/hooks/pyo3/test files from this run. Preserve W1-A/C
edits.
