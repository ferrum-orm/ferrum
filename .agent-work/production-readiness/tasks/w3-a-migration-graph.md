---
task_id: w3-a-migration-graph
wave: wave-3
owner: production-readiness-executor
status: in_progress
run_id: 20260829T093132Z
shared_path_lease: null
dependencies:
  - w1-c-migration-safety
owned_paths:
  - python/ferrum/migrations/orchestrator.py
  - python/ferrum/migrations/loader.py
  - python/ferrum/migrations/base.py
  - python/ferrum/migrations/tokens.py
  - tests/python/unit/test_migration_graph.py
  - tests/python/integration/test_migration_graph.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: true
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: Migration graph applies DDL; destructive classification and dependency enforcement are security-gated
architecture_review: true
product_review: false
code_review: true
---

# Task: Migration graph and reversibility

## Specify

### Problem

There is no explicit migration dependency graph with deterministic topological
ordering, target upgrades/downgrades, checksum enforcement, status/history, or
recovery guidance. Reversible operations are incomplete. Data migration callables
lack explicit transaction policy. Offline SQL generation lacks checksums and
phase annotations.

### Scope

`python/ferrum/migrations/orchestrator.py` (graph logic — W1-C complete, ownership
transferred), `loader.py`, `base.py`, `tokens.py`, and owned tests. Implement
explicit dependencies, topological ordering, reversibility, data migrations,
and offline SQL.

### Non-goals

No `ledger.py` edits (shared path — need lease if required; avoid if possible).
No `operations.py` edits (W1-C owns, complete — import only). No `cli/` edits
(W2-F owns `check_schema_cmd.py`; `migrate_cmd.py` is W1-C — import only). No
autodiff (W3-C). No schema/shard coordination (W3-B). No `README.md` /
`CHANGELOG.md` (record bullets in log).

### Invariants and failure modes

Explicit dependencies with deterministic topological ordering. Target
upgrades/downgrades. Checksum enforcement. Status/history. Recovery guidance.
Reversible rename/type/default/nullability/constraint/index operations with
destructive/type-narrowing classification. Data migration callables with
transaction policy and no automatic source-code execution from untrusted files.
Offline SQL with checksums and phase annotations. Preserve W1-C advisory lock,
transactional DDL, and destructive confirm gates.

### Acceptance criteria

- Explicit dependencies between migrations with deterministic topological ordering.
- Target upgrades/downgrades (migrate to specific migration).
- Checksum enforcement; reject changed applied files (already in W1-C — preserve).
- Status/history and recovery guidance.
- Reversible rename/type/default/nullability/constraint/index operations.
- Destructive/type-narrowing classification preserved from W1-C.
- Data migration callables with explicit transaction policy.
- No automatic source-code execution from untrusted files.
- Offline SQL generation with checksums and phase annotations.
- Tests: dependency cycle detection, topological ordering, target upgrade/downgrade,
  recovery from partial failure, reversible operations, data migrations.

## Plan

Extend `orchestrator.py` with graph logic (topological sort, dependency
resolution, target upgrade/downgrade). Extend `loader.py` with dependency
parsing. Extend `base.py` with reversible operation support. Extend `tokens.py`
if needed for checksum/phase annotations. ChiefArchitect for the graph
architecture; SecurityEngineer for migration-apply DDL classification;
CodeReviewer required.

## Tasks

1. Audit existing orchestrator/loader/base and identify gaps vs the plan.
2. Add explicit dependencies with deterministic topological ordering.
3. Add target upgrade/downgrade (migrate to specific migration).
4. Add status/history and recovery guidance.
5. Add reversible rename/type/default/nullability/constraint/index operations.
6. Add data migration callables with explicit transaction policy.
7. Add offline SQL generation with checksums and phase annotations.
8. Tests: dependency cycles, topological ordering, target upgrade/downgrade,
   recovery, reversible operations, data migrations.
9. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T093132Z` with exclusive owned paths
and no shared-path lease. Implement the Tasks section. Preserve W1-C advisory
lock, transactional DDL, and destructive confirm gates.

## Validation contract

Focused migration graph unit tests plus live migration integration tests
against PostgreSQL, then `mise run ci-local`.

## Independent verification contract

Verifier proves topological ordering, target upgrade/downgrade, checksum
enforcement, recovery, reversibility, and data migration transaction policy.
Named gates: ChiefArchitect, SecurityEngineer, CodeReviewer. ProductManager
`not_required`.

## Revert contract

Revert only owned orchestrator/loader/base/tokens/test files from this run.
Preserve W1-C work and all other workstreams.
