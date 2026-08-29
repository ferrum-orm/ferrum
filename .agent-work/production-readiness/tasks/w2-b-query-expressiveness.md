---
task_id: w2-b-query-expressiveness
wave: wave-2
owner: production-readiness-executor
status: in_progress
run_id: 20260829T095632Z
shared_path_lease: null
dependencies:
  - w1-a-query-correctness
  - w1-b-transactions-retries-locks
owned_paths:
  - python/ferrum/queryset.py
  - python/ferrum/expressions.py
  - crates/ferrum-sql/src/emit.rs
  - crates/ferrum-sql/src/lib.rs
  - crates/ferrum-core/src/ir/mod.rs
  - crates/ferrum-core/src/compile/mod.rs
  - tests/python/unit/test_query_expressiveness.py
  - tests/python/integration/test_query_expressiveness.py
security_triage_complete: true
security_surfaces:
  sql_compilation: true
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: SQL compilation — identifiers must stay allowlisted, values bound, no raw SQL escape hatches
architecture_review: true
product_review: false
code_review: true
---

# Task: Query expressiveness required by Org AI

## Specify

### Problem

Consumer parity manifests require multi-hop relation lookups, subqueries, scalar
subqueries, typed joins/projections, conditional expressions, database functions,
reusable expressions, grouping/having, filtered aggregates, window functions, CTEs,
and UNION. These do not exist or are incomplete.

### Scope

`python/ferrum/queryset.py`, `python/ferrum/expressions.py`, Rust SQL emitter
(`crates/ferrum-sql/`), IR (`crates/ferrum-core/src/ir/`), compiler
(`crates/ferrum-core/src/compile/`), and owned tests. Implement only parity-manifest-
backed APIs.

### Non-goals

No `models.py` edits (W2-A owns). No `relations.py` edits (W2-C owns). No `connection.py` /
`runtime.py` edits (W1-B/E own, complete). No `errors.py` / `hooks.py` edits (W1-D/W4-A
own, complete). No `__init__.py` edits (shared path, no lease). No `README.md` /
`CHANGELOG.md` (record bullets in log). Do NOT add `raw()`, `extra()`, or string fragments.

### Invariants and failure modes

Immutable QuerySets. Allowlisted identifiers. Bound values. SQL fingerprints. Write-scope
safety. No `raw()`, `extra()`, or string fragments. Query-plan and index-usage checks for
hot consumer paths. Prevent accidental N+1 and unbounded materialization.

### Acceptance criteria

- Multi-hop relation lookup compilation with deterministic aliases and cycle/depth limits.
- `exists`/`not exists` subqueries, scalar subqueries, typed joins/projections.
- Conditional expressions, database functions, reusable expressions.
- Grouping/having, filtered aggregates, window functions, CTEs, UNION only where consumer
  queries require them.
- Preserve immutable QuerySets, allowlisted identifiers, bound values, SQL fingerprints,
  and write-scope safety.
- Query-plan and index-usage checks for hot consumer paths.
- Prevent accidental N+1 and unbounded materialization.
- No `raw()`, `extra()`, or string fragments.

## Plan

Extend `expressions.py` with typed expression classes. Extend `queryset.py` with
subquery/join/aggregate/window/CTE/UNION builders. Extend Rust IR and SQL emitter.
ChiefArchitect for the IR/emitter architecture; SecurityEngineer for SQL compilation
safety; CodeReviewer required.

## Tasks

1. Audit existing `expressions.py` and `queryset.py` and identify gaps vs the plan.
2. Add multi-hop relation lookup compilation with deterministic aliases and cycle/depth limits.
3. Add `exists`/`not exists` subqueries, scalar subqueries, typed joins/projections.
4. Add conditional expressions, database functions, reusable expressions.
5. Add grouping/having, filtered aggregates, window functions, CTEs, UNION.
6. Extend Rust IR and SQL emitter for new node types (bump IR version if unavoidable —
   stop and escalate if that happens).
7. Query-plan and index-usage checks for hot consumer paths.
8. Prevent N+1 and unbounded materialization.
9. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T095632Z` with exclusive owned paths and
no shared-path lease. Do NOT bump IR version or introduce new IR nodes without
escalating to the coordinator (ChiefArchitect gate).

## Validation contract

Focused query expressiveness unit tests plus live PostgreSQL integration tests, then
`mise run ci-local`.

## Independent verification contract

Verifier proves SQL compilation safety, query-plan quality, and expressiveness parity.
Named gates: ChiefArchitect, SecurityEngineer, CodeReviewer. ProductManager `not_required`.

## Revert contract

Revert only owned queryset/expressions/IR/emitter/compiler/test files from this run.
Preserve all other workstreams.
