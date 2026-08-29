---
task_id: w1-f-tenancy-shards
wave: wave-1
owner: production-readiness-executor
status: ready
run_id: null
shared_path_lease: w1-f-shared-20260829T100000Z
dependencies:
  - w1-e-pool-lifecycle
owned_paths:
  - python/ferrum/session.py
  - python/ferrum/routing.py
  - tests/python/unit/test_session.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: true
  schema_selection: true
security_review: true
security_review_justification: admin GUCs, search_path schema selection, and shard routing are security-gated
architecture_review: true
product_review: false
code_review: true
---

# Task: RLS, platform administration, schema tenancy, and shards

## Specify

### Problem

§5a ratifies `platform_admin_transaction`, `schema_transaction`, and optional
`ConnectionRegistry`/`ShardRouter`. None exist. Only `tenant_transaction()`
GUC binding is shipped. Ticket Analyzer needs an admin path without a fake
tenant id and validated schema selection.

### Scope

`session.py`, new `routing.py`, and session tests. `python/ferrum/__init__.py`
exports require a coordinator lease at ready-time (not granted yet).

### Non-goals

No `platform_scoped` model flag. No dialect-switching Session. No implicit
QuerySet connection selection. Do not implement until W1-E verifies (pool
ownership of independently configured PostgreSQL pools). mysql/sqlite/mssql
extras stay out of this router.

### Invariants and failure modes

Tenant and admin GUCs are allowlisted and transaction-local; reset on
commit/rollback/pool reuse. Schema identifiers are strictly allowlisted, never
interpolated from untrusted input; `search_path` is transaction-local.
Shard keys are trusted caller/router values. QuerySet stays connection-explicit.
Cancellation must not leak GUC or search_path onto a pooled connection.
Cross-tenant/schema/shard leak tests are fail-closed.

### Acceptance criteria

- `platform_admin_transaction()` sets only allowlisted admin GUCs; no fake tenant.
- `schema_transaction(schema, ...)` validates the identifier and sets
  transaction-local `search_path`.
- Optional `ConnectionRegistry`/`ShardRouter` over independent PostgreSQL pools
  with bounded parallel startup/health/close and per-shard stats.
- Cross-tenant, cross-schema, cross-shard leak tests including cancellation
  and pool reuse.

## Plan

Extend the existing `set_config(..., true)` pattern. New routing module owns
pools; QuerySet remains unaware. Wait for W1-E so Connection/pool close and
stats exist. ChiefArchitect, SecurityEngineer, CodeReviewer required. Request
an `__init__.py` + README/CHANGELOG lease before public exports.

## Tasks

1. Wait for W1-E verified; coordinator grants `__init__.py` (+ docs) lease.
2. Add `platform_admin_transaction`.
3. Add `schema_transaction` with identifier allowlist.
4. Add ConnectionRegistry/ShardRouter.
5. Leak/cancellation/pool-reuse tests; `mise run ci-local`.

## Implement

Implementation begins only when the coordinator marks this task `ready`.

## Validation contract

Focused session/routing tests plus live RLS/schema leak tests, then
`mise run ci-local`.

## Independent verification contract

Verifier proves GUC/search_path reset on rollback and pool reuse, and
cross-tenant/schema/shard isolation. Named gates: ChiefArchitect,
SecurityEngineer, CodeReviewer. ProductManager `not_required`.

## Revert contract

Revert session.py, routing.py, tests, and leased export/docs hunks from this
workstream only.
