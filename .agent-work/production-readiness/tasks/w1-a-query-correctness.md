---
task_id: w1-a-query-correctness
wave: wave-1
owner: production-readiness-executor
status: in_progress
run_id: 20260821T093501Z
shared_path_lease: null
dependencies: []
owned_paths:
  - python/ferrum/queryset.py
  - crates/ferrum-sql/
  - crates/ferrum-core/
  - tests/python/unit/test_queryset_ir.py
  - tests/python/unit/test_queryset_terminals.py
  - tests/python/unit/test_queryset_guards.py
  - tests/python/unit/test_queryset_value_variants.py
  - tests/python/unit/test_pg_types.py
  - tests/python/security/test_sql_safety.py
  - tests/python/integration/test_crud.py
  - tests/python/integration/test_query_expressions.py
  - tests/python/integration/test_terminals.py
  - tests/python/integration/test_bulk.py
security_triage_complete: true
security_surfaces:
  sql_compilation: true
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: SQL identifier/operator emission and write-scope invariants are security-gated
architecture_review: false
product_review: false
code_review: true
---

# Task: Query and write correctness

## Specify

### Problem

`filter(field=None)` / `exclude(field=None)` do not compile to Django-style
`IS NULL` / `IS NOT NULL`. `danger_delete_all` is an `xfail` that emits a
dangling `WHERE`. Casts on the write path can disagree with migration DDL
(UUID/numeric/array/jsonb), which fails live PostgreSQL. Malformed
fields/operators/sorts must fail before SQL emission.

### Scope

QuerySet IR construction, Rust SQL emission, and owned unit/security/integration
tests. Live PostgreSQL round-trips for create/filter/join/update/bulk
update/upsert/hydration of the supported type matrix.

### Non-goals

No `select_for_update` (W1-B after this workstream verifies). No retry,
pool, tenancy, or migration-apply changes. No IR major bump unless a new node
is unavoidable; prefer the existing `is_null` / `is_not_null` IR. Do not edit
`crates/ferrum-pyo3/` (W1-D), `python/ferrum/errors.py`, `README.md`, or
`CHANGELOG.md` (record changelog bullets in the run log). Do not edit
`tests/python/integration/schema.py` or `test_read_path.py`.

### Invariants and failure modes

Identifiers stay metadata-allowlisted; values stay bound parameters. Slicing,
`order_by`, and `limit` must never widen UPDATE/DELETE scope. Unknown
fields/operators/sort directions fail closed before SQL. Rust remains
pure/sync/stateless. Cancellation stays in Python. Concurrent compiles must
not share mutable per-request state.

### Acceptance criteria

- `filter(field=None)` emits `IS NULL`; `exclude(field=None)` emits `IS NOT NULL`;
  explicit `__is_null` still works.
- `danger_delete_all` is un-`xfail`ed and emits valid SQL with no dangling `WHERE`.
- Invariant tests prove slice/order/limit cannot widen write scope.
- Table-driven PostgreSQL cast matrix matches migration DDL for UUID, numeric/
  Decimal, arrays, JSON/JSONB, enum, timestamp/date/time, bytea, tsvector, inet,
  and vector.
- Golden IR-to-SQL plus live round-trips for create, filter, join, update, bulk
  update, upsert, and hydration.
- Fuzz of malformed fields/operators/sorts fails before SQL emission.

## Plan

Map `None` equality in QuerySet to existing null IR; fix `danger_delete_all`
emitter; add a DDL-aligned bind/cast table in `ferrum-sql`; extend security
fuzz tests. Do not change the PyO3 error surface. SecurityEngineer reviews the
diff before completion; CodeReviewer is the quality gate. Architecture review
is not required unless an IR version bump or new IR node is introduced — stop
and escalate to the coordinator if that happens.

## Tasks

1. Reproduce `filter(None)` / `exclude(None)` and `danger_delete_all` against
   compiled SQL and live PostgreSQL.
2. Implement null-equality compilation and un-`xfail` `danger_delete_all`.
3. Add write-scope invariant tests (slice/order/limit).
4. Add the cast matrix and golden IR-to-SQL tests; live round-trips for each type.
5. Extend malformed-input fuzz in `test_sql_safety.py`.
6. Run focused pytest plus `mise run ci-local`. Record public CHANGELOG bullets
   in the log; do not edit CHANGELOG.md.

## Implement

Coordinator marked this workstream `in_progress` at `20260821T093501Z` with
exclusive owned paths and no shared-path lease. Implement the Tasks section.
Stop on path overlap, IR-version surprise, or a new architecture choice.

## Validation contract

Focused: ruff/ty on touched Python, `cargo fmt`/`clippy`/`test` for
`ferrum-sql` and `ferrum-core`, owned pytest unit + security + live PostgreSQL
integration. Then `mise run ci-local`. Record full command output. Known
external blockers: unowned `schema.py` / `test_read_path.py` lint; do not
relabel those as this task passing.

## Independent verification contract

Verifier recompiles SQL for `filter(None)` / `exclude(None)` / `danger_delete_all`,
re-runs the cast matrix against live PostgreSQL, and confirms write-scope
invariants and pre-emission fuzz. Named gates:
`reviews/w1-a-query-correctness/<run-id>-security-engineer.md` and
`...-code-reviewer.md` with `decision: approved`. ChiefArchitect and
ProductManager are `not_required` unless an IR bump is introduced.

## Revert contract

Revert only owned QuerySet/emitter/test files from this run. Preserve W1-C/D
edits, unowned integration helpers, and Wave 0 docs. Do not use destructive git.
