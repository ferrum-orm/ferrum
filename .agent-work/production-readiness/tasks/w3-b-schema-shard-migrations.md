---
task_id: w3-b-schema-shard-migrations
wave: wave-3
owner: production-readiness-executor
status: in_progress
run_id: 20260829T095632Z
shared_path_lease: null
dependencies:
  - w1-f-tenancy-shards
  - w3-a-migration-graph
owned_paths:
  - python/ferrum/routing.py
  - python/ferrum/migrations/coordinator.py
  - tests/python/unit/test_schema_shard_migrations.py
  - tests/python/integration/test_schema_shard_migrations.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: true
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: true
security_review: true
security_review_justification: Cross-shard migration coordination and schema selection are security-gated
architecture_review: true
product_review: false
code_review: true
---

# Task: Schema/shard migration coordinator

## Specify

### Problem

There is no coordinator to apply one migration graph across selected schemas/shards
with per-target advisory locks, bounded concurrency, resumable status, and
fail-fast/continue policy. There is no canary-target support or structured progress
hooks. Cross-shard atomicity must not be promised.

### Scope

`python/ferrum/routing.py` (ShardRouter integration for migration coordination), new
`python/ferrum/migrations/coordinator.py` (schema/shard migration coordinator), and
owned tests. Import from `orchestrator.py` (W3-A owns, complete) — do NOT modify it.

### Non-goals

No `orchestrator.py` edits (W3-A owns, complete — import only). No `operations.py` /
`ledger.py` edits (W1-C owns, complete — import only). No `session.py` edits (W1-F owns,
complete — import only). No `__init__.py` edits (shared path, no lease). No autodiff
(W3-C). No `README.md` / `CHANGELOG.md` (record bullets in log).

### Invariants and failure modes

Apply one migration graph across selected schemas/shards. Per-target advisory locks.
Bounded concurrency. Resumable status. Fail-fast/continue policy. Never promise
cross-shard atomicity. Report partial rollout precisely. Make reruns idempotent.
Canary-target support. Structured progress hooks.

### Acceptance criteria

- Apply one migration graph across selected schemas/shards.
- Per-target advisory locks (reuse W1-C advisory lock pattern).
- Bounded concurrency (configurable parallelism).
- Resumable status (track per-target migration state).
- Fail-fast/continue policy (configurable).
- Never promise cross-shard atomicity; report partial rollout precisely.
- Make reruns idempotent.
- Canary-target support.
- Structured progress hooks.
- Tests: multi-shard migration, partial failure, rerun/resume, canary, concurrency.

## Plan

Create `migrations/coordinator.py` that uses `MigrationGraph` (W3-A) and `ShardRouter`
(W1-F) to coordinate migrations across shards. ChiefArchitect for the coordination
architecture; SecurityEngineer for cross-shard migration safety; CodeReviewer required.

## Tasks

1. Audit existing `routing.py` (ShardRouter) and `orchestrator.py` (MigrationGraph) APIs.
2. Create `migrations/coordinator.py` with schema/shard migration coordinator.
3. Add per-target advisory locks (reuse W1-C pattern).
4. Add bounded concurrency (configurable parallelism).
5. Add resumable status (per-target migration state tracking).
6. Add fail-fast/continue policy.
7. Add canary-target support.
8. Add structured progress hooks.
9. Ensure reruns are idempotent; report partial rollout precisely.
10. Tests: multi-shard, partial failure, rerun/resume, canary, concurrency.
11. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T095632Z` with exclusive owned paths and
no shared-path lease. Import from `orchestrator.py` and `routing.py` — do NOT modify them.

## Validation contract

Focused coordinator unit tests plus live multi-shard integration tests against
PostgreSQL, then `mise run ci-local`.

## Independent verification contract

Verifier proves per-target advisory locks, bounded concurrency, resumable status,
partial failure reporting, idempotent reruns, and canary support. Named gates:
ChiefArchitect, SecurityEngineer, CodeReviewer. ProductManager `not_required`.

## Revert contract

Revert only owned routing/coordinator/test files from this run. Preserve all other
workstreams.
