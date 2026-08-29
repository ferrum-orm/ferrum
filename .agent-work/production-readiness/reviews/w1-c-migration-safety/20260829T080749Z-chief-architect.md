---
task_id: w1-c-migration-safety
run_id: 20260829T080749Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 02d585513980ae89dbd6474196619a82faac460d
decision: approved
scope:
  - ADR-004 closure criteria: one-connection transactionality, advisory locking, atomic ledger writes, tested non-transactional failure semantics
  - JSON orchestrator path (orchestrator.apply) is the ADR-004 closure surface, not CLI postgres wrapping
  - EnableRLS(force=True) emits ENABLE then FORCE (W0-B ta-16)
  - alter_column SET NOT NULL / type narrowing hit the destructive confirm gate (§5a confirm hole)
  - Non-transactional phases (CREATE INDEX CONCURRENTLY) are explicit, not pretended rollback-safe
---

# Named Authority Verdict

## Authority

`ChiefArchitect`

This record grants only the ChiefArchitect gate for run `20260829T080749Z`.
It does not grant SecurityEngineer or CodeReviewer clearance. The workstream
cannot transition to `complete` until those sibling gates also record
`decision: approved`. ADR-004 may be closed in `AGENTS.md` §5 by the
coordinator after all three named-authority gates are recorded.

## Claims reviewed

1. **ADR-004 closure criterion 1 — one-connection transactionality.**
   `orchestrator.apply()` pins one connection via `conn.acquire()` and wraps
   all transactional ops + the ledger write in a single
   `raw_conn.transaction()` on that connection.
2. **ADR-004 closure criterion 2 — advisory locking.**
   `pg_advisory_xact_lock(int4, int4)` is acquired inside the transaction,
   auto-released on commit/rollback, with a stable key derived from
   `sha256(b"ferrum.migrations")`.
3. **ADR-004 closure criterion 3 — atomic ledger writes.**
   `record_applied_on_conn` runs inside the same transaction as the DDL ops
   on the same pinned connection; a failed DDL op rolls back the ledger
   INSERT.
4. **ADR-004 closure criterion 4 — tested non-transactional failure
   semantics.** Non-transactional ops (CREATE INDEX CONCURRENTLY, CREATE
   EXTENSION, CREATE FUNCTION) run as explicit pre-tx/post-tx phases;
   interspersed plans are rejected; post-tx failure has documented
   partial-failure semantics, not pretended rollback safety.
5. **JSON orchestrator path is the closure surface.** The transactional
   apply lives on `orchestrator.apply()` → `_apply_postgres`, which is the
   path §5a identified as the ADR-004 gap — not the CLI postgres wrapping.
6. **EnableRLS(force=True) emits ENABLE then FORCE.** A single `force=True`
   op emits both statements so `relrowsecurity` and `relforcerowsecurity`
   are both set; the W0-B ta-16 defect is closed.
7. **alter_column SET NOT NULL / type narrowing hit the destructive confirm
   gate.** Both the orchestrator `_is_op_destructive` scan and the
   `AlterColumn.classification` property classify these as destructive;
   `confirm=False` raises before any SQL is emitted.
8. **Replay guard fires for every non-dry-run apply.** `is_applied_on_conn`
   runs unconditionally inside the transaction, not only for
   token-authenticated applies.

## Evidence

Inspected independently from source at working-tree diff against base
revision `02d585513980ae89dbd6474196619a82faac460d`. The executor log
`logs/w1-c-migration-safety/20260829T080749Z.md` and the independent
verification `verification/w1-c-migration-safety/20260829T080749Z.md` were
read as claim lists only. Source citations are from the current working
tree.

### ADR-004 closure criterion 1 — one-connection transactionality

- `orchestrator.py:1427` — `async with conn.acquire() as raw_conn,
  raw_conn.transaction():` pins one connection and opens one transaction.
- `orchestrator.py:1463-1479` — transactional ops execute on `raw_conn`
  (the pinned connection), not on the driver or pool.
- `orchestrator.py:1482-1488` — `record_applied_on_conn(raw_conn, ...)`
  writes the ledger on the same pinned connection inside the same
  transaction.
- Independent verification §5 (live PostgreSQL): a plan with
  `[AddColumn(extra_col), RawSQL('CREATE TABLE bad_xxx; SELECT
  nonexistent_function();')]` — the first raw_sql statement succeeded
  inside the transaction, the second failed; the entire transaction
  (including AddColumn and the ledger write) rolled back. `extra_col` is
  absent, `bad_table` is absent, ledger row is absent. **Atomicity
  confirmed on live PostgreSQL.**

### ADR-004 closure criterion 2 — advisory locking

- `ledger.py:55-59` — `ADVISORY_LOCK_KEY_1/2` derived from
  `sha256(b"ferrum.migrations")` at import time; not user-supplied.
- `ledger.py:62-72` — `advisory_lock_sql()` returns
  `SELECT pg_advisory_xact_lock($1, $2)` with keys as bound parameters —
  no interpolation.
- `orchestrator.py:1437-1448` — lock acquired inside the transaction via
  `raw_conn.execute(advisory_lock_sql(), ADVISORY_LOCK_KEY_1,
  ADVISORY_LOCK_KEY_2)`. `pg_advisory_xact_lock` is transaction-scoped:
  auto-released on commit/rollback, so cancellation/interruption cannot
  leak the lock.
- `orchestrator.py:1440-1448` — on lock acquisition failure,
  `_lock_holder_diagnostics` queries `pg_locks` + `pg_stat_activity` for
  PID, application_name, and state only — never query text or bound
  values. Sanitized error with SQLSTATE when available.

### ADR-004 closure criterion 3 — atomic ledger writes

- `orchestrator.py:1458-1460` — `is_applied_on_conn(raw_conn, plan_digest,
  dialect="postgres")` checks the ledger on the pinned connection inside
  the transaction — no race between check and mutate.
- `orchestrator.py:1482-1488` — `record_applied_on_conn(raw_conn,
  plan_digest, ...)` writes the ledger on the same connection in the same
  transaction. If any DDL op fails, the `async with` exit rolls back the
  ledger INSERT along with the DDL.
- `ledger.py:289-316` — `record_applied_on_conn` maps
  `UniqueViolationError` to `FerrumMigrationError [FERR-M003]` so a
  concurrent second runner that slipped past the advisory lock still
  fails closed.
- Independent verification §5 confirms: failed DDL → ledger row absent.

### ADR-004 closure criterion 4 — tested non-transactional failure semantics

- `orchestrator.py:1289-1332` — `_split_ops_by_phase` splits ops into
  `(pre_tx, tx, post_tx)`. Non-transactional kinds (`create_extension`,
  `create_function`) and `add_index` with `concurrently=True` are routed
  outside the transaction block.
- `orchestrator.py:1326-1330` — interspersed plans (transactional op after
  a post-tx non-transactional op) raise `FerrumMigrationError
  [FERR-M001]`.
- `orchestrator.py:1415-1421` — pre-tx ops run in autocommit before the
  transaction; if a pre-tx op fails, the ledger is not written and
  re-running is safe.
- `orchestrator.py:1493-1508` — post-tx ops run in autocommit after the
  transaction; if a post-tx op fails, the error explicitly states "The
  migration is recorded as applied; the failed op must be reconciled
  manually." This is documented partial-failure semantics, not
  pretended rollback safety.
- `operations.py:282-307` — `AddIndex(concurrently=True)` classified as
  `"non_transactional"`; emits `CREATE INDEX CONCURRENTLY` (no `IF NOT
  EXISTS` — PostgreSQL rejects the combination); rejected on
  non-PostgreSQL backends.
- Independent verification §7 confirms: pre-tx/post-tx phase splitting
  visible in integration output; `test_interspersed_non_transactional_ops_rejected`
  confirms interspersed plans raise.

### JSON orchestrator path is the closure surface

- `orchestrator.py:1384-1396` — `apply()` dispatches to
  `_apply_postgres` for PostgreSQL and `_apply_thin_parity` for
  non-PostgreSQL. The transactional apply is on the JSON orchestrator
  path (`orchestrator.apply()`), which is the surface §5a identified as
  the ADR-004 gap:
  > Current JSON `orchestrator.apply()` ... takes
  > `conn._require_driver()`, then `for op in ops: await driver.execute(sql)`
  > with no wrapping `conn.transaction()`.
- `migrate_cmd.py:152-277` — `_apply_migration_postgres` mirrors the
  same advisory-lock + transactional + atomic-ledger pattern for CLI
  file-based apply. This is alignment, not the closure surface. The
  §5a note that CLI postgres wrapping "is a different surface from
  `orchestrator.apply()`" is respected: the closure is on the JSON path.

### EnableRLS(force=True) emits ENABLE then FORCE

- `orchestrator.py:701-710` — `enable_rls` with `force=True` returns:
  `ALTER TABLE "t" ENABLE ROW LEVEL SECURITY; ALTER TABLE "t" FORCE ROW
  LEVEL SECURITY`. ENABLE before FORCE.
- `orchestrator.py:711` — `enable_rls` without `force` still emits only
  `ENABLE ROW LEVEL SECURITY` (plain EnableRLS unchanged).
- Independent verification §2 (direct SQL emission):
  `ALTER TABLE "tickets" ENABLE ROW LEVEL SECURITY; ALTER TABLE "tickets"
  FORCE ROW LEVEL SECURITY` — contains ENABLE, contains FORCE, ENABLE
  before FORCE.
- Independent verification §3 (live PostgreSQL pg_class): after a single
  `EnableRLS(tname, force=True)` op, `relrowsecurity = True` AND
  `relforcerowsecurity = True`. RLS enforcement: zero rows returned with
  no GUC on a non-superuser non-bypassrls connection.
- The W0-B xfail `test_force_rls_without_enable_rls_grants_no_isolation_defect`
  is dropped; replaced by `test_force_rls_alone_enables_and_forces_rls`
  which verifies both flags on a non-superuser connection. `ta-16`
  classification in `manifest.py` is `SUPPORTED`.

### alter_column SET NOT NULL / type narrowing destructive confirm gate

- `orchestrator.py:155-169` — `_is_op_destructive(op)` returns `True`
  for `alter_column` with `not_null is True` OR `sql_type is not None`.
- `orchestrator.py:1370` — `is_destructive = any(_is_op_destructive(op)
  for op in ops)` replaces the old `_DESTRUCTIVE_KINDS`-only check that
  omitted `alter_column`.
- `operations.py:216-219` — `AlterColumn.classification` returns
  `"destructive"` for `not_null is True OR sql_type is not None`.
- Independent verification §4 (live PostgreSQL):
  - `AlterColumn(tname, 'val', not_null=True)` with `confirm=False` →
    raises "Migration requires explicit confirmation" (SET NOT NULL).
  - `AlterColumn(tname, 'val', sql_type='INT')` with `confirm=False` →
    raises "Migration requires explicit confirmation" (type narrowing).
  - `AlterColumn(tname, 'val', not_null=False)` with `confirm=False` →
    applied successfully (DROP NOT NULL is safe).
- This closes the §5a confirm hole: "JSON `apply()` of `alter_column`
  SET NOT NULL with `requires_confirmation=False` therefore does not hit
  the MIG-2 confirm gate."

### Replay guard

- `orchestrator.py:1458-1460` — `is_applied_on_conn` runs unconditionally
  inside the transaction for every non-dry-run apply. The old code
  (`if confirm and token is not None: if await is_applied(...)`) only
  checked for token-authenticated applies.
- Independent verification §6 (live PostgreSQL): re-applying the same
  digest raises `[FERR-M003]` without a token.

## Findings

### Non-blocking (do not reopen this gate)

1. **info** — Pre-tx op failures (`orchestrator.py:1415-1421`) propagate
   without `migration_op_failure` wrapping, so the error surfaces as a raw
   driver exception rather than a sanitized `FerrumMigrationError`. Tx ops
   (line 1469-1479) and post-tx ops (line 1499-1508) have proper wrapping.
   This is a consistency issue, not a safety gap: a pre-tx failure means the
   ledger is not written and re-running is safe. Optional: wrap pre-tx op
   failures in `migration_op_failure` for consistent error taxonomy.

2. **info** — Ledger table creation is inlined in `_apply_postgres`
   (`orchestrator.py:1451-1456`) as `CREATE TABLE IF NOT EXISTS
   ferrum_migrations (...)` rather than calling `ensure_ledger_on_conn`
   from `ledger.py:328-333`. Functionally equivalent; minor inconsistency
   with the `_on_conn` variants added to `ledger.py`. Optional: call
   `ensure_ledger_on_conn(raw_conn, dialect="postgres")` for
   single-source DDL.

3. **info** — The verifier's finding #3 (revert contract): `git checkout
   02d5855 -- <owned paths>` would reintroduce the FORCE-only EnableRLS
   bug because the base revision has the defect. The executor documented
   this caveat twice in the revert section. A proper revert that must not
   reintroduce the bug would need to keep the `orchestrator.py` EnableRLS
   fix while reverting the transactional-apply changes. Not blocking — a
   full revert is unlikely and the caveat is explicit.

4. **info** — `migrate_cmd.py:_apply_migration_postgres` (lines 152-277)
   duplicates the phase-splitting loop from `orchestrator._split_ops_by_phase`
   rather than calling it. The classification logic (`_is_op_non_transactional`)
   is shared; only the loop structure is duplicated. Optional: refactor
   the CLI to call `orchestrator._split_ops_by_phase` on
   `[op.to_op_dict() for op in ops]`. Code quality, not blocking.

5. **info** — `SET LOCAL lock_timeout = {lock_timeout}`
   (`orchestrator.py:1431`) uses f-string interpolation, but
   `_validate_timeout` (line 186-196) enforces `^\d+(ms|s|min|h)?$` before
   emission, so SQL injection is prevented. The regex is the security
   boundary. SecurityEngineer should confirm this in the sibling gate.

### Affirmed (architecture)

6. **ADR-004 closure criteria met.** All four criteria from AGENTS.md §5
   are satisfied on the JSON orchestrator path with independent live
   PostgreSQL evidence:
   - one-connection transactionality ✅
   - advisory locking ✅
   - atomic ledger writes ✅
   - tested non-transactional failure semantics ✅
7. **JSON orchestrator path is the closure surface.** The transactional
   apply lives on `orchestrator.apply()` → `_apply_postgres`, not just
   CLI postgres wrapping. The §5a distinction is respected.
8. **EnableRLS ENABLE+FORCE.** The W0-B ta-16 defect is closed. A single
   `force=True` op sets both `relrowsecurity` and `relforcerowsecurity`.
9. **alter_column confirm hole closed.** SET NOT NULL and type narrowing
   both hit the destructive confirm gate on the JSON orchestrator path.
10. **Non-transactional phases are explicit.** Pre-tx/post-tx phase
    splitting with interspersed rejection and documented post-tx
    partial-failure semantics — not pretended rollback safety.
11. **Boundary discipline preserved.** Advisory lock, transaction,
    ledger, phase splitting, and timeout validation are all Python-side.
    No async I/O or transaction logic leaked into Rust.

### Escalations (do not self-clear)

- **SecurityEngineer (required, sibling gate):** SQL emission for
  EnableRLS ENABLE+FORCE, advisory lock SQL, `SET LOCAL` timeout
  interpolation (regex boundary), lock-holder diagnostics query, and the
  apply diff touching migration-apply security surfaces. This verdict
  affirms the architecture; SecurityEngineer must clear the SQL/apply
  security gate.
- **CodeReviewer (required, sibling gate):** general code quality
  including the duplication noted in finding 4 and the pre-tx error
  wrapping noted in finding 1.
- **ProductManager:** not required per task contract.
- **CEO:** none. No new board-level technology choice.

## Decision

`approved`

The implementation meets all four ADR-004 closure criteria on the JSON
orchestrator path (`orchestrator.apply()` → `_apply_postgres`):

1. **One-connection transactionality** — `_apply_postgres` pins one
   connection via `conn.acquire()` and wraps all transactional ops + the
   ledger write in `raw_conn.transaction()` on that connection.
2. **Advisory locking** — `pg_advisory_xact_lock($1, $2)` acquired inside
   the transaction, auto-released on commit/rollback, with a stable
   key derived from `sha256(b"ferrum.migrations")`.
3. **Atomic ledger writes** — `record_applied_on_conn` runs inside the
   same transaction as the DDL ops on the same pinned connection.
4. **Tested non-transactional failure semantics** — phase splitting with
   interspersed rejection and documented post-tx partial-failure
   semantics.

Additionally:
- EnableRLS(force=True) emits ENABLE then FORCE (W0-B ta-16 closed).
- alter_column SET NOT NULL / type narrowing hit the destructive confirm
  gate (§5a confirm hole closed).
- The closure is on the JSON orchestrator path, not just CLI postgres
  wrapping.

**ADR-004 closure assessment:** The implementation meets ADR-004 closure
criteria. ADR-004 may be closed in `AGENTS.md` §5 by the coordinator
after SecurityEngineer and CodeReviewer also record `decision:
approved` for this run. The ChiefArchitect gate is satisfied.

This record grants only the named authority's gate. It does not
substitute for SecurityEngineer, CodeReviewer, or independent
verification.
