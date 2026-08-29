---
task_id: w3-c-autodiff
wave: wave-3
owner: production-readiness-executor
status: in_progress
run_id: 20260829T102316Z
shared_path_lease: null
dependencies:
  - w1-c-migration-safety
  - w3-a-migration-graph
owned_paths:
  - python/ferrum/migrations/autodiff.py
  - tests/python/unit/test_autodiff.py
  - tests/python/integration/test_autodiff.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: Autodiff generates migration ops; destructive/type-narrowing classification must be correct
architecture_review: true
product_review: false
code_review: true
---

# Task: Autodiff quality

## Specify

### Problem

Autodiff is incomplete — it does not handle types, renames (needs explicit hints), constraints,
relations, indexes, extensions, RLS, and functions. Ambiguous renames or destructive
conversions must not be guessed; developer intent is required. Schema-state replay and
round-trip tests from empty schema through upgrade/revert are missing.

### Scope

New `python/ferrum/migrations/autodiff.py`, owned autodiff unit and integration tests. Import
from `orchestrator.py` (W3-A owns, complete), `operations.py` (W1-C owns, complete),
`base.py` (W3-A owns, complete) — do NOT modify any of them.

### Non-goals

No `orchestrator.py` / `operations.py` / `ledger.py` / `base.py` / `tokens.py` / `loader.py`
edits (W1-C/W3-A own, complete — import only). No `cli/` edits (W1-C/W2-F own — import only).
No `__init__.py` edits (shared path, no lease). No `README.md` / `CHANGELOG.md` (record
bullets in log). No migration graph (W3-A). No schema/shard coordination (W3-B).

### Invariants and failure modes

Autodiff extends for types, renames via explicit hints, constraints, relations, indexes,
extensions, RLS, and functions. Never guess ambiguous renames or destructive conversions;
require developer intent. Schema-state replay and round-trip tests from empty schema through
upgrade/revert.

### Acceptance criteria

- Extend autodiff for: types, renames (explicit hints), constraints, relations, indexes,
  extensions, RLS, functions.
- Never guess ambiguous renames or destructive conversions; require developer intent.
- Destructive/type-narrowing classification correct (consistent with W1-C).
- Schema-state replay and round-trip tests from empty schema through upgrade/revert.
- Tests: type changes, rename detection (with/without hint), constraint add/drop,
  relation changes, index changes, extension add/drop, RLS policy changes, function changes,
  destructive conversion rejection, schema-state replay.

## Plan

Create `migrations/autodiff.py` that extends the existing autodiff (currently in
`orchestrator.py`) as a separate module. Import `SchemaState`, `MigrationGraph`, and
operation classes from the existing modules. ChiefArchitect for the autodiff architecture;
SecurityEngineer for destructive classification correctness; CodeReviewer required.

## Tasks

1. Audit existing autodiff in orchestrator.py and identify gaps vs the plan.
2. Create `migrations/autodiff.py` with extended autodiff logic.
3. Add type change detection (safe vs destructive).
4. Add rename detection with explicit hints (never guess).
5. Add constraint, relation, index, extension, RLS, function change detection.
6. Add destructive/type-narrowing classification (consistent with W1-C).
7. Add schema-state replay and round-trip tests.
8. Tests: type changes, rename detection, constraint/relation/index/extension/RLS/function
   changes, destructive rejection, schema-state replay.
9. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T102316Z` with exclusive owned paths and no
shared-path lease. Import from orchestrator.py/operations.py/base.py — do NOT modify them.

## Validation contract

Focused autodiff unit tests plus live PostgreSQL schema-state replay integration tests,
then `mise run ci-local`.

## Independent verification contract

Verifier proves autodiff detection accuracy, destructive classification correctness, rename
hint requirement, and schema-state replay round-trip. Named gates: ChiefArchitect,
SecurityEngineer, CodeReviewer. ProductManager `not_required`.

## Revert contract

Revert only owned autodiff/test files from this run. Preserve all other workstreams.
