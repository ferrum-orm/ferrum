---
task_id: w1-c-migration-safety
run_id: 20260829T080749Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-29T10:45:00Z
base_revision: 02d585513980ae89dbd6474196619a82faac460d
decision: approved
scope:
  - EnableRLS SQL emission (ENABLE + FORCE)
  - Advisory-locked transactional apply
  - Destructive confirm gate (alter_column SET NOT NULL / type narrowing)
  - Dry-run mandatory before apply
  - No secret/DSN/row data in migration output
  - Identifier allowlists; no raw SQL escape hatches
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

Per AGENTS.md §3 (Security rules — migration safety, credential handling),
§5a (migration apply notes, safe error fields), and the W1-C task contract
security surfaces (`sql_compilation`, `migration_apply`, `rls_admin_gucs`):

1. **EnableRLS(force=True) emits BOTH ENABLE AND FORCE** (not FORCE-only) so
   `pg_class.relrowsecurity` and `relforcerowsecurity` are both set.
2. **Advisory lock prevents concurrent migrators from racing** — ledger
   check and write are on one pinned connection inside an advisory-locked
   transaction.
3. **Destructive actions (column drop, type narrowing, SET NOT NULL) require
   explicit confirmation** before any SQL is executed.
4. **Dry-run is still mandatory before apply** — the dry-run path is
   preserved and unchanged.
5. **No secret/DSN/row data in migration output, error messages, or dry-run
   output.**
6. **Identifiers are allowlisted; no raw SQL escape hatches.**

## Evidence

### Diff inspection

`git diff HEAD -- python/ferrum/migrations/orchestrator.py
python/ferrum/migrations/ledger.py python/ferrum/migrations/operations.py
python/ferrum/cli/migrate_cmd.py tests/python/security/test_migration_safety.py`

### Claim 1: EnableRLS(force=True) emits ENABLE + FORCE

- `orchestrator.py:701-708`: `force=True` branch returns:
  ```
  ALTER TABLE "t" ENABLE ROW LEVEL SECURITY; ALTER TABLE "t" FORCE ROW LEVEL SECURITY
  ```
  ENABLE precedes FORCE (order matters: `relrowsecurity` must be true before
  `relforcerowsecurity` can enforce owner compliance).
- The `else` arm (plain `EnableRLS` without `force`) still emits only
  `ENABLE ROW LEVEL SECURITY` — no regression.
- `test_new_operations.py:132-140` (`test_sql_emission_force`): asserts both
  `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` present and
  `sql.index("ENABLE") < sql.index("FORCE")`.
- Table identifier goes through `_quote_ident` (allowlisted). No user input
  in the statement beyond the validated identifier.

### Claim 2: Advisory lock prevents concurrent migrators from racing

- `ledger.py:44-78`: `ADVISORY_LOCK_KEY_1/2` derived from
  `sha256(b"ferrum.migrations")` at import time — not user-supplied.
  `advisory_lock_sql()` returns `SELECT pg_advisory_xact_lock($1, $2)` with
  keys passed as **bound parameters** (not string interpolation).
- `orchestrator.py:1420-1490` (`_apply_postgres`): lock acquired inside
  `async with conn.acquire() as raw_conn, raw_conn.transaction():` —
  auto-released on commit/rollback. Cancellation or process interruption
  cannot leak the lock.
- Ledger check (`is_applied_on_conn`) and ledger write
  (`record_applied_on_conn`) run on the **same pinned raw connection** inside
  the same transaction — no check/mutate race.
- `record_applied_on_conn` (`ledger.py:289-321`) maps
  `UniqueViolationError` → `FerrumMigrationError [FERR-M003]` — a concurrent
  runner that somehow slips past the advisory lock still fails closed via
  the unique constraint.
- Replay guard (`orchestrator.py:1455-1457`): `is_applied_on_conn` runs
  unconditionally inside the tx for every non-dry-run apply (not just
  token-authenticated ones). The old `if confirm and token is not None`
  scoping is gone.
- `migrate_cmd.py:199-225` (`_apply_migration_postgres`): CLI mirrors the
  same advisory-lock + transactional + atomic-ledger pattern for file-based
  apply.

### Claim 3: Destructive confirm gate covers alter_column

- `_is_op_destructive` (`orchestrator.py:155-167`): returns `True` for
  `alter_column` with `not_null is True` OR `sql_type is not None` (type
  narrowing). Other kinds use `_DESTRUCTIVE_KINDS` allowlist.
- `AlterColumn.classification` (`operations.py:216-219`): same logic —
  `not_null is True or sql_type is not None` → `"destructive"`.
- Gate fires at `orchestrator.py:1370-1375` BEFORE `_apply_postgres` is
  called — no SQL is executed before the confirm check. A crafted plan
  JSON with `requires_confirmation: False` still hits the gate because
  `_is_op_destructive` scans op dicts independently.
- Unit tests: `test_alter_column_set_not_null_requires_confirm`
  (`test_migrations.py:642`), `test_alter_column_type_narrowing_requires_confirm`
  (`test_migrations.py:656`), `test_alter_column_drop_not_null_does_not_require_confirm`
  (`test_migrations.py:670` — DROP NOT NULL is safe).
- `_DESTRUCTIVE_KINDS` unchanged: `drop_table`, `drop_column`, `drop_fk`,
  `raw_sql`, `drop_extension`, `disable_rls`, `drop_policy`, `drop_function`.

### Claim 4: Dry-run preserved

- `orchestrator.py:1354-1356`: `if dry_run: _print_plan(plan); return
  MigrationResult(applied=False, ops_count=len(ops), dry_run=True)` —
  returns before token validation, destructive gate, or any SQL. W1-C did
  not modify this path.
- MIG-1 tests (`TestMIG1DryRunDefault`) pass: `dry_run=True` never calls
  `execute`.
- CLI (`migrate_cmd.py:112-131`): `dry_run=True` prints plan without
  executing; destructive + dry_run prints and exits 0.

### Claim 5: No secret/DSN/row data in migration output

- `apply()` / `_apply_postgres` output: `[ferrum migrate] applying: <kind>
  <table>` — only operation kind and table name (Ferrum metadata). No DSN,
  password, bound values, or row data.
- Error messages: `migration_op_failure_from` delegates to
  `errors.migration_op_failure` (existing sanitized builder). Post-tx
  failure message includes only `type(exc).__name__` and op kind/table —
  no exception message body, no DSN, no row data.
- Lock-holder diagnostics (`orchestrator.py:1229-1257`): queries
  `pg_locks` + `pg_stat_activity` for `pid`, `application_name`, `state`
  only — never `query` text or bound values. Keys passed as bound
  parameters.
- MIG-5 tests pass: `test_dry_run_output_does_not_contain_dsn`,
  `test_error_message_does_not_contain_dsn` — secret password marker not
  found in captured output or exception messages.
- Timeout strings: `_validate_timeout` (`orchestrator.py:191-200`)
  validates against `^\d+(ms|s|min|h)?$` before any `SET LOCAL` emission.
  Only digits + optional unit can reach the SQL — no SQL injection vector.

### Claim 6: Identifier allowlists; no raw SQL escape hatches

- All identifiers go through `_quote_ident` with allowlists.
- `_SQL_TYPE_ALLOWLIST` (`orchestrator.py:165+`) for DDL type position.
- `_INDEX_USING_ALLOWLIST` for index access methods.
- Advisory lock keys: bound parameters (`$1`, `$2`), not interpolation.
- The only string interpolation into SQL is `SET LOCAL lock_timeout =
  {lock_timeout}` / `SET LOCAL statement_timeout = {statement_timeout}`,
  both validated by `_validate_timeout` against
  `^\d+(ms|s|min|h)?$` before emission. The regex admits only `\d+` plus
  an optional unit suffix — no SQL metacharacters can pass. (PostgreSQL
  `SET LOCAL` does not accept bound parameters for GUC values, so
  interpolation is the only option; the strict regex is the correct
  defense.)
- No `.raw()`, `.extra()`, or string-fragment filter APIs introduced or
  widened.
- `AddIndex(concurrently=True)` emits `CREATE INDEX CONCURRENTLY` with
  `_quote_ident` for index/table names and `_INDEX_USING_ALLOWLIST` for
  the access method. Rejected on non-PostgreSQL backends.

### Test run

```
$ uv run pytest tests/python/security/test_migration_safety.py -x -q -m "security or not security"
.................................                                        [100%]
33 passed in 0.29s
```

```
$ uv run pytest tests/python/security/test_migration_safety.py -x -q -m security
.................................                                        [100%]
33 passed in 0.26s
```

All 33 migration safety security tests pass.

## Findings

| # | Severity | Evidence | Required correction |
|---|----------|----------|---------------------|
| 1 | info | `orchestrator.py:1464-1469` creates the ledger table with inline SQL `CREATE TABLE IF NOT EXISTS ferrum_migrations (...)` rather than calling `ledger.ensure_ledger_on_conn`. The DDL is a fixed constant string with no user input, so it is safe, but it duplicates the schema definition from `ledger._create_ledger_sql`. | None — not a security issue. Optional: call `ensure_ledger_on_conn` to avoid schema drift. |
| 2 | info | `migrate_cmd.py:166-184` duplicates the phase-splitting loop from `orchestrator._split_ops_by_phase`. Both use `_is_op_non_transactional` for classification, so security classification is shared. | None — not a security issue. Optional: call `orchestrator._split_ops_by_phase` to avoid duplication. |
| 3 | info | `SET LOCAL lock_timeout = {lock_timeout}` uses string interpolation, but `_validate_timeout` runs first and the regex `^\d+(ms|s|min|h)?$` prevents any SQL injection. PostgreSQL does not accept bound parameters for `SET LOCAL` GUC values, so interpolation is the only option. | None — the validation is the correct defense. |
| 4 | info | Pre-existing (NOT a W1-C regression): `errors.py:_postgres_ddl_error_detail` includes the PostgreSQL HINT via `str(exc)` despite its docstring claiming "DETAIL/HINT attributes are never included." `errors.py` was not modified by W1-C. | None for W1-C. W1-D owns promoting the sanitized error-field contract per AGENTS.md §5a. |

No blocking or `changes_required` findings.

## Decision

**`approved`**

All security gates within the SecurityEngineer scope are satisfied with
fresh evidence from independent diff inspection and a passing security test
run:

- EnableRLS(force=True) emits ENABLE then FORCE (both `pg_class` flags set).
- Advisory lock serializes concurrent migrators via bound parameters;
  auto-released on transaction end; ledger check and write on one pinned
  connection; UniqueViolation fails closed.
- Destructive confirm gate covers `alter_column` SET NOT NULL and type
  narrowing; fires before any SQL execution; crafted plan JSON cannot
  bypass it.
- Dry-run path preserved and unchanged.
- No secret/DSN/row data in migration output, error messages, lock-holder
  diagnostics, or dry-run output.
- Identifiers allowlisted; no raw SQL escape hatches; timeout validation
  prevents SQL injection.

This record grants only the SecurityEngineer gate. It does not substitute
for the ChiefArchitect gate (ADR-004 closure criteria) or the CodeReviewer
gate (code quality). ADR-004 remains reopened in AGENTS.md §5 until
ChiefArchitect closes it.
