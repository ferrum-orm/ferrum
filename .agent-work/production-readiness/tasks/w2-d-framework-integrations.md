---
task_id: w2-d-framework-integrations
wave: wave-2
owner: production-readiness-executor
status: in_progress
run_id: 20260829T091235Z
shared_path_lease: null
dependencies:
  - w1-e-pool-lifecycle
owned_paths:
  - python/ferrum/contrib/fastapi.py
  - python/ferrum/contrib/__init__.py
  - tests/python/unit/test_fastapi_contrib.py
  - tests/python/integration/test_fastapi_integration.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: true
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: FastAPI dependency injection and auth adapter must not leak credentials or expose raw connections
architecture_review: true
product_review: false
code_review: true
---

# Task: FastAPI and authentication integrations

## Specify

### Problem

The existing FastAPI lifespan/dependency integration needs hardening for one pool per
process and transaction-scoped request dependencies. An optional `fastapi-users` database
adapter may be needed if the Org AI parity inventory confirms continued use.

### Scope

`python/ferrum/contrib/fastapi.py`, `python/ferrum/contrib/__init__.py`, and owned
framework integration tests. Keep framework integrations in optional extras; core
Ferrum must not import FastAPI or fastapi-users.

### Non-goals

No core Ferrum changes. No `__init__.py` edits (shared path, no lease). No
QuerySet/Connection/Transaction changes (W1-E owns those, now complete — import only).
Do not add raw SQL escape hatches. Do not edit `README.md` / `CHANGELOG.md` (record
bullets in the log).

### Invariants and failure modes

One pool per process via lifespan. Transaction-scoped request dependencies via
`Depends`. Error translation maps Ferrum exceptions to HTTP responses without leaking
DSNs, bound values, or row data. Core Ferrum must not import FastAPI. Optional
`fastapi-users` adapter stays in contrib if implemented.

### Acceptance criteria

- Hardened FastAPI lifespan: one pool per process, clean shutdown via W1-E event-based
  drain.
- Transaction-scoped request dependencies via `Depends(get_ferrum_conn)`.
- Error translation: Ferrum exceptions → HTTP responses (integrity → 409, not found
  → 404, timeout → 503, etc.) without leaking secrets/DSNs/bound values.
- Optional `fastapi-users` adapter if parity inventory confirms use; cover user
  lookup/create/update/delete, unique conflicts, OAuth account relations.
- Core Ferrum does not import FastAPI or fastapi-users (import boundary test).
- Live integration tests with a real FastAPI app + PostgreSQL.

## Plan

Harden the existing `contrib/fastapi.py` lifespan and dependency injection. Add error
translation middleware. If fastapi-users is needed, add a contrib adapter. ChiefArchitect
for the integration architecture; SecurityEngineer for auth/credential handling;
CodeReviewer required.

## Tasks

1. Audit existing `contrib/fastapi.py` and identify gaps vs the plan.
2. Harden lifespan for one-pool-per-process with W1-E event-based shutdown.
3. Add transaction-scoped request dependencies (`Depends`).
4. Add Ferrum-to-HTTP error translation (no secret/DSN/row data leakage).
5. Optional: fastapi-users adapter if parity inventory confirms use.
6. Import boundary test proving core Ferrum never imports FastAPI.
7. Live integration tests with real FastAPI app + PostgreSQL.
8. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T091235Z` with exclusive owned paths and
no shared-path lease. Implement the Tasks section.

## Validation contract

Focused contrib unit tests plus live FastAPI integration tests against PostgreSQL,
then `mise run ci-local`. Record HTTP response bodies and status codes.

## Independent verification contract

Verifier proves one-pool-per-process, transaction-scoped dependencies, error translation
without secret leakage, and import boundary. Named gates: ChiefArchitect,
SecurityEngineer, CodeReviewer `decision: approved`. ProductManager `not_required`.

## Revert contract

Revert only owned contrib/test files from this run. Preserve all other workstreams.
