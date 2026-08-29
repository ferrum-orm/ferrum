---
task_id: w2-c-relations-bulk
wave: wave-2
owner: production-readiness-executor
status: in_progress
run_id: 20260829T102316Z
shared_path_lease: null
dependencies:
  - w1-a-query-correctness
  - w2-a-field-codecs
  - w2-b-query-expressiveness
owned_paths:
  - python/ferrum/relations.py
  - tests/python/unit/test_relations.py
  - tests/python/unit/test_bulk_operations.py
  - tests/python/integration/test_relations_bulk.py
security_triage_complete: true
security_surfaces:
  sql_compilation: true
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: Relation JOIN SQL and bulk write SQL must use allowlisted identifiers and bound values
architecture_review: true
product_review: false
code_review: true
---

# Task: Relationship and bulk behavior

## Specify

### Problem

Reverse FK/one-to-one/many-to-many loading, nested `select_related`/`prefetch_related`,
through models, ordering, and bounded batching are incomplete. Bulk
create/upsert/update/delete need hardening for composite keys, per-row values, conflict
predicates, returning, batch sizing, and PostgreSQL parameter limits. Cascade behavior
must be documented as database-driven; no SQLAlchemy unit-of-work cascades.

### Scope

`python/ferrum/relations.py`, owned relation/bulk unit and integration tests. Import from
`queryset.py` (W1-A/W2-B own, complete) — do NOT modify queryset.py. Import from `models.py`
(W2-A owns, complete) — do NOT modify models.py.

### Non-goals

No `queryset.py` edits (W1-A/W2-B own, complete — import only). No `models.py` edits (W2-A
owns, complete — import only). No `connection.py` / `runtime.py` edits (W1-B/E own, complete
— import only). No `__init__.py` edits (shared path, no lease). No `README.md` /
`CHANGELOG.md` (record bullets in log). No `hooks.py` / `observability.py` edits (W4-A owns,
complete). No new IR nodes (ChiefArchitect gate — stop if needed).

### Invariants and failure modes

Relation access stays explicit: unloaded relations raise rather than performing hidden async
I/O. Reverse FK/OTO may return an unbound QuerySet filtered by FK with NO I/O; a later terminal
still requires an explicit ConnectionLike. Bulk operations handle composite keys, per-row
values, conflict predicates, returning, batch sizing, and PostgreSQL parameter limits.
Cascade behavior is database-driven; no SQLAlchemy unit-of-work cascade emulation.

### Acceptance criteria

- Finish reverse FK/one-to-one/many-to-many loading.
- Nested `select_related`/`prefetch_related` with through models, ordering, bounded batching.
- Relation access stays explicit (unloaded raises, no hidden I/O).
- Harden bulk create/upsert/update/delete for composite keys, per-row values, conflict
  predicates, returning, batch sizing, PostgreSQL parameter limits.
- Document cascade behavior as database-driven; no unit-of-work cascades.
- Benchmark memory and latency for Ticket Analyzer backfills and Org AI batch workloads.

## Plan

Extend `relations.py` with reverse FK/OTO/M2M loading, nested prefetch, through models.
Harden bulk operations in `relations.py` (or test-level if bulk logic is in queryset.py —
import only). ChiefArchitect for the relation/bulk architecture; SecurityEngineer for JOIN
SQL safety; CodeReviewer required.

## Tasks

1. Audit existing `relations.py` and identify gaps vs the plan.
2. Finish reverse FK/one-to-one/many-to-many loading.
3. Add nested `select_related`/`prefetch_related` with through models, ordering, batching.
4. Keep relation access explicit (unloaded raises, no hidden I/O).
5. Harden bulk create/upsert/update/delete for composite keys, per-row values, conflict
   predicates, returning, batch sizing, PostgreSQL parameter limits.
6. Document cascade behavior as database-driven.
7. Benchmark memory and latency for backfill/batch workloads.
8. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T102316Z` with exclusive owned paths and no
shared-path lease. Import from queryset.py and models.py — do NOT modify them.

## Validation contract

Focused relation/bulk unit tests plus live PostgreSQL integration tests, then
`mise run ci-local`.

## Independent verification contract

Verifier proves reverse relation loading, nested prefetch, bulk composite-key handling,
batch sizing, and explicit relation access (no hidden I/O). Named gates: ChiefArchitect,
SecurityEngineer, CodeReviewer. ProductManager `not_required`.

## Revert contract

Revert only owned relations/test files from this run. Preserve all other workstreams.
