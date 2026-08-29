---
task_id: w0-a-architecture-contracts
run_id: 20260821T082329Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-21T08:48:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: approved
scope:
 - Retry scope as a security property (statement retry vs aborted PostgreSQL transactions; autocommit retry categories)
 - Safe error fields (sqlstate, category, constraint, model, operation; never DETAIL, HINT, values, rows, DSNs)
 - RLS / admin GUCs and schema selection (transaction-local, allowlisted identifiers, no fake tenant id, search_path reset)
 - Migration apply as a gated surface (dry-run, confirm, redaction); ADR-004 remains reopened
---

# Named Authority Verdict

## Authority

`SecurityEngineer`

This record is a re-review of `AGENTS.md` §5a against `AGENTS.md` §3 and current source after the executor applied the 075800Z `changes_required` must-fix list. It does not implement code, does not edit shared paths, and does not persist itself under `reviews/`. It does not substitute for ChiefArchitect, ProductManager, CodeReviewer, or independent verification.

Mapped quality gate: **Pass of the binding contract text** → `approved`. Both required §5a corrections are present and accurate versus current source. Current-code holes named below remain assigned to W1-B / W1-C / W1-D / W1-F and must not be treated as shipped. This record does not start those workstreams.

## Claims reviewed

1. **Retry scope (draft — ChiefArchitect + SecurityEngineer; 075800Z must-fix 1).** Statement retry on `Transaction` / `savepoint` is unsafe. Remaining autocommit statement retry, if any, is deadlock `40P01` + serialization `40001` only; `connection` / timeout are not valid for DML; default `retry=None`; transactional write retry is only W1-B `run_transaction`. Do not ratify today’s `RetryPolicy` as a supported autocommit contract.
2. **ADR-004 / migration notes (draft — SecurityEngineer; 075800Z must-fix 2).** JSON `apply()` `_DESTRUCTIVE_KINDS` omits `alter_column`; `AlterColumn.classification` omits type narrowing; autodiff `requires_confirmation: False`; CLI file-migrate postgres transaction is not ADR-004 closure. W1-C owns the SET NOT NULL / type-narrowing confirm hole.
3. **Safe error fields (draft — SecurityEngineer; re-confirm).** Sanctioned fields are exactly `sqlstate`, `category`, `constraint`, `model`, `operation`. PostgreSQL `DETAIL` / `HINT`, bound values, row data, and full DSNs never appear in any field, hook payload, log line, or exception message at any tier. W1-D promotes `sqlstate` / `category` to structured attributes on every mapped exception.
4. **Schema tenancy and sharding (draft — ChiefArchitect + SecurityEngineer; re-confirm).** `tenant_transaction()` GUC pattern is the RLS mechanism; add `platform_admin_transaction()` with allowlisted admin GUCs and no fake tenant id; add `schema_transaction()` with identifier allowlist, transaction-local `search_path`, guaranteed reset; optional `ConnectionRegistry` / `ShardRouter` with a trusted shard key; QuerySet stays connection-explicit; no `platform_scoped` model flag.

Out of this gate (ProductManager): alpha-to-stable compatibility / multi-backend policy. Out of this gate (ChiefArchitect): identity map, implicit lazy I/O, unrestricted SQL rejections — except as they touch SQL-safety. This record grants only the SecurityEngineer gate for run `20260821T082329Z`.

## Evidence

Inspected current source at `base_revision` `768ec1f3013f6d0eccd7c8b590ba36b54b12d23e`. The executor log `logs/w0-a-architecture-contracts/20260821T082329Z.md` was read only as a claim list; every claim below is from `AGENTS.md` §5a and source. No tests were executed (review-only). No `reviews/` file was written. Prior verdict `reviews/w0-a-architecture-contracts/20260821T075800Z-security-engineer.md` (`decision: changes_required`) is not rewritten.

### Retry — 075800Z must-fix 1 present and accurate

Current runtime (unchanged; still a safety gap, not a supported pattern):

- `python/ferrum/runtime.py:23-24,43-44,49-76,197-233` — `TimedQueryExecutor._execute_with_policy` applies connection-scoped `RetryPolicy` (default `None`) to every `fetch` / `fetchrow` / `fetchval` / `execute`. Allowed categories today: `{deadlock, connection, serialization}`. Default `RetryPolicy.on` is `{deadlock}`. `_exception_category` maps `TimeoutError` / `asyncio.TimeoutError` / `ConnectionError` / `OSError` to `connection`. Query-timeout `TimeoutError` from `asyncio.timeout` is converted to `FerrumTimeoutError` before the retry `except Exception` and is not retried.
- `python/ferrum/connection.py:118,133-139,375-390,484-491,534-540` — `Connection.__init__` default `retry=None`. `Transaction` is constructed with `runtime=self._runtime`. `Transaction._require_driver()` wraps the pinned bound connection in `TimedQueryExecutor` with that same runtime. `savepoint()` yields another `Transaction` with the same runtime. There is no transaction-boundary check in the retry loop.

§5a Retry scope (`AGENTS.md:135-180`) now states, and this matches source:

- (a) Disable statement retry on every `Transaction` and savepoint-wrapped `Transaction`, **object-scoped** (statements issued through a `Transaction` never retry; `conn` terminals during `async with conn.transaction() as tx` use a different pooled connection).
- (b) Remaining autocommit statement retry, if enabled, is deadlock (`40P01`) and serialization (`40001`) only, and only for discrete autocommit **reads** (`fetch` / `fetchrow` / `fetchval` and QuerySet read terminals). Restricting remaining retry to reads is stricter than the 075800Z minimum and is accepted.
- (c) `connection` and timeout are **not** valid statement-retry categories for DML (ambiguous commit → duplicate writes). Autocommit writes (`execute`, QuerySet `create` / `update` / `delete` / `upsert`, DDL) must not statement-retry.
- (d) Default remains `retry=None`.
- (e) The only write-retry story is W1-B `run_transaction(fn, retry=...)` (fresh transaction per attempt, SQLSTATE allowlist `40001` / `40P01`, capped exponential backoff with jitter, honor cancellation/deadline, caller-documented idempotency). No special case for autocommit `execute` deadlocks.
- Explicit: do **not** treat today’s `RetryPolicy` (includes a `connection` category and wraps `execute`) as a supported autocommit contract. Current in-TX firing is named a known safety gap.
- Explicit: this contract does **not** close ADR-004; `orchestrator.apply()` must not grow statement-retry as a stand-in for a migration-spanning transaction.

075800Z must-fix 1 is satisfied. The live `RetryPolicy` still wraps `execute` and still inherits onto `Transaction` / `savepoint`; that remains W1-B implementation work, not a remaining §5a wording defect.

### ADR-004 / migration notes — 075800Z must-fix 2 present and accurate

Current runtime (unchanged):

- `python/ferrum/migrations/orchestrator.py:8-10,127-138,1150-1156,1212-1241` — JSON `apply()` defaults `dry_run=True`. Destructive gate is `op["kind"] in _DESTRUCTIVE_KINDS` and does not trust plan JSON `requires_confirmation`. `_DESTRUCTIVE_KINDS` is `drop_table`, `drop_column`, `drop_fk`, `raw_sql`, `drop_extension`, `disable_rls`, `drop_policy`, `drop_function` — **`alter_column` is absent**. Autodiff emits `"requires_confirmation": False`. Live apply is `for op in ops: await driver.execute(sql)` with **no** wrapping `conn.transaction()`. `record_applied` runs after the loop as a separate execute. Module docstring still claims a structural dry-run cycle via `MigrationPlan`; the API is `apply(conn, plan_json, dry_run=False)`.
- `python/ferrum/migrations/operations.py:173-217` — `AlterColumn` docstring classifies type narrowing and `SET NOT NULL` as destructive. `classification` returns `"destructive"` only for `not_null is True`; type-only alters return `"safe"`.
- `python/ferrum/cli/migrate_cmd.py:92-155` — file-based postgres apply uses `op.classification == "destructive"` (would catch SET NOT NULL) and wraps ops + ledger in `async with pool.acquire() as db_conn, db_conn.transaction()`. Non-postgres file apply is still per-op autocommit.

§5a ADR-004 / migration apply notes (`AGENTS.md:182-206`) now records all of the above, assigns the SET NOT NULL / type-narrowing confirm hole to W1-C, and states the CLI postgres transaction is a different surface — not one-connection advisory locking, tested non-transactional phases, or ADR-004 closure. `AGENTS.md` §5 still marks ADR-004 reopened (`AGENTS.md:107-116`). `AGENTS.md` §3 still requires confirmation for type narrowing and `NOT NULL` on a populated column (`AGENTS.md:81-85`).

075800Z must-fix 2 is satisfied. The confirm hole remains open in code; W1-C owns closing it. Do not treat CLI file-migrate’s postgres transaction as shipped ADR-004.

### Safe error fields — re-confirm, no wording change required

- `python/ferrum/errors.py:84-97,134-143,218-236,286-413` — `FerrumCompileError` has `model`, `field`, `operator`, `category`. `FerrumIntegrityError` has `constraint`, `category`. Query-path `map_db_error` surfaces `type(exc).__name__` plus constraint name for integrity; it does not copy `.detail`, `.hint`, bound values, or DSNs. `context` is documented for `model` / `operation` and is never applied. The only `sqlstate` use is inside `_postgres_ddl_error_detail` (migration path): SQLSTATE plus `str(exc)` primary message — never `.detail` / `.hint`. No `sqlstate` attribute on query-path `FerrumConnectionError`, `FerrumDatabaseError`, `FerrumTimeoutError`, `FerrumSchemaError`, or `FerrumIntegrityError`.
- §5a Safe error fields (`AGENTS.md:208-228`) still names the sanctioned set as exactly `sqlstate`, `category`, `constraint`, `model`, `operation`, and still forbids `DETAIL` / `HINT` / bound values / row data / full DSNs at every tier. W1-D remains the owner of promoting `sqlstate` / `category` to structured attributes on every mapped exception.

**Confirm.** Current query-path redaction still matches the “never DETAIL / HINT / values / rows / DSNs” rule. Structured `sqlstate` / `model` / `operation` are still not shipped. W1-D must not promote the migration-path primary message onto query-path exceptions. `map_db_error(..., context=)` must apply allowlisted context keys as attributes, never by interpolating driver text.

### RLS / admin GUCs / schema selection — re-confirm, no wording change required

- `python/ferrum/session.py:32-43,64-83,113-171` — `ALLOWED_GUC_NAMES` is a closed frozenset; `search_path` is not on it. `set_config` interpolates the name only after allowlist check and binds the value with `transaction_local=true`. `tenant_transaction(..., admin=True)` still requires `tenant_id` and always sets the tenant GUC, then `admin_guc='true'`. There is no `platform_admin_transaction`.
- Grep of `python/ferrum/`: no `schema_transaction`, `ShardRouter`, `ConnectionRegistry`, or `platform_admin_transaction`.
- `python/ferrum/connection.py:396-437` — `call_function` still validates schema / function identifiers with `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`.

§5a Schema tenancy and sharding (`AGENTS.md:230-257`) still requires `platform_admin_transaction()` with allowlisted admin GUCs and no fake tenant id; `schema_transaction()` with strict identifier validation (never string-interpolated from untrusted input), transaction-local `search_path`, reset at transaction end; QuerySet connection-explicit; no `platform_scoped` model flag; no dialect switching through the registry.

**Confirm** as the W1-F security contract. Transaction-local `set_config(..., true)` remains the correct reset guarantee. `schema_transaction` must not add caller-supplied schema names to the `f"SELECT set_config('{name}', ...)"` pattern; validate with the same identifier regex as `call_function`, use SET LOCAL / `set_config(..., true)`, and test pool reuse after cancel/rollback.

## Findings

### High

None remaining for this §5a contract gate.

### Medium (implementation / contract precision; do not claim closed)

1. **`apply()` dry-run is default, not structurally mandatory.** Module docstring (`orchestrator.py:8-10`) claims `apply()` requires a `MigrationPlan` from `dry_run()`. Callers can pass `dry_run=False` on raw plan JSON. W1-C must not document this as a structural gate until it is one.
2. **CLI postgres file-migrate transaction ≠ ADR-004.** `migrate_cmd.py:124-149` wraps one file’s ops + ledger in a driver transaction. `orchestrator.apply()` does not. Non-transactional kinds (`create_extension`, `create_function`) are “future enforcement” only (`orchestrator.py:125-126,139-143`). Partial JSON apply without ledger is possible.
3. **Migration DDL `str(exc)` is a transitional exception, not a sanctioned field.** `_postgres_ddl_error_detail` (`errors.py:218-236`) includes the PostgreSQL primary message. W1-D may keep a schema-level primary message on `FerrumMigrationError` only if it still excludes `.detail` / `.hint` / values; query-path exceptions must stay on structured fields only.
4. **`map_db_error(..., context=)` never attaches `model` / `operation`.** `errors.py:286-296` vs body through `:413`. W1-D must apply allowlisted context keys as attributes, never by interpolating driver text.

### Low

5. **GUC name interpolation is safe only because the allowlist is closed.** `session.py:83`. `schema_transaction` must not add caller-supplied schema names to this `f"SELECT set_config('{name}', ...)"` pattern.

These medium/low items are unchanged from 075800Z. They are W1-C / W1-D / W1-F implementation constraints, not remaining §5a wording defects.

## Decision

`approved`

**Retry — confirm diagnosis, confirm the amended contract.** In-transaction statement retry is unsafe and is live today whenever `RetryPolicy` is configured (`Transaction` and `savepoint` inherit `RuntimeConfig.retry`). §5a now forbids it object-scoped, restricts any remaining autocommit statement retry to discrete reads with SQLSTATE `40P01` / `40001` only, excludes `connection` / timeout for DML, keeps `retry=None`, and points write retry at W1-B only. It does not ratify today’s `RetryPolicy`. W1-B must make the disable structural.

**Safe error fields — confirm.** Current query-path mapping matches the “never DETAIL / HINT / values / rows / DSNs” rule. Structured `sqlstate` / `category` / `model` / `operation` are the correct W1-D target. Do not treat them as shipped.

**RLS / schema / shards — confirm the draft as the security contract.** `tenant_transaction` is transaction-local and allowlisted. `platform_admin_transaction` must not require a fake tenant id. `schema_transaction` must use strict identifier validation, transaction-local `search_path`, and reset-on-end tests (including cancellation and pool reuse). Shard routing stays outside QuerySet. No `platform_scoped` model flag.

**Migration apply — confirm gated, confirm ADR-004 unshipped, confirm the confirm hole is recorded.** Dry-run default, drop_* / raw_sql confirm, non-dev confirm, and dry-run redaction hold on the JSON path. SET NOT NULL / type narrowing via `alter_column` do not. §5a now states that JSON `apply()` `_DESTRUCTIVE_KINDS` omits `alter_column`, `AlterColumn.classification` omits type narrowing, autodiff emits `requires_confirmation: False`, and CLI file-migrate’s postgres transaction is not ADR-004 closure. W1-C owns transactional apply, advisory locking, atomic ledger writes, non-transactional phase semantics, and closing the `alter_column` confirm hole.

This approval makes the SecurityEngineer-owned §5a drafts (retry, ADR-004/migration notes, safe error fields, tenancy/sharding security contract) eligible to become binding **for this run** once ChiefArchitect independently records `decision: approved` for `20260821T082329Z`. Until that ChiefArchitect verdict exists, §5a remains UNRATIFIED as a whole. Do not implement W1-B / W1-C / W1-F against these drafts until both named verdicts exist.

### Missing tests (do not claim covered; assign to implementing workstreams)

- No test that `RetryPolicy` is a no-op on `Transaction` / `savepoint`, or that a deadlock inside an open TX does not retry into `25P02`.
- No test that mapped query-path exceptions expose `sqlstate` as an attribute (does not exist yet).
- No security test that JSON `apply(..., dry_run=False, confirm=False)` rejects `alter_column` SET NOT NULL and type narrowing.
- No test for `platform_admin_transaction` without a tenant id, or `schema_transaction` search_path reset on commit/rollback/cancel/pool reuse (APIs absent).
- No test that autocommit `connection`-category retry of non-idempotent DML is rejected.

### Recommendations (non-blocking)

- W1-B: make in-TX retry disable structural in `TimedQueryExecutor` (inspect the execution object / use a Transaction-specific executor), not documentation-only.
- W1-C: validate index `WHERE` fragments; keep `raw_sql` confirm-gated; do not add new string-fragment SQL.
- W1-F: shard key is a routing input, never an SQL identifier; pools remain independently configured PostgreSQL; no implicit multi-DSN QuerySet behavior.
- Observability/echo already honor Ferrum-specific opt-in; preserve that when W1-D adds `sqlstate` to failure payloads — SQLSTATE may appear as a structured exception attribute, not as hook payload text unless explicitly designed as Tier A metadata.

This record grants only the SecurityEngineer gate. It does not substitute for another authority or independent verification.
