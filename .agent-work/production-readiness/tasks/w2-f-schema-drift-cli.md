---
task_id: w2-f-schema-drift-cli
wave: wave-2
owner: production-readiness-executor
status: in_progress
run_id: 20260829T091235Z
shared_path_lease: null
dependencies:
  - w1-c-migration-safety
owned_paths:
  - python/ferrum/migrations/drift.py
  - python/ferrum/cli/check_schema_cmd.py
  - python/ferrum/cli/app.py
  - tests/python/unit/test_schema_drift.py
  - tests/python/integration/test_schema_drift.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: false
security_review_justification: Schema drift detection is read-only introspection; no SQL compilation, migration apply, or credential surfaces
architecture_review: true
product_review: false
code_review: true
---

# Task: Schema drift and compatibility CLI

## Specify

### Problem

There is no `ferrum check-schema` command that detects model/live-schema drift. The
existing `drift.py` module needs hardening to compare columns, types, constraints,
indexes, extensions, RLS policies, and functions with machine-readable output.

### Scope

`python/ferrum/migrations/drift.py`, new CLI command `check_schema_cmd.py`, CLI
registration in `app.py`, and owned tests. Support explicit unmanaged table/schema
exclusions for Better Auth, LangGraph, and Alembic-owned objects.

### Non-goals

No `orchestrator.py` / `ledger.py` / `operations.py` edits (W1-C owns those, now
complete — import only). No `README.md` / `CHANGELOG.md` edits (record bullets in
log). No autodiff (W3-C). No migration graph (W3-A).

### Invariants and failure modes

`ferrum check-schema` returns non-zero exit on drift. Machine-readable output (JSON)
for CI integration. Explicit unmanaged exclusions for third-party tables. Read-only
introspection — no DDL, no migration apply, no credential surfaces.

### Acceptance criteria

- `ferrum check-schema` with non-zero exit on model/live-schema drift.
- Compare columns, exact PostgreSQL types, nullability/defaults, PK order, unique/FK/
  check constraints, indexes/opclasses/predicates, extensions, RLS policies,
  functions, and vector dimensions.
- Machine-readable JSON output for CI.
- Explicit unmanaged table/schema exclusions (Better Auth, LangGraph, Alembic-owned).
- Alembic coexistence: Alembic remains authoritative, Ferrum checks drift.
- Live-schema drift tests against PostgreSQL.

## Plan

Harden `drift.py` with comprehensive comparison. Add `check_schema_cmd.py` CLI
command. Register in `app.py`. ChiefArchitect for the drift detection architecture;
CodeReviewer required. SecurityEngineer not required (read-only introspection).

## Tasks

1. Audit existing `drift.py` and identify gaps vs the plan.
2. Add comprehensive comparison: columns, types, nullability, defaults, PK order,
   constraints, indexes, extensions, RLS policies, functions, vector dimensions.
3. Add machine-readable JSON output mode.
4. Add explicit unmanaged table/schema exclusions.
5. Create `check_schema_cmd.py` CLI command and register in `app.py`.
6. Live-schema drift tests against PostgreSQL.
7. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T091235Z` with exclusive owned paths and
no shared-path lease. Implement the Tasks section.

## Validation contract

Focused drift unit tests plus live schema-drift integration tests against PostgreSQL,
then `mise run ci-local`. Record JSON output and exit codes.

## Independent verification contract

Verifier proves drift detection accuracy, JSON output, exclusions, and non-zero exit
on drift. Named gates: ChiefArchitect, CodeReviewer `decision: approved`.
SecurityEngineer `not_required`. ProductManager `not_required`.

## Revert contract

Revert only owned drift/CLI/test files from this run. Preserve all other workstreams.
