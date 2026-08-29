---
task_id: w0-a-architecture-contracts
run_id: 20260821T075800Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-21T08:12:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: changes_required
scope:
 - Retry scope vs PostgreSQL abort-on-error; W1-B transaction replay; no ADR-004 pre-emption
 - Schema tenancy and sharding (schema_transaction, ConnectionRegistry/ShardRouter, no platform_scoped)
 - Explicit rejections: identity map, implicit lazy I/O, unrestricted SQL
 - Boundary discipline: Python owns pools/transactions/routing/retries; Rust stays pure sync
---

# Named Authority Verdict

## Authority

`ChiefArchitect`

Narrative label for this gate: **Needs adjustments** (maps to `decision: changes_required`).

This record does not grant SecurityEngineer, ProductManager, or CodeReviewer clearance.

## Claims reviewed

1. **Retry scope (W0-A / W1-B).** Statement-level `RetryPolicy` is valid only for safe autocommit reads on a `Connection` with no open transaction; once a `Transaction` is open, statement retry must be disabled; transactional retry is whole-callback replay via `run_transaction(fn, retry=...)`. Confirm or challenge against PostgreSQL abort-on-error / `25P02`. Must not close or substitute for ADR-004.
2. **Schema tenancy and sharding (W1-F).** `schema_transaction` with validated identifiers and transaction-local `search_path`; optional `ConnectionRegistry`/`ShardRouter` with QuerySet remaining connection-explicit; reject `platform_scoped` model flag.
3. **Explicit rejections.** Identity map, implicit lazy I/O, unrestricted SQL remain non-goals without a new architecture review.
4. **Boundary discipline (`AGENTS.md` §2.1–§2.2, §4).** Pools, transactions, routing, retries, and cancellation live in Python; Rust remains a pure synchronous compiler/codec off the async I/O path.
5. **Out of this ratification (noted, not approved here):** safe-error-field set (SecurityEngineer / W1-D); alpha-to-stable compatibility / §2.6 vs shipped extras (ProductManager).

## Evidence

Inspected independently from source. The executor log `logs/w0-a-architecture-contracts/20260821T075800Z.md` was read as a claim list only. No Shell commands were run (read-only review; `mise run ci-local` was not re-executed).

### Contracts and plan

- `AGENTS.md` §2.1–§2.2 (Python ergonomics / Rust pure sync), §2.6 (PostgreSQL-only invariant), §2.9 (no raw SQL), §5 ADR-004 reopened, §5a unratified drafts.
- `CLAUDE.md` §5a pointer: drafts are not binding until named `decision: approved`.
- `README.md:46-50` compatibility blockquote: disputed thin-parity wording left unchanged pending ProductManager.
- Approved plan `.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md:49-53,79-84,117-128,169-179`.
- `PROTOCOL.md` named-authority stop; `reviews/TEMPLATE.md` record shape.
- Task contract `.agent-work/production-readiness/tasks/w0-a-architecture-contracts.md` lists security surfaces `migration_apply`, `errors_redaction`, `rls_admin_gucs`, `schema_selection`.

### Retry path (current runtime)

- `python/ferrum/runtime.py:1-3` — retries live at the Python await boundary; Rust is not involved.
- `python/ferrum/runtime.py:23-24` — `_RETRY_CATEGORIES` is `{deadlock, connection, serialization}`. Default `RetryPolicy.on` is `{deadlock}` (`:57`); `RuntimeConfig.retry` defaults to `None` (`:87`). `ferrum.connect(..., retry=None)` at `python/ferrum/connection.py:637`.
- `python/ferrum/runtime.py:197-221` — `_execute_with_policy` retries any wrapped op when `retry.should_retry` is true. `TimeoutError` from `asyncio.timeout` is mapped to `FerrumTimeoutError` and is **not** retried (`:213-216`).
- `python/ferrum/runtime.py:223-233` — `fetch`, `fetchrow`, `fetchval`, **and** `execute` all go through `_run` → `_execute_with_policy`. There is no read/write split.
- `python/ferrum/connection.py:171-175` — `Connection._require_driver()` wraps the pool driver in `TimedQueryExecutor` with the connection `RuntimeConfig` (including `retry`).
- `python/ferrum/connection.py:378,387` and `:475,487-491` — `Transaction` is constructed with `runtime=self._runtime` and `_require_driver()` builds another `TimedQueryExecutor` with that same config. Statement retry therefore fires on a pinned transaction.
- `python/ferrum/connection.py:534-539` — `Transaction.savepoint()` yields a nested `Transaction` with the same `runtime`, so statement retry also applies inside savepoints.
- `python/ferrum/connection.py:588-624` and `runtime.py:151-155` — compiled streams do **not** use `_execute_with_policy`; chunk `__anext__` calls the driver directly. Streaming is currently non-retrying.

PostgreSQL semantics that the draft must match: after any error in an open transaction the session is aborted until `ROLLBACK` / `ROLLBACK TO SAVEPOINT` (`25P02`). Deadlock (`40P01`) and serialization failure (`40001`) abort the whole transaction. Retrying the same statement on the same `Transaction` cannot succeed. Safe retry of transactional work requires a new `BEGIN` and replay of the entire callback (plan W1-B). Autocommit reads may retry a discrete statement because each statement is its own transaction. Autocommit **writes** (`execute`) plus a `connection`-category retry (`runtime.py:23-24,41-44`) can double-apply if the original statement committed and the client saw a disconnect/timeout. That is why the approved plan (`plan.md:80,123`) says **safe autocommit reads**, not “any autocommit call.”

`AGENTS.md` §5a currently says: “statement-level retry … is valid only for autocommit calls made directly on a `Connection` with no open transaction.” That is broader than the plan and broader than this gate.

### ADR-004 / migration apply (must stay reopened)

- `python/ferrum/migrations/orchestrator.py:1225-1241` — `apply()` takes `conn._require_driver()`, then `for op in ops: await driver.execute(sql)` with **no** `conn.transaction()` (or equivalent) wrapping the loop. `record_applied(...)` runs after the loop as a separate execute. This is autocommit-per-operation, matching `AGENTS.md` §5’s reopened ADR-004 text. W1-C owns one-connection transactionality, advisory locking, and atomic ledger writes. Statement retry must not be used as a substitute for that contract.

### Tenancy / routing (current runtime)

- Grep of `python/ferrum/` found **no** `schema_transaction`, `ShardRouter`, `ConnectionRegistry`, `platform_admin_transaction`, or `platform_scoped`.
- `python/ferrum/session.py:32-43` — `ALLOWED_GUC_NAMES` includes `app.team_id`, `app.platform_admin`, `ferrum.tenant_id`, `ferrum.admin`, plus timeouts/`work_mem`/`application_name`. **`search_path` is not allowlisted.**
- `python/ferrum/session.py:64-83` — `set_config` validates the GUC name, then `SELECT set_config('<name>', $1, true)` (transaction-local). Value is bound; name is allowlisted then interpolated.
- `python/ferrum/session.py:113-171` — `tenant_transaction()` opens `conn.transaction(...)`, binds tenant GUC, optionally sets admin GUC. Admin mode still requires a `tenant_id`. A dedicated `platform_admin_transaction()` that sets only allowlisted admin GUCs and needs no fake tenant id is therefore additive, not a model flag.
- `python/ferrum/connection.py:55-74,432-437` — `_validate_pg_identifier` already exists for `call_function` schema/function names (quoted identifiers, bound args). `schema_transaction` should reuse this class of validation for `search_path`, not a new string-fragment API.
- `python/ferrum/queryset.py:1103` (`all`), `:1306` (`get`), and `:3043-3046` (`delete` rejects `conn is None`) — terminals take an explicit `ConnectionLike`. No implicit/global connection.

### Explicit rejections (current runtime)

- Identity map: grep of `python/ferrum/` found no session-level identity cache. `relations.py:71-82` is a **per-instance** `__ferrum_relations__` dict for already-eager-loaded relations, not a SQLAlchemy identity map.
- Implicit lazy I/O: `relations.py:90-97` (`get_loaded_relation`) and forward descriptor `:105-108` raise `FerrumRelationNotLoadedError`. Reverse M2M `:121-125` also raises. Reverse non-M2M `:126-131` returns an unbound `QuerySet` filtered by FK — **no query is executed**; a later terminal still requires `conn`. §5a’s claim that “the relation descriptor `__get__` path raise[s] `FerrumRelationNotLoadedError`” (`:118-125`) is true for M2M/forward, overstated as a universal rule.
- Unrestricted SQL: grep of `python/ferrum/` found no `raw()` / `extra()`. Identifiers are allowlisted (`queryset` compile path, `session.py` GUC names, `connection.py` `_validate_pg_identifier`). Values are bound parameters.

### Boundary / Rust

- Grep of `crates/` found no `tokio`, `async fn`, `RetryPolicy`, `search_path`, or connection-pool types. Query compilation/hydration remains `queryset.py` → `_native.compile_query` / `hydrate_rows` (`queryset.py:65` `_IR_VERSION = 4`; `:1060,618-622`). W0-A drafts do not change the IR.

### Residual docs (not in W0-A owned paths; do not block this wording pass)

- `docs/architecture.md:197` still states ADR-004 as “Transactional by default…” with no reopened/gap language. Stale relative to `AGENTS.md` §5 and `orchestrator.apply()`. Coordinator follow-up after this contract is ratified; do not treat that file as current ADR status.

## Findings

### Must fix before ChiefArchitect approval (this gate)

1. **Retry scope in `AGENTS.md` §5a is too broad vs the approved plan and PostgreSQL write hazards.**
 - **Severity:** high (contract defect; would mis-scope W1-B).
 - **Evidence:** plan `ferrum-production-readiness_6b5f422d.plan.md:80,123`; `runtime.py:223-233` wrapping `execute`; `_RETRY_CATEGORIES` includes `connection` (`runtime.py:23-24`).
 - **Required correction to §5a Retry scope** (exact contract to ratify):
 - Statement-level `RetryPolicy` may apply only to **discrete autocommit read** terminals on a `Connection` (`fetch` / `fetchrow` / `fetchval` and QuerySet read terminals that use them). Default remains `retry=None`.
 - Autocommit **writes** (`execute`, QuerySet `create`/`update`/`delete`/`upsert`, DDL) must **not** statement-retry. A `connection`-category failure after an unknown commit is a duplicate-write hazard (Blast Radius). Write retries belong only to W1-B `run_transaction(fn, retry=...)` with a fresh transaction per attempt, allowlisted SQLSTATE (`40001` / `40P01` unless SecurityEngineer narrows further), capped backoff+jitter, cancellation/deadline honored, and caller-documented idempotency.
 - Disable statement retry on every `Transaction` and savepoint-wrapped `Transaction` (`connection.py:378,387,487-491,534-539`). Wording must be **object-scoped** (“statements issued through a `Transaction`”), not “once any transaction is open on this `Connection`” — `conn` terminals during `async with conn.transaction() as tx` are a different pooled connection.
 - Streams/cursors (`_open_compiled_stream` / `ManagedChunkStream`) stay out of statement-retry scope; mid-stream retry is not a safe read.
 - This contract does **not** close ADR-004. W1-C remains the only owner of migration transactionality. `orchestrator.apply()` must not grow statement-retry as a stand-in for a migration-spanning transaction and atomic ledger write (`orchestrator.py:1227-1241`).
 - Least Astonishment / YAGNI: one write-retry story (`run_transaction`), not a special case for autocommit `execute` deadlocks.

2. **`ConnectionRegistry`/`ShardRouter` must be specified as PostgreSQL shard routing, not a multi-database Session.**
 - **Severity:** medium (Schema Evolution / §2.6 collision).
 - **Evidence:** `AGENTS.md` §2.6; plan `:51-53,178`; §5a already says “no implicit multi-DB behavior” but does not freeze registry membership.
 - **Required correction to §5a sharding bullet:** registry members are independently configured **PostgreSQL** pools. The router resolves a **trusted** shard key chosen by caller/router code and returns an explicit `Connection`/`Transaction`. QuerySet stays shard-unaware and connection-explicit. No implicit connection selection from model metadata, tenant id, or schema name. No dialect-switching Session. ProductManager still owns whether shipped MySQL/SQLite/MSSQL extras remain; W1-F must not use the registry to launder that decision.

### Should fix in the same §5a wording pass (non-blocking for the rejections themselves)

3. **Implicit lazy I/O wording overstates the reverse-descriptor path.**
 - **Severity:** low (Least Astonishment for W2 relation work).
 - **Evidence:** `relations.py:90-108` raise; `:121-125` reverse M2M raises; `:126-131` reverse FK/OTO returns an unbound `QuerySet` with no I/O.
 - **Required correction:** reject “attribute access executes a hidden query.” Forward relations and reverse M2M raise `FerrumRelationNotLoadedError`. Reverse FK/OTO may return an unbound `QuerySet` that still requires an explicit `ConnectionLike` terminal. Do not “fix” reverse accessors into always-raise as part of this rejection.

### Affirmed (no architecture objection; not self-cleared)

4. **`schema_transaction` + `platform_admin_transaction` + reject `platform_scoped`.** Aligned with plan W1-F and Ticket Analyzer / Org AI tenancy. Access control stays at the transaction/session boundary. `schema_transaction` must use strict identifier validation (same class as `_validate_pg_identifier`) and **transaction-local** `search_path` (`SET LOCAL` / `set_config(..., true)`), never session-level `SET`, so pool reuse cannot leak schema (Blast Radius). `search_path` is not in `ALLOWED_GUC_NAMES` today (`session.py:32-43`); adding it is a SecurityEngineer surface, not a ChiefArchitect self-clear.
5. **Identity map rejection.** Aligned with plan `:52` and shipped code. Per-instance relation cache is not an identity map.
6. **Unrestricted SQL rejection.** Aligned with §2.9. Close consumer gaps with typed ORM ops, allowlisted helpers, or `call_function`.
7. **Boundary discipline.** Retry, pools, transactions, GUC session helpers, and future `ShardRouter` are Python. Rust stays pure sync. W1-F must not move routing or retry into `crates/`.
8. **ADR-004.** Current `apply()` loop matches the reopened contract. Do not implement W1-C in this task; do not describe per-op autocommit as the target end state.

### Escalations (do not self-clear)

- **SecurityEngineer (required):** statement vs transaction retry; RLS / `platform_admin` GUCs; `schema_transaction` identifier/`search_path` selection; GUC name interpolation in `set_config`. This verdict does not approve those surfaces.
- **ProductManager (required):** §2.6 vs shipped MySQL/SQLite/MSSQL extras and alpha-to-stable compatibility. ChiefArchitect does not pick (a) vs (b). Finding 2 only constrains W1-F so it cannot pre-empt that call.
- **CodeReviewer:** still required by the task contract; not this record.
- **CEO:** none. No new board-level technology choice.

## Decision

`changes_required`

The draft is **architecturally close** and does **not** block the direction of W1-B / W1-F: PostgreSQL abort-on-error makes in-transaction statement retry a known safety gap; transactional replay belongs in W1-B; schema tenancy is validated `search_path` on a pinned transaction; sharding is caller-chosen PostgreSQL pools with connection-explicit QuerySet; `platform_scoped`, identity map, implicit lazy I/O, and unrestricted SQL are rejected; ADR-004 stays reopened for W1-C; Rust stays off the I/O path.

It is **not** `approved` until `AGENTS.md` §5a (and the `CLAUDE.md` pointer, which must keep mirroring rather than duplicating) is edited to:

1. Narrow statement retry to **safe autocommit reads** only, disable it on `Transaction`/savepoint executors, exclude streams and `execute`, and explicitly refuse ADR-004 closure via retry.
2. Freeze `ConnectionRegistry`/`ShardRouter` as PostgreSQL shard routing with explicit `ConnectionLike` hand-off.
3. Tighten the lazy-I/O sentence so reverse FK/OTO QuerySet accessors are not later “corrected” into always-raise.

After that wording lands on the W0-A shared-path lease, re-request this authority. W1-B / W1-F implementation must not start against the current broader §5a retry sentence.

This record grants only the named authority's gate. It does not substitute for SecurityEngineer, ProductManager, CodeReviewer, or independent verification.
