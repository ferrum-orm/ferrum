---
task_id: w1-b-transactions-retries-locks
run_id: 20260829T085649Z
authority: ChiefArchitect
reviewer: ChiefArchitect
reviewed_at: 2026-08-29T11:00:00Z
base_revision: 71e04328688bf9142751aa8a2c1c59dc1a69b410
decision: approved
scope:
  - Retry contract architecture (AGENTS.md §5a — binding)
  - run_transaction design
  - select_for_update SQL architecture
  - Advisory lock helper design
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

1. Statement-level retry applies only to discrete autocommit reads
   (fetch/fetchrow/fetchval); default `retry=None`.
2. Autocommit writes NEVER statement-retry.
3. Transaction/savepoint statements NEVER retry (object-scoped, not
   connection-scoped).
4. `run_transaction` opens fresh tx per attempt, replays callback, restricts
   to 40001/40P01, capped exponential backoff with jitter, honors
   cancellation/deadline.
5. `select_for_update` SQL is architecturally sound (identifiers from
   metadata, values bound).
6. Advisory lock helpers with validated keys, transaction/session scope.
7. No retry on `orchestrator.apply()` (not added as stand-in for migration
   transaction).
8. `connection` category removed from retry categories.

## Evidence

### Source inspected

- `git diff HEAD -- python/ferrum/runtime.py python/ferrum/connection.py python/ferrum/queryset.py` (full diff)
- `python/ferrum/runtime.py` lines 45-66, 69-111, 114-230, 323-416 (current state)
- `python/ferrum/connection.py` lines 588-679, 680-762, 796-998 (current state)
- `python/ferrum/queryset.py` lines 282-315, 1715-1817, 2266-2269, 2636-2639, 3017-3019, 3117-3119, 3186-3193, 3436-3439 (current state)
- Task contract: `.agent-work/production-readiness/tasks/w1-b-transactions-retries-locks.md`
- Executor log: `.agent-work/production-readiness/logs/w1-b-transactions-retries-locks/20260829T085649Z.md`
- Independent verification: `.agent-work/production-readiness/verification/w1-b-transactions-retries-locks/20260829T085649Z.md`
- Binding contract: AGENTS.md §5a (Retry scope, ADR-004 notes, Safe error fields, Schema tenancy)

### Claim 1: Statement-level retry on discrete autocommit reads only; default retry=None

`_RETRY_CATEGORIES` reduced to `frozenset({"deadlock", "serialization"})`
(`runtime.py:50`). The `"connection"` category was removed. `_exception_category`
(`runtime.py:53-66`) only returns `"deadlock"`, `"serialization"`, or `None` —
both the asyncpg `PostgresConnectionError` check and the generic Python
exception check (`TimeoutError`/`ConnectionError`/`OSError`) that previously
returned `"connection"` were removed. `RetryPolicy.__post_init__`
(`runtime.py:95-102`) rejects any category not in `_RETRY_CATEGORIES`, so
constructing `RetryPolicy(on={"connection"})` raises `ValueError`.

`RetryPolicy` docstring (`runtime.py:71-89`) accurately describes the §5a
contract: default is `retry=None`, allowed categories are deadlock/serialization
only, and the policy never applies to writes or Transaction-pinned statements.

`TimedQueryExecutor._execute_with_policy` (`runtime.py:382-384`) sets
`retry = None` when `self._is_transaction or is_write`. The remaining
autocommit read retry path (`runtime.py:398-400`) calls
`retry.should_retry(exc, attempt)` which checks `_exception_category(exc) in
self.on` — limited to deadlock/serialization.

**Verdict: PASS.** Statement-level retry is restricted to discrete autocommit
reads with deadlock/serialization categories only. Default is `retry=None`.

### Claim 2: Autocommit writes NEVER statement-retry

`TimedQueryExecutor.execute` (`runtime.py:412-415`) always passes
`is_write=True` to `_run`, which passes it to `_execute_with_policy`, which
sets `retry = None` (`runtime.py:383-384`).

QuerySet write terminals with RETURNING pass `is_write=True` on their
`fetch`/`fetchrow` calls:
- `create` (returning): `queryset.py:2269` — `fetchrow(..., is_write=True)`
- `bulk_create` (returning): `queryset.py:2639` — `fetch(..., is_write=True)`
- `upsert` (returning): `queryset.py:3019` — `fetchrow(..., is_write=True)`
- `bulk_upsert` (returning): `queryset.py:3119` — `fetchrow(..., is_write=True)`
- `update_returning`: `queryset.py:3439` — `fetch(..., is_write=True)`

Write terminals without RETURNING use `driver.execute(...)` which is always
`is_write=True`.

**Verdict: PASS.** No autocommit write path can statement-retry.

### Claim 3: Transaction/savepoint NEVER retry (object-scoped)

`Transaction._require_driver()` (`connection.py:803-808`) constructs
`TimedQueryExecutor(..., is_transaction=True)`. The `is_transaction` flag is
on the executor object, not on the Connection — this is object-scoped, not
connection-scoped. A `Connection`'s configured `RetryPolicy` is inherited by
the `RuntimeConfig` passed to the `TimedQueryExecutor`, but `is_transaction=True`
overrides it to `None` at `runtime.py:383-384`.

`Transaction.savepoint()` yields another `Transaction` whose
`_require_driver()` also passes `is_transaction=True` — nested savepoints
inherit the no-retry guarantee.

`Connection.advisory_lock()` (`connection.py:680-762`) yields a `Transaction`
handle constructed with the same `self._runtime`. The yielded Transaction's
`_require_driver()` passes `is_transaction=True`, so statements inside an
advisory lock session also never statement-retry.

The §5a contract says: "Disable statement retry on every Transaction and
savepoint-wrapped Transaction. This is object-scoped: statements issued
through a Transaction never retry. It is not 'once any transaction is open on
this Connection' — `conn` terminals during `async with conn.transaction() as
tx` use a different pooled connection." The implementation satisfies this:
the `Connection`'s own `_require_driver()` does NOT set `is_transaction`, so
`conn` terminals during a transaction use a different executor (and different
pooled connection) that retains its own retry policy.

**Verdict: PASS.** Object-scoped no-retry on Transaction, savepoint, and
advisory-lock-pinned handles. Connection-scoped terminals retain their own
retry policy.

### Claim 4: run_transaction matches §5a

`Connection.run_transaction` (`connection.py:591-678`):

- **Fresh tx per attempt:** `async with self.transaction(...)` opens a new
  transaction on each loop iteration (`connection.py:661-666`).
- **Replays entire callback:** `return await fn(tx)` — the full callback
  executes from scratch in each attempt (`connection.py:667`).
- **Restricts to 40001/40P01:** `TransactionRetryPolicy.should_retry`
  (`runtime.py:186-193`) checks `isinstance(exc, FerrumError)` and
  `exc.sqlstate in _RETRIABLE_XACT_SQLSTATES` where
  `_RETRIABLE_XACT_SQLSTATES = frozenset({"40001", "40P01"})`
  (`runtime.py:118`). Uses the W1-D `sqlstate` attribute — no second taxonomy.
- **Capped exponential backoff with jitter:** `backoff_seconds`
  (`runtime.py:195-203`) computes `min(backoff_base * 2**(attempt-1),
  backoff_max)` plus uniform jitter in `[-jitter * delay, +jitter * delay]`.
  `TransactionRetryPolicy.__post_init__` validates `max_attempts >= 1`,
  `backoff_base >= 0`, `backoff_max >= backoff_base`, `jitter in [0, 1]`.
- **Honors cancellation:** `except asyncio.CancelledError: raise`
  (`connection.py:675-678`) — no retry, rollback already happened via the
  `self.transaction()` context manager.
- **Honors deadline:** `deadline` passed to `self.transaction(deadline=...)`
  per attempt (`connection.py:665`). Docstring (`connection.py:643-646`)
  accurately describes this as per-attempt: "Each attempt's transaction
  receives the full `deadline` (the budget is not tracked across attempts)."
- **Documents callback idempotency:** Docstring (`connection.py:613-616`)
  states "fn MUST be idempotent. Replay re-executes the entire callback from
  scratch on each attempt."

Non-Ferrum, non-CancelledError exceptions propagate without retry — correct
(business-logic errors should not be retried).

**Verifier Finding 1 (deadline semantics):** The original docstring said
"for the entire run (all attempts combined)" but the implementation gives
each attempt the full budget. The current code resolves this: the docstring
was corrected to accurately describe per-attempt deadline behavior. Per-attempt
deadline is architecturally safe (each attempt gets bounded), and the §5a
contract says "honor cancellation/deadline" without mandating total-vs-per-attempt.
The corrected docstring eliminates the discrepancy.

**Verifier Finding 2 (contradictory comment):** The verifier referenced
`runtime.py:66-68` as having a comment "connection is intentionally NOT
returned here" followed by `return "connection"`. In the current code
(`runtime.py:53-66`), `_exception_category` is clean: it returns only
`"deadlock"`, `"serialization"`, or `None`. Both `return "connection"` paths
were removed. No contradictory comment exists. This finding is not present in
the current state.

**Verdict: PASS.** `run_transaction` faithfully implements the §5a
write-retry contract.

### Claim 5: select_for_update SQL architecturally sound

`QuerySet.select_for_update` (`queryset.py:1718-1817`):

- **Identifiers from metadata:** The `of` argument entries are validated
  against `metadata.relations` field names or the literal `"self"`. Each is
  resolved to `metadata.table_name` via `get_model(rel.to_model).get_metadata()`.
  Unknown entries raise `FerrumCompileError` (`queryset.py:1799-1807`).
  No user-supplied identifiers reach the SQL string.
- **Values bound:** `nowait` and `skip_locked` are boolean flags that append
  literal `NOWAIT` / `SKIP LOCKED` keywords. There are no values to bind —
  all dynamic content is identifiers from metadata.
- **SQL placement:** `_append_for_update_clause` (`queryset.py:285-314`)
  appends `FOR UPDATE [OF ...] [NOWAIT] [SKIP LOCKED]` after the compiled SQL
  text. PostgreSQL places `FOR UPDATE` after `LIMIT`/`OFFSET`, so appending at
  the end is correct.
- **PostgreSQL-only:** Non-postgres dialects are rejected with
  `FerrumConfigError` before SQL emission (`queryset.py:301-305`).
- **Write scope rejection:** `_check_write_scope` (`queryset.py:3186-3193`)
  rejects `select_for_update` on `update()` / `delete()` / `bulk_*()` / `upsert()`.
- **Mutual exclusion:** `nowait=True, skip_locked=True` raises
  `FerrumCompileError` (`queryset.py:1748-1752`).

**Architectural observation (non-blocking):** The `FOR UPDATE OF` clause
quotes table names with `f'"{t}"'` (`queryset.py:309`). This does not escape
embedded double-quote characters per PostgreSQL identifier quoting rules
(`"` should be doubled to `""`). However, `metadata.table_name` is set at
model class definition time from developer-controlled `Meta.table_name` and is
validated by the model metadata builder — it is not user input. This is
consistent with §2.9 (identifiers from metadata allowlists). A future
hardening pass could add proper identifier escaping, but this is not an
architectural violation.

**Architectural observation (non-blocking):** The `FOR UPDATE` clause is
post-processed in Python rather than compiled in the Rust IR. The executor log
acknowledges this as a known follow-up. This is architecturally acceptable
for v0.1 — the Rust IR does not yet support `FOR UPDATE`, and the Python
post-processing uses only validated identifiers from metadata. The boundary
contract (§4) says Rust owns compilation; this is a pragmatic exception
documented for future IR extension.

**Verdict: PASS.** `select_for_update` is architecturally sound. Identifiers
come from validated model metadata, no raw user input reaches SQL, and the
clause is correctly placed and scoped.

### Claim 6: Advisory lock helpers with validated keys, transaction/session scope

`AdvisoryLockKey` (`runtime.py:208-230`):

- Validates `int` (signed 64-bit: `-(2**63) <= key < 2**63`) or `(int, int)`
  (two signed 32-bit halves: `-(2**31) <= k < 2**31`).
- Rejects `bool` (subclass of int — explicit `isinstance(key, bool)` check).
- Rejects non-int, bad tuple length, overflow.
- `as_args()` returns a tuple of validated ints for SQL parameter binding.

Transaction-scoped (`connection.py:903-970`):

- `Transaction.advisory_xact_lock(key)` — `pg_advisory_xact_lock`, auto-released
  at transaction commit/rollback. Uses `_advisory_xact_lock_sql` with `$N::bigint`
  or `$1::int, $2::int` casts.
- `Transaction.advisory_try_xact_lock(key)` — `pg_try_advisory_xact_lock`,
  returns bool. Uses `_advisory_try_xact_lock_sql` with the same cast pattern.

Session-scoped (`connection.py:680-762`):

- `Connection.advisory_lock(key)` — pins a pool connection, takes
  `pg_advisory_lock`, yields a `Transaction` surface (for `ConnectionLike`
  protocol compatibility), releases with `pg_advisory_unlock` on exit
  (including exception/cancellation).
- Connection affinity is guaranteed by pinning from the pool for the lock
  duration.

All advisory lock SQL helpers (`connection.py:974-998`) use `$N` positional
parameters with explicit casts. No identifier interpolation. No user-supplied
SQL.

**Architectural observation (non-blocking):** `Connection.advisory_lock`
yields a `Transaction` object without issuing `BEGIN`. The docstring documents
this ("Yields a Transaction surface (without an actual transaction)"). This
reuses the `ConnectionLike` protocol so QuerySet terminals work against the
pinned connection, and the yielded handle's `_require_driver()` passes
`is_transaction=True` (no statement retry). A user calling `commit()` or
`rollback()` on the yielded handle would issue `COMMIT`/`ROLLBACK` without a
corresponding `BEGIN` — PostgreSQL treats these as no-op warnings. This is a
minor API semantic stretch, not an architectural violation.

**Verdict: PASS.** Advisory lock helpers validate keys, use bound parameters
with explicit casts, and correctly implement transaction-scoped and
session-scoped lock semantics.

### Claim 7: No retry on orchestrator.apply()

The diff does not touch `python/ferrum/migrations/orchestrator.py`. No
statement-retry was added to `orchestrator.apply()`. The §5a contract
explicitly states: "This contract does not close ADR-004. W1-C remains the
only owner of migration transactionality. `orchestrator.apply()` must not
grow statement-retry as a stand-in for a migration-spanning transaction."

**Verdict: PASS.** No retry added to migration apply.

### Claim 8: connection category removed

`_RETRY_CATEGORIES` is `frozenset({"deadlock", "serialization"})`
(`runtime.py:50`). The `"connection"` string was removed. Both code paths in
`_exception_category` that returned `"connection"` (asyncpg
`PostgresConnectionError` and generic Python `TimeoutError`/`ConnectionError`/
`OSError`) were removed. `RetryPolicy.__post_init__` rejects `"connection"`
because it is not in `_RETRY_CATEGORIES`.

**Verdict: PASS.** The `connection` category is fully removed from retry
categories and cannot be used.

## Findings

### FINDING A (observation, non-blocking): FOR UPDATE OF identifier quoting does not escape embedded double-quotes

- **Severity:** observation
- **Evidence:** `queryset.py:309` — `f'"{t}"'` quotes table names without
  doubling embedded `"` characters. PostgreSQL identifier quoting requires
  `"` to be escaped as `""`.
- **Risk:** None in current architecture — `metadata.table_name` is
  developer-controlled and validated at model class definition time, not
  user input. Consistent with §2.9 (identifiers from metadata allowlists).
- **Recommendation:** A future hardening pass should use a proper identifier
  quoting utility that doubles embedded quotes. Not a W1-B blocker.

### FINDING B (observation, non-blocking): FOR UPDATE post-processed in Python, not Rust IR

- **Severity:** observation
- **Evidence:** `_append_for_update_clause` (`queryset.py:285-314`) appends
  the `FOR UPDATE` clause to compiled SQL in Python. The Rust IR/compiler
  does not support `FOR UPDATE`.
- **Risk:** None — identifiers come from validated model metadata, and the
  clause placement (after LIMIT/OFFSET) is correct for PostgreSQL.
- **Recommendation:** A future workstream should add `FOR UPDATE` to the Rust
  IR for cleaner compilation. Documented as follow-up in the executor log.
  Not a W1-B blocker.

### FINDING C (observation, non-blocking): Connection.advisory_lock yields Transaction without BEGIN

- **Severity:** observation
- **Evidence:** `connection.py:740-747` — `advisory_lock` constructs a
  `Transaction` object but does not issue `BEGIN`. The yielded handle is for
  `ConnectionLike` protocol compatibility on the pinned connection.
- **Risk:** Minor API semantic stretch — a user calling `commit()`/`rollback()`
  on the yielded handle issues `COMMIT`/`ROLLBACK` without `BEGIN` (PostgreSQL
  no-op warning). Not a security issue.
- **Recommendation:** Document or guard against `commit()`/`rollback()` on
  the advisory-lock-pinned handle. Not a W1-B blocker.

### FINDING D (observation, non-blocking): # type: ignore on is_write call sites

- **Severity:** observation
- **Evidence:** 5 call sites in `queryset.py` (lines 2269, 2639, 3019, 3119,
  3439) use `# type: ignore` to suppress `ty`'s `unknown-argument` error
  because `QueryExecutorProtocol` in `drivers/protocol.py` (not owned by W1-B)
  does not declare the `is_write` keyword arg.
- **Risk:** None at runtime — the object is always a `TimedQueryExecutor` which
  accepts `is_write`.
- **Recommendation:** A future workstream should update
  `QueryExecutorProtocol` to include `is_write` as an optional parameter and
  remove the `# type: ignore` comments. Not a W1-B blocker.

### Verifier findings status

- **Verifier Finding 1 (deadline docstring):** RESOLVED. The current docstring
  (`connection.py:643-646`) accurately describes per-attempt deadline behavior.
  The §5a contract says "honor cancellation/deadline" without mandating
  total-vs-per-attempt; per-attempt is architecturally safe.
- **Verifier Finding 2 (contradictory comment):** NOT PRESENT in current code.
  `_exception_category` (`runtime.py:53-66`) is clean — both `return
  "connection"` paths were removed. No contradictory comment exists.

## Decision

**approved**

The retry contract architecture faithfully implements the ratified §5a
binding contract:

1. Statement-level retry is restricted to discrete autocommit reads with
   deadlock/serialization categories only; default is `retry=None`.
2. Autocommit writes never statement-retry (`execute` always `is_write=True`;
   QuerySet write terminals with RETURNING pass `is_write=True`).
3. Transaction/savepoint statements never retry (object-scoped via
   `is_transaction=True` on the executor, not connection-scoped).
4. `run_transaction` opens a fresh transaction per attempt, replays the entire
   callback, restricts to 40001/40P01 via W1-D `sqlstate`, uses capped
   exponential backoff with jitter, honors cancellation and deadline, and
   documents callback idempotency.
5. `select_for_update` is architecturally sound — identifiers from validated
   model metadata, no raw user input, correct PostgreSQL clause placement,
   write-scope rejection, PostgreSQL-only enforcement.
6. Advisory lock helpers validate keys (`AdvisoryLockKey`), use bound
   parameters with explicit casts, and correctly implement transaction-scoped
   and session-scoped lock semantics.
7. No retry was added to `orchestrator.apply()`.
8. The `connection` category is fully removed from `_RETRY_CATEGORIES` and
   `_exception_category`.

The four findings (A-D) are non-blocking architectural observations documented
as follow-ups. The verifier's two findings are resolved (Finding 1) or not
present in the current code (Finding 2).

This record grants only the ChiefArchitect gate. It does not substitute for
the SecurityEngineer gate (SQL compilation, advisory lock SQL safety) or the
CodeReviewer gate (general code quality).
