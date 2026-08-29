---
task_id: pilot-ticket-analyzer
run_id: 20260829T104025Z
authority: SecurityEngineer
reviewer: security-engineer-agent
reviewed_at: 2026-08-29T11:40:00Z
base_revision: 768784ef788eb0641c224ead99d1e35662c3f8e3
decision: approved
scope:
  - tests/consumer_contracts/test_ticket_analyzer_contracts.py
  - tests/consumer_contracts/manifest.py
  - tests/consumer_contracts/conftest.py
---

# Named Authority Verdict

## Authority

SecurityEngineer

## Claims reviewed

1. RLS tenant GUC (`app.team_id`) does not leak after `tenant_transaction` commit.
2. Platform-admin GUC (`app.platform_admin`) does not leak after admin
   `tenant_transaction` commit.
3. Tenant GUC does not leak on rollback (exception path).
4. No raw SQL injection surface in the contract tests.
5. GUC names are allowlist-validated; values are bound parameters.
6. No secrets/DSNs/credentials in test code or manifest evidence.

## Evidence

### GUC isolation mechanism

Source read of `python/ferrum/session.py:94-113`:
```python
async def set_config(tx: Transaction, name: str, value: str) -> None:
    _validate_guc_name(name)
    driver = tx._require_driver()
    await driver.execute(f"SELECT set_config('{name}', $1, true)", value)
```

The third argument `true` to PostgreSQL's `set_config()` sets
`is_local=true`, which guarantees the GUC reverts to its prior value when the
current transaction commits or rolls back. This is the correct PostgreSQL
mechanism for transaction-scoped configuration that cannot leak across pooled
connection reuse.

`tenant_transaction` (`session.py:197-201`) wraps `set_config` calls inside
`async with conn.transaction(...) as tx:`, so the GUC lifecycle is bound to
the transaction lifecycle. On commit or rollback (including exception-driven
rollback), PostgreSQL resets the GUC automatically.

### GUC allowlist

`session.py:36-39`: `ALLOWED_GUC_NAMES = frozenset({"app.team_id", "app.platform_admin"})`.
`_validate_guc_name` (`session.py:85-86`) rejects any name not in the allowlist
with `FerrumCompileError` before opening the transaction. The GUC name in the
SQL string `f"SELECT set_config('{name}', $1, true)"` is therefore always an
allowlist constant — never user input. The value is passed as bound parameter
`$1` — never interpolated.

### GUC isolation tests (live PG)

Three new tests verified live:
- `test_tenant_guc_does_not_leak_after_commit`: creates an event inside
  `tenant_transaction`, commits, bare-queries `rls_conn` (same pooled
  connection) → `assert leaked == []`. PASSES.
- `test_platform_admin_guc_does_not_leak_after_commit`: admin transaction sees
  both teams; after commit, bare query → `assert leaked == []`. PASSES.
- `test_tenant_guc_does_not_leak_on_rollback`: raises `RuntimeError` inside
  `tenant_transaction`, bare query → `assert leaked == []`. PASSES.

The rollback test is particularly important: it confirms GUC reset works on
the exception/rollback path, not just the happy commit path. The bare query
goes through the same `rls_conn` (pooled connection), proving no cross-request
leakage.

### Raw SQL in contract tests

The only `driver.execute(...)` calls in
`test_ticket_analyzer_contracts.py` are DDL role management
(`_create_rls_role` / `_drop_rls_role`, lines 49-69): `CREATE ROLE`, `GRANT`,
`REVOKE`, `DROP ROLE`. These use f-strings with `role_name` derived from
`unique_suffix` (a 12-char UUID hex), and `team_table` / `event_table` derived
the same way. No user-supplied input reaches these strings. This is test
infrastructure for creating a non-superuser RLS role, not application SQL.

The application-level queries in the tests all go through `QuerySet` terminals
(`Event.objects.create`, `Event.objects.filter().all`, `Event.objects.all`),
which enforce identifier allowlisting and bound parameters per §2.9. No
`.raw()`, `.extra()`, or string-fragment filters are present.

### Secrets/credentials

No DSNs, passwords, or secrets appear in the manifest evidence fields. The
test fixtures use `FERRUM_TEST_DSN` from the environment (conftest.py:42-45).
The RLS role password `'ferrum_rls'` is a test-only constant for a disposable
local role, not a production secret.

## Findings

No blocking findings.

The GUC isolation is verified by both source inspection (the `set_config(...,
true)` transaction-local mechanism is the correct PostgreSQL primitive) and
live test execution (all 3 isolation tests pass against a live PostgreSQL
with FORCE RLS enforced via a non-superuser role).

## Decision

**approved** — RLS tenant and platform-admin GUC isolation is verified: no
leak on commit, rollback, or pool reuse. GUC names are allowlist-validated;
values are bound parameters. No raw SQL injection surface in contract tests.
No secrets exposed.
