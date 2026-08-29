---
task_id: pilot-ticket-analyzer
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
  - tests/consumer_contracts/test_ticket_analyzer_contracts.py
  - tests/consumer_contracts/manifest.py
  - tests/consumer_contracts/conftest.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: true
  schema_selection: false
security_review: true
security_review_justification: RLS tenant transactions and platform-admin bypass must not leak GUCs
architecture_review: true
product_review: true
code_review: true
---

# Task: Ticket Analyzer consumer pilot

## Specify

### Problem

The W0-B consumer contract tests for Ticket Analyzer were written as an inventory of
supported/defect/missing APIs. Three contract tests assert that APIs are NOT available
(`schema_transaction`, `ShardRouter`, `select_for_update`) — but these were implemented
in W1-F and W1-B. These stale "missing API" tests need to be retargeted to verify the
APIs now exist and work correctly against live PostgreSQL.

Additionally, the contract tests that previously marked items as "defect" or "missing"
need to be re-evaluated against the now-complete Ferrum implementation to confirm the
defects are fixed and missing APIs are implemented.

### Scope

Retarget and validate `tests/consumer_contracts/test_ticket_analyzer_contracts.py`,
`tests/consumer_contracts/manifest.py`, and `tests/consumer_contracts/conftest.py`
against the complete Ferrum implementation. Run all contract tests against live
PostgreSQL. Update the manifest to reflect current support status.

This pilot is scoped to IN-REPO contract validation only. Actual consumer codebase
migration (replacing SQLAlchemy in `ticket-analyzer-agent`) is EXTERNAL work requiring
access to the consumer repository and is out of scope for this tick.

### Non-goals

No external consumer codebase migration (requires access to `ticket-analyzer-agent`
repository). No `__init__.py` edits (shared path, no lease). No source code edits to
Ferrum implementation files — only test/contract files. No `README.md` / `CHANGELOG.md`
(record bullets in log).

### Invariants and failure modes

All consumer contract tests pass against live PostgreSQL. RLS tenant transactions and
platform-admin bypass verified with no GUC leakage. Inbox lease/CAS/SKIP LOCKED behavior
verified under concurrency. JSONB, vector, bulk, composite-key, aggregate, and streaming
parity verified. Platform tables represented by ordinary Ferrum models and typed relations,
without raw SQL. No `xfail` remains for items now implemented.

### Acceptance criteria

- All 3 stale "missing API" tests retargeted (schema_transaction, ShardRouter,
  select_for_update) to verify the APIs exist and work.
- All previously-defect items re-evaluated; defects fixed by implementation workstreams
  confirmed resolved.
- Manifest updated to reflect current support status (defects → supported, missing →
  supported).
- All contract tests pass against live PostgreSQL.
- RLS tenant/platform-admin GUC isolation verified.
- No `xfail` remains for implemented items.
- Consumer contract gate transitions from `ready` to `passed`.

## Plan

Audit existing contract tests and manifest. Retarget stale "missing API" tests. Re-evaluate
defect items. Run all tests against live PostgreSQL. ChiefArchitect for the contract
architecture; SecurityEngineer for RLS/GUC safety; ProductManager for consumer readiness
assessment; CodeReviewer required.

## Tasks

1. Audit `test_ticket_analyzer_contracts.py` and `manifest.py` for stale assertions.
2. Retarget the 3 "missing API" tests to verify APIs exist and work (schema_transaction,
   ShardRouter, select_for_update).
3. Re-evaluate all "defect" items against the complete implementation.
4. Update `manifest.py` to reflect current support status.
5. Run all contract tests against live PostgreSQL.
6. Verify RLS tenant/platform-admin GUC isolation (no leak on commit/rollback/pool reuse).
7. Verify inbox lease/CAS/SKIP LOCKED under concurrency.
8. Verify JSONB, vector, bulk, composite-key, aggregate, streaming parity.
9. Remove any `xfail` for items now implemented.
10. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T104025Z` with exclusive owned paths and no
shared-path lease. Only edit test/contract files — do NOT edit Ferrum source code.

## Validation contract

All consumer contract tests pass against live PostgreSQL, then `mise run ci-local`.

## Independent verification contract

Verifier proves all contract tests pass, stale tests retargeted, manifest accurate, RLS
GUC isolation verified. Named gates: ChiefArchitect, SecurityEngineer, ProductManager,
CodeReviewer — all required.

## Revert contract

Revert only owned contract/test files from this run. Preserve all implementation workstreams.
