---
task_id: pilot-org-ai-platform
wave: consumer-pilots
owner: production-readiness-executor
status: in_progress
run_id: 20260829T104025Z
shared_path_lease: null
dependencies:
  - w2-a-field-codecs
  - w2-b-query-expressiveness
  - w2-c-relations-bulk
  - w2-d-framework-integrations
  - w2-e-extension-lifecycle
  - w2-f-schema-drift-cli
  - w4-a-observability
  - w4-d-packaging-release
owned_paths:
  - tests/consumer_contracts/test_org_ai_platform_contracts.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: true
security_review: true
security_review_justification: Schema selection and shard routing must not leak across tenants
architecture_review: true
product_review: true
code_review: true
---

# Task: Org AI Platform consumer pilot

## Specify

### Problem

The W0-B consumer contract tests for Org AI Platform include stale "missing API" tests
that assert `schema_transaction`, `ShardRouter`, and `select_for_update` do NOT exist —
but these were implemented in W1-F and W1-B. These need to be retargeted to verify the
APIs exist and work correctly. Additionally, contract tests for encrypted/JSON codecs,
schema-per-tenant, and shard routing need validation against the now-complete
implementation (W2-A field codecs, W1-F tenancy/shards).

### Scope

Retarget and validate `tests/consumer_contracts/test_org_ai_platform_contracts.py`
against the complete Ferrum implementation. Run all contract tests against live
PostgreSQL.

This pilot is scoped to IN-REPO contract validation only. Actual consumer codebase
migration (refactoring Org AI Platform to async Ferrum) is EXTERNAL work requiring access
to the consumer repository and is out of scope for this tick.

### Non-goals

No external consumer codebase migration (requires access to Org AI Platform repository).
No `__init__.py` edits (shared path, no lease). No source code edits to Ferrum
implementation files — only test/contract files. No `README.md` / `CHANGELOG.md`
(record bullets in log). No `manifest.py` edits (pilot-ticket-analyzer owns it).

### Invariants and failure modes

All consumer contract tests pass against live PostgreSQL. Schema selection validated
(transaction-local search_path, no leak). Shard routing validated (trusted keys,
connection-explicit). Encrypted/JSON codecs validated (key-provider injection, PII
redaction). Row locks (SKIP LOCKED) validated. Relations and auth adapter validated.
No `xfail` remains for items now implemented.

### Acceptance criteria

- All 3 stale "missing API" tests retargeted (schema_transaction, ShardRouter,
  select_for_update) to verify the APIs exist and work.
- Schema-per-tenant contract validated (schema_transaction with allowlist + search_path).
- Shard routing contract validated (ConnectionRegistry/ShardRouter with trusted keys).
- Encrypted/JSON codec contracts validated (key-provider injection, PII redaction).
- Row lock contracts validated (select_for_update with nowait/skip_locked).
- All contract tests pass against live PostgreSQL.
- No `xfail` remains for implemented items.

## Plan

Audit existing contract tests. Retarget stale "missing API" tests. Add validation tests
for schema_transaction, ShardRouter, encrypted codecs, select_for_update. Run all tests
against live PostgreSQL. ChiefArchitect for the contract architecture; SecurityEngineer
for schema selection/shard routing safety; ProductManager for consumer readiness
assessment; CodeReviewer required.

## Tasks

1. Audit `test_org_ai_platform_contracts.py` for stale assertions.
2. Retarget the 3 "missing API" tests to verify APIs exist and work.
3. Add validation tests for schema_transaction (allowlist, search_path, reset).
4. Add validation tests for ShardRouter (trusted keys, connection-explicit).
5. Add validation tests for encrypted/JSON codecs (key-provider, PII redaction).
6. Add validation tests for select_for_update (nowait, skip_locked).
7. Run all contract tests against live PostgreSQL.
8. Remove any `xfail` for items now implemented.
9. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T104025Z` with exclusive owned paths and no
shared-path lease. Only edit test/contract files — do NOT edit Ferrum source code. Do NOT
edit `manifest.py` or `conftest.py` (pilot-ticket-analyzer owns them).

## Validation contract

All consumer contract tests pass against live PostgreSQL, then `mise run ci-local`.

## Independent verification contract

Verifier proves all contract tests pass, stale tests retargeted, schema/shard/codec
validation verified. Named gates: ChiefArchitect, SecurityEngineer, ProductManager,
CodeReviewer — all required.

## Revert contract

Revert only owned test files from this run. Preserve all implementation workstreams.
