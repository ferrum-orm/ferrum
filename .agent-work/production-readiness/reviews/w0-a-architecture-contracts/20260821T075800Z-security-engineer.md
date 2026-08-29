---
task_id: w0-a-architecture-contracts
run_id: 20260821T075800Z
authority: SecurityEngineer
reviewer: security-engineer
reviewed_at: 2026-08-21T08:25:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: changes_required
scope:
 - Retry scope as a security property (statement retry vs aborted PostgreSQL transactions; autocommit retry categories)
 - Safe error fields (sqlstate, category, constraint, model, operation; never DETAIL, HINT, values, rows, DSNs)
 - RLS / admin GUCs and schema selection (transaction-local, allowlisted identifiers, no fake tenant id, search_path reset)
 - Migration apply as a gated surface (dry-run, confirm, redaction); ADR-004 remains reopened
---

# Named Authority Verdict

## Authority

`SecurityEngineer`

This record is a contract review of `AGENTS.md` §5a drafts against `AGENTS.md` §3 and current source. It does not implement code, does not edit shared paths, and does not persist itself under `reviews/`. It does not substitute for ChiefArchitect, ProductManager, CodeReviewer, or independent verification.

Mapped quality gate: **Fail that blocks merge of the binding contract** → `changes_required`. The drafts are directionally correct and do not claim the gaps are closed; two required text corrections must land in §5a before this authority will record `approved`. Current-code holes named below are assigned to later workstreams and must not be treated as shipped.

## Claims reviewed

1. **Retry scope (draft — ChiefArchitect + SecurityEngineer).** Statement-level `RetryPolicy` on an open PostgreSQL transaction is unsafe because deadlock / serialization failure aborts the whole transaction. Draft: valid only for autocommit; disable once a `Transaction` is open; transactional retry belongs to W1-B `run_transaction(fn, retry=...)`.
2. **Safe error fields (draft — SecurityEngineer).** Sanctioned fields are exactly `sqlstate`, `category`, `constraint`, `model`, `operation`. PostgreSQL `DETAIL` / `HINT`, bound values, row data, and full DSNs never appear in any field, hook payload, log line, or exception message at any tier. W1-D promotes `sqlstate` / `category` to structured attributes on every mapped exception.
3. **Schema tenancy and sharding (draft — ChiefArchitect + SecurityEngineer).** `tenant_transaction()` GUC pattern is the RLS mechanism; add `platform_admin_transaction()` with allowlisted admin GUCs and no fake tenant id; add `schema_transaction()` with identifier allowlist, transaction-local `search_path`, guaranteed reset; optional `ConnectionRegistry` / `ShardRouter` with a trusted shard key; QuerySet stays connection-explicit; no `platform_scoped` model flag.
4. **Migration apply remains gated; ADR-004 is reopened.** Dry-run default, destructive and non-dev confirmation, credential redaction. `orchestrator.apply()` is per-operation autocommit. Do not treat transactional apply as shipped.

Out of this gate (ProductManager): alpha-to-stable compatibility / multi-backend policy. Out of this gate (ChiefArchitect): identity map, implicit lazy I/O, unrestricted SQL rejections — except as they touch SQL-safety.

## Evidence

Inspected current source at `base_revision` `768ec1f3013f6d0eccd7c8b590ba36b54b12d23e`. The executor log `logs/w0-a-architecture-contracts/20260821T075800Z.md` was read only as a claim list; every claim below is from source. No tests were executed (review-only). No `reviews/` file was written.

### Retry

- `python/ferrum/runtime.py:23-24,27-45,49-76,197-221` — `TimedQueryExecutor._execute_with_policy` applies connection-scoped `RetryPolicy` (default `None`) to every `fetch` / `fetchrow` / `fetchval` / `execute`. Allowed categories: `deadlock`, `serialization`, `connection`. Default `RetryPolicy.on` is `{deadlock}`. `_exception_category` maps `TimeoutError`, `asyncio.TimeoutError`, `ConnectionError`, and `OSError` to `connection`. Query-timeout `TimeoutError` from `asyncio.timeout` is converted to `FerrumTimeoutError` before the retry `except Exception` and is not retried; driver-level connection loss / `OSError` is retriable if `connection` is in `on`. Backoff is linear (`backoff_base * attempt`), no jitter.
- `python/ferrum/connection.py:133-139,170-175,375-390,484-491,534-540` — `Connection` stores `retry` on `RuntimeConfig`. `Transaction` is constructed with `runtime=self._runtime`. `Transaction._require_driver()` wraps the pinned bound connection in `TimedQueryExecutor` with that same runtime. `savepoint()` yields another `Transaction` with the same runtime. There is no transaction-boundary check in the retry loop.
- `tests/python/unit/test_runtime.py:49-62` — retries deadlock on a fake executor; does not cover an open `Transaction` or PostgreSQL `25P02` (in_failed_sql_transaction).

PostgreSQL aborts the entire transaction on deadlock (`40P01`) and serialization failure (`40001`). Retrying the same statement on the pinned aborted connection cannot succeed; further commands fail until rollback. That is a safety gap, not a supported pattern. **Confirm** the draft's diagnosis and the requirement to disable statement retry once a `Transaction` (including savepoint) is open.

**Challenge:** the draft sentence “statement-level retry (today’s `RetryPolicy`) is valid only for autocommit” would bind today’s allowlist, which includes `connection`. A timed-out or dropped client after the server committed a non-idempotent `INSERT`/`UPDATE` can duplicate writes on autocommit retry. Default `retry=None` keeps this latent; ratifying “today’s `RetryPolicy`” as the autocommit contract would make it a supported pattern. W1-B’s SQLSTATE allowlist (deadlock, serialization) is the right transactional API; autocommit statement retry must be the same allowlist, not `connection` / timeout.

### Safe error fields

- `python/ferrum/errors.py:84-97,134-143,218-236,286-413` — `FerrumCompileError` has `model`, `field`, `operator`, `category`. `FerrumIntegrityError` has `constraint`, `category`. Query-path `map_db_error` surfaces `type(exc).__name__` plus constraint name for integrity; it does not copy `.detail`, `.hint`, bound values, or DSNs. `context` is documented for `model` / `operation` and is never applied. No `sqlstate` attribute on `FerrumConnectionError`, `FerrumDatabaseError`, `FerrumTimeoutError`, `FerrumSchemaError`, or `FerrumIntegrityError`.
- `_postgres_ddl_error_detail` (migration path only) reads `sqlstate` and `str(exc)` (PostgreSQL primary message). It does not read `.detail` / `.hint`. `tests/python/unit/test_errors.py:96-113,390-410` assert `DETAIL` / `HINT` sentinels do not appear. Residual: some primary messages contain submitted values (e.g. invalid input syntax).
- `python/ferrum/hooks.py:37-63,66-87,192-211` — default payloads are Tier A keys only. `DEBUG=1` does not elevate. `FERRUM_OBS=C` additionally requires `FERRUM_OBS_ALLOW_TIER_C=1`. `query_failure` carries a Ferrum class name, not SQLSTATE or message text.
- `python/ferrum/echo.py:9-11,26-36` — bound values only under Ferrum-specific `FERRUM_ECHO=debug` / `echo="debug"`. Generic `DEBUG=1` never enables echo.
- `python/ferrum/connection.py:77-91,193-200` — connect failures use `_redacted_dsn_info` (host, port, database, username) plus `type(exc).__name__`. `tests/python/security/test_credential_safety.py` covers this shape.

**Confirm** current query-path redaction vs the draft: query-path mapping already withholds `DETAIL` / `HINT` / values / DSNs; structured `sqlstate` / `model` / `operation` are not yet on mapped exceptions. **Confirm** the sanctioned field set as the W1-D target. W1-D must not promote the migration-path primary message onto query-path exceptions.

### RLS / admin GUCs / schema selection

- `python/ferrum/session.py:32-43,46-61,64-83,113-171` — `ALLOWED_GUC_NAMES` is a closed frozenset. Names not on the list raise `FerrumCompileError` before execute (`tests/python/unit/test_session.py`). `set_config` uses `set_config(name, $1, true)`: name interpolated only after allowlist check (closed literals, including dotted names such as `app.team_id`); value is a bound parameter; `transaction_local=true`. `tenant_transaction` validates names before `conn.transaction()`, then sets GUCs inside the transaction so commit / rollback / cancellation resets them and pooled connections do not leak tenant state.
- `search_path` is **not** in `ALLOWED_GUC_NAMES`. No `schema_transaction` exists.
- `tenant_transaction(..., admin=True)` still requires `tenant_id` and always sets the tenant GUC, then `admin_guc='true'` (`session.py:167-170`, `test_session.py:269-279`). There is no `platform_admin_transaction`. Platform-admin callers must supply a tenant id (fake or real) — the draft correctly rejects that.
- Grep of `python/ferrum/`: no `schema_transaction`, `ShardRouter`, `ConnectionRegistry`, or `platform_admin_transaction`. `call_function` already validates schema / function identifiers with `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$` (`connection.py:396-437`).
- Every QuerySet terminal takes an explicit `ConnectionLike`; no default connection.

**Confirm** the W1-F draft as the security contract, with implementation constraints below. Transaction-local `set_config(..., true)` is the correct reset guarantee for GUCs; `schema_transaction` must use the same transaction-end reset (SET LOCAL / `set_config(..., true)`), never `SET SESSION`.

### Migration apply / ADR-004

- `python/ferrum/migrations/orchestrator.py:8-18,124-138,701-714,1159-1243` — `apply()` defaults `dry_run=True` (prints kind + table only; no DSN, SQL, or token). Destructive gate scans `op["kind"] in _DESTRUCTIVE_KINDS` and does not trust plan JSON `requires_confirmation`. Non-dev requires `confirm=True`. Live apply is `for op in ops: await driver.execute(sql)` with **no** wrapping `BEGIN`/`COMMIT`. Ledger `record_applied` runs after the loop, and only when `confirm and token` are set. Module docstring claims dry-run is structurally required via a `MigrationPlan` object; the API is `apply(conn, plan_json, dry_run=False)` and does not require a prior `dry_run()` result.
- `_DESTRUCTIVE_KINDS` = `drop_table`, `drop_column`, `drop_fk`, `raw_sql`, `drop_extension`, `disable_rls`, `drop_policy`, `drop_function`. **`alter_column` is absent.** `AlterColumn.classification` returns `"destructive"` only for `not_null is True` (`operations.py:173-217`); type narrowing is documented as destructive and is classified `"safe"`. `test_alter_column_set_not_null_is_destructive_class` asserts the Operation class only. JSON `apply()` of `alter_column` SET NOT NULL with `requires_confirmation=False` does not hit MIG-2. Autodiff emits `"requires_confirmation": False` (`orchestrator.py:1150-1156`).
- `python/ferrum/cli/migrate_cmd.py:92-149` — file-based postgres apply uses `op.classification == "destructive"` (would catch SET NOT NULL) and wraps ops + ledger in `async with pool.acquire() as db_conn, db_conn.transaction()`. That is a **different** surface from `orchestrator.apply()`. It is not one-connection advisory locking, tested non-transactional phases, or ADR-004 closure. Non-postgres file apply is still per-op autocommit.
- `_op_to_sql` interpolates `raw_sql` as `op["sql"]` after the confirm gate (`orchestrator.py:614-617`); `raw_sql` is always in `_DESTRUCTIVE_KINDS`. Index `WHERE` fragments are interpolated from plan JSON (`orchestrator.py:582-585`) — developer-authored metadata, not request input; still a string fragment W1-C should validate.
- `tests/python/security/test_migration_safety.py` covers dry-run default, drop_table / drop_column / raw_sql confirm, non-dev confirm, DSN absence in dry-run output, token replay. It does not cover `alter_column` SET NOT NULL or type narrowing on the JSON apply path.
- Unscoped `delete()` / `update()` refuse without `danger_delete_all()` / `danger_update_all()` (`queryset.py:3018-3168`). In scope for §3; not claimed shipped-closed by §5a beyond existing behavior.

**Confirm** ADR-004 remains reopened. **Do not** treat CLI’s per-migration postgres transaction as shipped transactional apply.

## Findings

### High (must fix in §5a before this gate can approve)

1. **Retry draft would bind an unsafe autocommit category set.**
 Cite: `AGENTS.md` §5a retry proposal; `runtime.py:23-24,57,43-44`; `connection.py:375-390,484-491,534-540`.
 Required correction: state that (a) statement retry is forbidden on `Transaction` and `savepoint`; (b) if statement retry remains on autocommit `Connection`, allowed categories are deadlock (`40P01`) and serialization (`40001`) only; (c) `connection` / timeout are not valid statement-retry categories for DML (ambiguous commit → duplicate writes); (d) default remains `retry=None`; (e) safe transactional retry is only W1-B `run_transaction` (fresh transaction per attempt, SQLSTATE allowlist, capped exponential backoff with jitter, honor cancellation/deadline, caller-documented idempotency). Do not write “today’s `RetryPolicy` is valid for autocommit.”

2. **JSON `apply()` destructive gate does not meet §3 for SET NOT NULL / type narrowing; §5a does not record the hole.**
 Cite: `AGENTS.md` §3 “Destructive actions (column/table drop, type narrowing, `NOT NULL` on a populated column) require explicit confirmation”; `orchestrator.py:127-138,1212-1218,1154-1156,1225-1233`; `operations.py:173-217`.
 Required correction: §5a (migration / ADR-004 notes) must state that JSON `apply()` classification is kind-set-based and currently omits `alter_column`; `AlterColumn.classification` omits type narrowing; autodiff plans set `requires_confirmation: False`; W1-C must close this before claiming the destructive gate is complete. CLI `migrate_cmd` classification is not a substitute. ADR-004 stays unshipped.

### Medium (implementation / contract precision; do not claim closed)

3. **`apply()` dry-run is default, not structurally mandatory.** Module docstring (`orchestrator.py:8-10`) claims `apply()` requires a `MigrationPlan` from `dry_run()`. Callers can pass `dry_run=False` on raw plan JSON. W1-C must not document this as a structural gate until it is one.

4. **CLI postgres file-migrate transaction ≠ ADR-004.** `migrate_cmd.py:128-149` wraps one file’s ops + ledger in a driver transaction. `orchestrator.apply()` does not. Non-transactional kinds (`create_extension`, `create_function`) are “future enforcement” only (`orchestrator.py:125-126,139-143`). Partial JSON apply without ledger is possible.

5. **Migration DDL `str(exc)` is a transitional exception, not a sanctioned field.** `_postgres_ddl_error_detail` (`errors.py:218-236`) includes the PostgreSQL primary message. That is outside the proposed sanctioned set. W1-D may keep a schema-level primary message on `FerrumMigrationError` only if it still excludes `.detail` / `.hint` / values; query-path exceptions must stay on structured fields only.

6. **`map_db_error(..., context=)` never attaches `model` / `operation`.** `errors.py:286-296` vs body through `:413`. W1-D must apply allowlisted context keys as attributes, never by interpolating driver text.

### Low

7. **GUC name interpolation is safe only because the allowlist is closed.** `session.py:83`. `schema_transaction` must not add caller-supplied schema names to this `f"SELECT set_config('{name}', ...)"` pattern. Validate with the same identifier regex as `call_function`, bind or quote as a value / quoted ident, use transaction-local `search_path`, test pool reuse after cancel/rollback.

## Decision

`changes_required`

**Retry — confirm diagnosis, amend the proposed contract.** In-transaction statement retry is unsafe and is live today whenever `RetryPolicy` is configured (`Transaction` and `savepoint` inherit `RuntimeConfig.retry`). Disable it in W1-B. Do not ratify today’s `connection` category for autocommit statement retry.

**Safe error fields — confirm.** Current query-path mapping matches the “never DETAIL / HINT / values / rows / DSNs” rule. Structured `sqlstate` / `category` / `model` / `operation` are the correct W1-D target. Do not treat them as shipped.

**RLS / schema / shards — confirm the draft as the security contract.** `tenant_transaction` is transaction-local and allowlisted. `platform_admin_transaction` must not require a fake tenant id. `schema_transaction` must use strict identifier validation, transaction-local `search_path`, and reset-on-end tests (including cancellation and pool reuse). Shard routing stays outside QuerySet. No `platform_scoped` model flag.

**Migration apply — confirm gated, confirm ADR-004 unshipped.** Dry-run default, drop_* / raw_sql confirm, non-dev confirm, and dry-run redaction hold on the JSON path. SET NOT NULL / type narrowing via `alter_column` do not. W1-C owns transactional apply, advisory locking, atomic ledger writes, non-transactional phase semantics, and closing the `alter_column` confirm hole.

### Required §5a corrections (this gate)

1. Retry subsection: forbid statement retry on `Transaction`/`savepoint`; restrict any remaining autocommit statement retry to deadlock + serialization SQLSTATE; exclude `connection`/timeout; keep default `retry=None`; point transactional retry at W1-B only.
2. ADR-004 / migration notes: record JSON `apply()` `_DESTRUCTIVE_KINDS` omitting `alter_column`; record `AlterColumn.classification` omitting type narrowing; state explicitly that CLI file-migrate’s postgres transaction is not ADR-004 closure.

After those two edits, re-invoke SecurityEngineer on the same `run_id` or a new run. Error-field and tenancy drafts need no wording change for this authority, subject to the W1-D / W1-F constraints above.

### Missing tests (do not claim covered; assign to implementing workstreams)

- No test that `RetryPolicy` is a no-op on `Transaction` / `savepoint`, or that a deadlock inside an open TX does not retry into `25P02`.
- No test that mapped query-path exceptions expose `sqlstate` as an attribute (does not exist yet).
- No security test that JSON `apply(..., dry_run=False, confirm=False)` rejects `alter_column` SET NOT NULL and type narrowing.
- No test for `platform_admin_transaction` without a tenant id, or `schema_transaction` search_path reset on commit/rollback/cancel/pool reuse (APIs absent).
- No test that autocommit `connection`-category retry of non-idempotent DML is rejected.

### Recommendations (non-blocking)

- W1-B: make in-TX retry disable structural in `TimedQueryExecutor` (inspect “open transaction” / use a Transaction-specific executor), not documentation-only.
- W1-C: validate index `WHERE` fragments; keep `raw_sql` confirm-gated; do not add new string-fragment SQL.
- W1-F: shard key is a routing input, never an SQL identifier; pools remain independently configured; no implicit multi-DSN QuerySet behavior.
- Observability/echo already honor Ferrum-specific opt-in; preserve that when W1-D adds `sqlstate` to failure payloads — SQLSTATE may appear as a structured exception attribute, not as hook payload text unless explicitly designed as Tier A metadata.

This record grants only the SecurityEngineer gate. It does not substitute for another authority or independent verification.
