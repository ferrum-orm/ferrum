---
task_id: w1-b-transactions-retries-locks
wave: wave-1
owner: production-readiness-executor
status: in_progress
run_id: 20260829T085649Z
shared_path_lease: null
dependencies:
  - w1-d-error-taxonomy
  - w1-e-pool-lifecycle
  - w1-a-query-correctness
owned_paths:
  - python/ferrum/runtime.py
  - python/ferrum/connection.py
  - python/ferrum/queryset.py
  - tests/python/unit/test_runtime.py
  - tests/python/unit/test_transactions.py
  - tests/python/integration/test_transactions.py
  - tests/python/integration/test_concurrency.py
security_triage_complete: true
security_surfaces:
  sql_compilation: true
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: true
security_review_justification: statement-retry scope and select_for_update SQL are security-gated
architecture_review: true
product_review: false
code_review: true
---

# Task: Transactions, retries, and concurrency control

## Specify

### Problem

Ratified `AGENTS.md` §5a forbids statement retry on `Transaction` / savepoints
and on autocommit writes. Current `RetryPolicy` still wraps `execute` and
includes a `connection` category. There is no `run_transaction` replay API, no
`select_for_update`, and no typed advisory-lock helpers.

### Scope

Implement the §5a retry contract, `run_transaction(fn, retry=...)`,
`select_for_update`, and advisory locks. This contract currently owns
`runtime.py` and transaction tests only.

### Non-goals

Do not edit `python/ferrum/connection.py` until W1-E verifies (coordinator will
extend ownership). Do not edit `python/ferrum/queryset.py` until W1-A verifies
(`select_for_update` waits for that grant). Do not close ADR-004. Do not add
statement retry to `orchestrator.apply()`. Do not edit errors.py (W1-D).

### Invariants and failure modes

Object-scoped: statements through a `Transaction` never retry. Autocommit
writes never statement-retry. Remaining autocommit read retry, if enabled, is
deadlock `40P01` and serialization `40001` only. `run_transaction` opens a
fresh transaction per attempt, retries allowlisted SQLSTATE only, uses capped
exponential backoff with jitter, honors cancellation/deadline, and documents
callback idempotency. Concurrent lock tests must not leak connections.

### Acceptance criteria

- Pinned `Transaction` / savepoint never statement-retries.
- Autocommit writes do not statement-retry; default remains `retry=None`.
- `run_transaction` matches §5a and is tested for serialization/deadlock replay,
  cancellation rollback, and no connection leaks.
- `select_for_update(nowait, skip_locked, of)` verified with concurrent live
  PostgreSQL (after queryset.py is granted).
- Typed advisory-lock helpers with validated keys and transaction/session scope.
- Nested savepoints remain correct.

## Plan

Disable retry in `TimedQueryExecutor` when the target is a `Transaction`.
Implement `run_transaction` in the connection/runtime layer after W1-E.
Compile `select_for_update` after W1-A. Use W1-D `sqlstate`/`category` for
retry decisions; do not invent a second taxonomy. ChiefArchitect, SecurityEngineer,
and CodeReviewer required.

## Tasks

1. Wait for W1-D and W1-E verified; coordinator extends owned paths to
   `connection.py` (and queryset.py after W1-A).
2. Disable statement retry on Transaction/savepoint and autocommit writes.
3. Implement `run_transaction` with allowlisted SQLSTATE, backoff, cancellation.
4. Add `select_for_update` and advisory-lock helpers.
5. Live PostgreSQL concurrency/cancellation tests.
6. Docs via a coordinator CHANGELOG/README lease; do not edit those files unleased.

## Implement

Implementation begins only when the coordinator marks this task `ready` and
grants any newly owned shared or previously exclusive paths.

## Validation contract

Focused runtime/transaction tests plus live lock/concurrency tests, then
`mise run ci-local`.

## Independent verification contract

Verifier proves object-scoped no-retry with a live aborted transaction,
`run_transaction` replay, and lock behavior. Named gates: ChiefArchitect,
SecurityEngineer, CodeReviewer `decision: approved`. ProductManager `not_required`.

## Revert contract

Revert only this workstream's runtime/connection/queryset/test files from its
runs. Preserve W1-A/D/E work.
