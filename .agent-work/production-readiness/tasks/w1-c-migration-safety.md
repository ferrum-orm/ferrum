---
task_id: w1-c-migration-safety
wave: wave-1
owner: production-readiness-executor
status: in_progress
run_id: 20260829T080749Z
shared_path_lease: w1-c-shared-20260829T080749Z
dependencies: []
owned_paths:
  - python/ferrum/migrations/orchestrator.py
  - python/ferrum/migrations/ledger.py
  - python/ferrum/migrations/operations.py
  - python/ferrum/cli/migrate_cmd.py
  - tests/python/unit/test_migrations.py
  - tests/python/unit/test_new_operations.py
  - tests/python/unit/test_ledger.py
  - tests/python/unit/test_migration_gates.py
  - tests/python/unit/test_migration_operations.py
  - tests/python/unit/test_migrations_cli.py
  - tests/python/security/test_migration_safety.py
  - tests/python/integration/test_migrations_integration.py
  - tests/python/integration/test_ticket_analyzer_compat.py
  - tests/consumer_contracts/test_ticket_analyzer_contracts.py
  - tests/consumer_contracts/manifest.py
security_triage_complete: true
security_surfaces:
  sql_compilation: true
  migration_apply: true
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: true
  schema_selection: false
security_review: true
security_review_justification: EnableRLS SQL emission, advisory-locked apply, and destructive confirm are security-gated
architecture_review: true
product_review: false
code_review: true
---

# Task: Migration execution safety

## Specify

### Problem

JSON `orchestrator.apply()` is autocommit-per-operation with a separate ledger
write (ADR-004 reopened). `_DESTRUCTIVE_KINDS` omits `alter_column`; SET NOT
NULL / type narrowing can skip MIG-2 confirm. `EnableRLS(force=True)` emits
FORCE without ENABLE (`relrowsecurity` stays false; policies are a silent
no-op — W0-B ta-16). There is no one-connection advisory lock around ledger
check/mutate.

### Scope

`orchestrator.apply()`, ledger, operations SQL (including EnableRLS), CLI
postgres apply alignment, destructive confirm, timeouts/lock diagnostics, and
owned tests including dropping the W0-B FORCE-only xfail.

### Non-goals

No statement-retry as a stand-in for a migration transaction (W1-B). No
Wave 3 migration graph/autodiff. Do not broaden `_DEFAULT_VALUE_ALLOWLIST`
(ta-15; later SecurityEngineer-gated). Do not edit `README.md` / `CHANGELOG.md`
/ `AGENTS.md` (record bullets in the log). ADR-004 stays reopened in `AGENTS.md`
until this work independently verifies transactional apply and ChiefArchitect
closes it.

### Invariants and failure modes

Dry-run remains mandatory. Destructive actions and non-dev applies require
explicit confirmation. Identifiers allowlisted; no secret/DSN/row data in
migration output. ENABLE then FORCE must run together so RLS is actually
enforced for table owners. Transactional DDL + ledger write are atomic on one
pinned connection with a stable advisory lock. Non-transactional phases
(`CREATE INDEX CONCURRENTLY`) are explicit and not pretended rollback-safe.
Concurrent migrators and changed applied files fail closed. Cancellation and
process interruption must not leave an unfenced ledger lie.

### Acceptance criteria

- Advisory lock before ledger check/mutate; duplicate runners fail closed.
- Each transactional migration and its ledger write are atomic on one connection.
- Non-transactional ops are validated phases with tested partial-failure semantics.
- Configurable `lock_timeout` / `statement_timeout`, lock-holder diagnostics.
- Checksum/dependency validation race-safe; reject changed applied files.
- Tests: interruption, failed-op rollback, lock contention, duplicate runners,
  cancellation, transactional DDL, non-transactional partial failure.
- EnableRLS(force=True) emits ENABLE and FORCE; live test on a non-superuser
  non-bypassrls role; drop
  `test_force_rls_without_enable_rls_grants_no_isolation_defect` xfail; retarget
  `test_sql_emission_force` and ticket-analyzer compat.
- SET NOT NULL / type-narrowing `alter_column` hit the destructive confirm gate.
- Preserve dry-run, environment confirmation, redaction.

## Plan

Implement transactional apply + advisory lock on the JSON orchestrator path
(do not treat CLI postgres wrapping as ADR-004). Fix EnableRLS emission to
ENABLE+FORCE. Classify destructive `alter_column`. SecurityEngineer must
approve the SQL-emission and apply diff before completion — gather evidence
and implement against the W0-B reproduction, but do not self-clear. ChiefArchitect
reviews ADR-004 closure criteria vs the new tests. CodeReviewer required.

## Tasks

1. Reproduce FORCE-without-ENABLE on a non-bypass role; fix emission; drop xfail.
2. Close the `alter_column` confirm hole (SET NOT NULL and type narrowing).
3. One-connection advisory lock + transactional ops + atomic ledger write.
4. Explicit non-transactional phases with partial-failure tests.
5. Timeouts, lock-holder diagnostics, race-safe checksum/dependency checks.
6. Interruption/cancellation/duplicate-runner tests; focused checks; `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260821T093502Z` with exclusive owned
paths and lease `w1-c-shared-20260821T093502Z` covering `ledger.py`. Implement
the Tasks section. Stop if a new architecture choice appears that §5a does not
already decide.

## Validation contract

Focused unit + `test_migration_safety` + live `test_migrations_integration` +
ticket-analyzer compat RLS tests, then `mise run ci-local`. Record SQL actually
emitted and `pg_class.relrowsecurity` / `relforcerowsecurity` from a
non-superuser role. Exit status alone does not prove RLS.

## Independent verification contract

Verifier reproduces ENABLE+FORCE on a non-bypass role, confirms the xfail is
gone, proves transactional apply+ledger atomicity and lock contention, and
confirms destructive `alter_column` cannot apply without confirmation. Named
gates: ChiefArchitect, SecurityEngineer, CodeReviewer `decision: approved`.
ProductManager `not_required`. Generic verification does not close ADR-004.

## Revert contract

Revert only owned migration/CLI/test files from this run. Preserve W1-A/D
edits and Wave 0 contracts. Inverse of EnableRLS must not reintroduce
FORCE-only emission.
