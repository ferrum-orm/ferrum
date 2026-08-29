---
task_id: w0-a-architecture-contracts
run_id: 20260821T082329Z
authority: ChiefArchitect
reviewer: chief-architect
reviewed_at: 2026-08-21T08:50:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: approved
scope:
 - Retry scope vs PostgreSQL abort-on-error; W1-B transaction replay; no ADR-004 pre-emption
 - Schema tenancy and sharding (schema_transaction, ConnectionRegistry/ShardRouter, no platform_scoped)
 - Explicit rejections: identity map, implicit lazy I/O, unrestricted SQL
 - Boundary discipline: Python owns pools/transactions/routing/retries; Rust stays pure sync
---

# Named Authority Verdict

## Authority

`ChiefArchitect`

Narrative label for this gate: **Aligned** (maps to `decision: approved`).

This record grants only the ChiefArchitect gate for run `20260821T082329Z`. It does not grant SecurityEngineer, ProductManager, or CodeReviewer clearance. It does not authorize W1-B / W1-C / W1-F implementation. `AGENTS.md` §5a remains labeled UNRATIFIED until SecurityEngineer also records `decision: approved` for this same run.

## Claims reviewed

1. **Retry scope (prior 075800Z must-fix 1).** Statement-level `RetryPolicy` applies only to discrete autocommit reads (`fetch` / `fetchrow` / `fetchval` and QuerySet read terminals that use them); it is disabled on every `Transaction` and savepoint (object-scoped, not “once any transaction is open on this `Connection`”); streams and `execute` are out of scope; this contract does not close ADR-004; the only write-retry story is W1-B `run_transaction(fn, retry=...)`.
2. **Sharding freeze (prior 075800Z must-fix 2).** `ConnectionRegistry` / `ShardRouter` members are independently configured **PostgreSQL** pools; the router resolves a trusted caller-chosen shard key and returns an explicit `Connection` / `Transaction`; QuerySet stays connection-explicit; no dialect-switching Session.
3. **Implicit lazy I/O (prior 075800Z should-fix).** Attribute access never executes a hidden query. Forward relations and reverse M2M raise `FerrumRelationNotLoadedError`. Reverse FK/OTO may return an unbound `QuerySet` that still requires an explicit `ConnectionLike`. Do not force always-raise.
4. **Boundary discipline (`AGENTS.md` §2.1–§2.2, §4).** Pools, transactions, routing, retries, and cancellation live in Python; Rust remains a pure synchronous compiler/codec off the async I/O path.
5. **Out of this ratification (noted, not approved here):** safe-error-field set and the SET NOT NULL / type-narrowing confirm hole (SecurityEngineer / W1-D / W1-C); alpha-to-stable compatibility / §2.6 vs shipped extras (ProductManager resolution A on `20260821T075800Z` — not reopened).

## Evidence

Inspected independently from source at working-tree docs plus runtime under base revision `768ec1f3013f6d0eccd7c8b590ba36b54b12d23e`. The executor log `logs/w0-a-architecture-contracts/20260821T082329Z.md` was read as a claim list only. No Shell commands were run (read-only review; `mise run ci-local` was not re-executed). Prior verdict `reviews/w0-a-architecture-contracts/20260821T075800Z-chief-architect.md` (`decision: changes_required`) is the must-fix baseline and is not rewritten.

### Contracts and product text

- `AGENTS.md` §5a heading remains **UNRATIFIED** (`:120–133`) and names this run (`20260821T082329Z`) as the required ChiefArchitect + SecurityEngineer re-approval. Remaining claims are still not precedent for W1-B / W1-C / W1-F until both verdicts exist.
- `AGENTS.md` §5a Retry scope (`:135–180`) now carries the merged draft proposal, not the prior “autocommit calls on a Connection with no open transaction” sentence.
- `AGENTS.md` §5a Schema tenancy and sharding (`:230–257`) freezes registry membership as PostgreSQL pools with explicit `ConnectionLike` hand-off.
- `AGENTS.md` §5a Explicit rejections (`:295–313`) now states the reverse-descriptor split.
- `AGENTS.md` §2.6 (`:46–50`) and `CLAUDE.md:34–35,63–69` apply ProductManager resolution A as binding product text (PostgreSQL-only production-readiness target; shipped extras remain best-effort, out of P0 gates, not removed). `README.md:27,343–346,379–409` and `CHANGELOG.md:6–11,29–31` match. This authority does not reopen that product call.
- `AGENTS.md` §7 YAGNI (`:354–360`) still points at UNRATIFIED §5a and forbids implementing `ConnectionRegistry` / `ShardRouter` / `schema_transaction` / `platform_admin_transaction` until ChiefArchitect and SecurityEngineer approve the run that froze that wording.
- `CLAUDE.md:63–69` is a pointer only; it does not duplicate the retry contract.

### Retry path (current runtime — still a safety gap, correctly described as such)

- `python/ferrum/runtime.py:1–3` — retries live at the Python await boundary; Rust is not involved.
- `python/ferrum/runtime.py:23–24,57,87` — `_RETRY_CATEGORIES` is `{deadlock, connection, serialization}`; default `RetryPolicy.on` is `{deadlock}`; `RuntimeConfig.retry` defaults to `None`. `ferrum.connect(..., retry=None)` at `python/ferrum/connection.py:637`.
- `python/ferrum/runtime.py:197–221,223–233` — `_execute_with_policy` still wraps `fetch` / `fetchrow` / `fetchval` **and** `execute`. There is still no read/write split in shipped code. §5a now describes this as a known safety gap, not a supported autocommit contract (`AGENTS.md:137–151,162–165`).
- `python/ferrum/connection.py:171–175,375–390,484–491,534–539` — `Transaction` and `savepoint()` still inherit the parent `RuntimeConfig.retry`. §5a now requires object-scoped disable: statements issued through a `Transaction` never retry; `conn` terminals during `async with conn.transaction() as tx` use a different pooled connection (`AGENTS.md:166–169`).
- `python/ferrum/connection.py:588–624` and `runtime.py:151–155` — compiled streams still do not use `_execute_with_policy`. §5a keeps streams/cursors out of statement-retry scope (`AGENTS.md:170–171`).
- §5a explicitly does **not** close ADR-004 via retry and names W1-B `run_transaction` as the only write-retry story (`AGENTS.md:172–180`).

PostgreSQL abort-on-error (`40P01` / `40001` → session aborted until rollback / `25P02`) is unchanged. Retrying one statement on an aborted `Transaction` still cannot succeed. That diagnosis is now matched by the draft, Least Astonishment, Blast Radius, and YAGNI (one write-retry story).

### ADR-004 / migration apply (must stay reopened)

- `python/ferrum/migrations/orchestrator.py:1225–1241` — `apply()` still takes `conn._require_driver()`, then `for op in ops: await driver.execute(sql)` with **no** wrapping `conn.transaction()`. `record_applied(...)` is a separate execute. Autocommit-per-operation, matching `AGENTS.md` §5.
- `_DESTRUCTIVE_KINDS` (`orchestrator.py:127–138`) still omits `alter_column`. Recorded in the SecurityEngineer-owned §5a subsection (`AGENTS.md:182–206`); this authority does not self-clear that surface.
- No `run_transaction` helper exists in `python/ferrum/` (grep: absent). W1-B is not started.

### Tenancy / routing (current runtime)

- Grep of `python/ferrum/` found **no** `schema_transaction`, `ShardRouter`, `ConnectionRegistry`, `platform_admin_transaction`, `run_transaction`, or `platform_scoped`. W1-F is not started.
- `python/ferrum/session.py:32–43` — `ALLOWED_GUC_NAMES` still has no `search_path`. `set_config` remains transaction-local (`session.py:64–80`). `tenant_transaction()` (`session.py:113–169`) still requires a `tenant_id` even in admin mode. The W1-F draft (`AGENTS.md:241–257`) is additive: `platform_admin_transaction()` with no fake tenant id; `schema_transaction` as validated, transaction-local `search_path` on one pinned transaction; sharding as independently configured **PostgreSQL** pools, trusted shard key, explicit `Connection` / `Transaction` hand-off, QuerySet shard-unaware. ProductManager resolution A is explicitly not a license to launder dialect switching (`AGENTS.md:255–257`).
- `search_path` allowlisting / identifier interpolation remains a SecurityEngineer surface (Low finding 7 in `20260821T075800Z-security-engineer.md`). Not self-cleared here.

### Explicit rejections (current runtime)

- Identity map: no session-level identity cache. `relations.py:71–82` is a per-instance `__ferrum_relations__` dict for already-eager-loaded relations. §5a (`:300–302`) matches.
- Implicit lazy I/O: `relations.py:90–108` (forward) and `:121–125` (reverse M2M) raise `FerrumRelationNotLoadedError`. Reverse non-M2M `:126–131` returns an unbound `QuerySet.filter(...)` with **no I/O**. §5a (`:303–309`) now states this split and forbids converting reverse FK/OTO accessors into always-raise.
- Unrestricted SQL: no `.raw()` / `.extra()`. §5a (`:310–313`) restates §2.9.

### Boundary / Rust

- Retry, pools, transactions, GUC helpers, and the proposed `ShardRouter` remain Python-owned. W0-A did not change IR (`QuerySet._IR_VERSION` is still cited as `4`). W1-F must not move routing or retry into `crates/`.

## Findings

### Prior must-fix items — now correctly stated (no remaining CA wording defect)

1. **Retry scope.** `AGENTS.md:156–180` now requires discrete autocommit **reads** only (`fetch` / `fetchrow` / `fetchval` + QuerySet read terminals); default `retry=None`; autocommit writes (`execute`, QuerySet `create` / `update` / `delete` / `upsert`, DDL) must not statement-retry; disable on every `Transaction` and savepoint, **object-scoped**; streams/cursors out of scope; does **not** close ADR-004; W1-B `run_transaction` is the only write-retry story, with no special case for autocommit `execute` deadlocks. The live `TimedQueryExecutor` wrapping of `execute` and `Transaction` inheritance of `retry` is described as a safety gap, not a supported contract. **Satisfies 075800Z must-fix 1.**

2. **Sharding freeze.** `AGENTS.md:248–257` freezes `ConnectionRegistry` / `ShardRouter` as independently configured **PostgreSQL** pools, trusted caller shard key, explicit `Connection` / `Transaction` return, QuerySet connection-explicit, no dialect-switching Session, resolution A not a laundering license. **Satisfies 075800Z must-fix 2.** Schema Evolution / §2.6 collision is closed in the draft.

3. **Lazy I/O.** `AGENTS.md:303–309` now matches `relations.py:90–131`. Least Astonishment for W2 relation work is preserved. **Satisfies 075800Z should-fix.**

### Non-blocking (do not reopen this gate)

4. **Retry bullet 2 “for DML” qualifier** (`AGENTS.md:160–163`) is slightly redundant once remaining statement retry is reads-only, but it is the SecurityEngineer-required duplicate-write wording and does not broaden CA’s reads-only scope. Leave it for the sibling gate.
5. **`docs/architecture.md` ADR-004** still lacks reopened/gap language (unowned residual from 075800Z). Coordinator follow-up after ratification; not a W0-A contract defect.
6. **Shipped runtime is unchanged.** This approval is of the **draft contract**, not of current retry/apply/tenancy behavior. W1-B must still make in-transaction retry disable structural; W1-C still owns ADR-004; W1-F still owns schema/shard helpers. None of those workstreams may start against this record alone.

### Affirmed (architecture; not self-cleared security)

7. **`schema_transaction` + `platform_admin_transaction` + reject `platform_scoped`.** Aligned with plan W1-F. Access control stays at the transaction/session boundary. `schema_transaction` must use strict identifier validation and transaction-local `search_path` (never session-level `SET`) so pool reuse cannot leak schema (Blast Radius). Adding `search_path` to `ALLOWED_GUC_NAMES` is a SecurityEngineer surface.
8. **Identity map and unrestricted SQL rejections.** Aligned with plan and shipped code.
9. **Boundary discipline.** Retry, pools, transactions, GUC session helpers, and future `ShardRouter` are Python. Rust stays pure sync.
10. **ADR-004.** Current `apply()` loop matches the reopened contract. Do not implement W1-C in this task; do not describe per-op autocommit as the target end state.

### Escalations (do not self-clear)

- **SecurityEngineer (required, sibling gate on this same run):** statement-retry category set (`40P01` / `40001` only; exclude `connection` / timeout); ADR-004 / `alter_column` confirm hole; RLS / `platform_admin` GUCs; `schema_transaction` identifier / `search_path` selection; GUC name interpolation in `set_config`. This verdict does not approve those surfaces.
- **ProductManager:** resolution A on `20260821T075800Z` remains binding. Not reopened.
- **CodeReviewer:** not this record. Coordinator may request a fresh pass on this run’s docs diff; 075800Z was `approved` with warnings that this apply addressed.
- **CEO:** none. No new board-level technology choice.

## Decision

`approved`

The 075800Z ChiefArchitect must-fix list is now correctly stated in `AGENTS.md` §5a for run `20260821T082329Z`:

1. Statement retry is discrete autocommit reads only; object-scoped disable on every `Transaction` / savepoint; streams and `execute` excluded; ADR-004 is not closed via retry; W1-B `run_transaction` is the only write-retry story.
2. `ConnectionRegistry` / `ShardRouter` is PostgreSQL shard routing with a trusted shard key and explicit `ConnectionLike` hand-off; no dialect-switching Session.
3. Lazy I/O rejection preserves reverse FK/OTO unbound `QuerySet` accessors.

This record does **not** make §5a binding by itself. The UNRATIFIED heading must remain until SecurityEngineer records `decision: approved` for **this** run. Do not start W1-B / W1-C / W1-F implementation from this verdict.

This record grants only the named authority's gate. It does not substitute for SecurityEngineer, ProductManager, CodeReviewer, or independent verification.
