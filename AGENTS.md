# AGENTS.md — Coding Agent Guidance for Ferrum

> Authoritative guidance for AI coding agents (Cursor and any `AGENTS.md`-aware tool)
> working in this repository. Claude Code reads `CLAUDE.md`, which defers to this file
> for the shared rules below. Treat this document as the single source of truth for
> how to build Ferrum. When this file and a deeper `.cursor/rules/*` file disagree, the
> more specific rule wins; otherwise this file governs.

## 1. What Ferrum is

Ferrum is a **next-generation async ORM for Python** with a **Rust-powered core**,
**Pydantic v2-native models**, and a **Django-inspired developer experience**. It targets
modern async Python services (FastAPI / Starlette) that need type-safe, observable,
PostgreSQL-backed persistence without a synchronous compatibility layer.

Read these before substantial work:

- `AGENTS.md` — authoritative architecture, security, ADR, and engineering contract.
- `README.md` — public API and product positioning.
- `CHANGELOG.md` — shipped behavior and compatibility history.
- Source plus tests — authority for current implementation behavior.
- `.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md` — approved scope and
  dependencies when working on production readiness.

`.claude/docs/` currently contains individual ADR material only. Do not follow stale
pointers to absent product/architecture/security/design documents.

## 2. Non-negotiable architectural constraints

These are product- and architecture-level invariants. **Do not violate them, and do not
"temporarily" violate them to make something compile.** If a task seems to require breaking
one, stop and surface it (see §9 Escalation).

1. **Python owns public developer ergonomics.** The public API, model definitions, QuerySet
   surface, async runtime, connection pool, transactions, and hook dispatch live in Python.
2. **Rust owns performance-critical internals only.** The Rust core is a **pure, synchronous,
   stateless compiler/codec**: `QuerySet IR → {sql_text, bound_params, param_type_summary}` and
   `raw rows → hydrated payload`. Rust stays **off the async I/O path**.
3. **Async-first only.** Every core API is awaitable. **No synchronous API, sync wrapper, or
   blocking compatibility layer** in the v0.1 MVP.
4. **Pydantic v2 first.** Model definitions are the single source of truth for validation,
   serialization, and persistence. No duplicate persistence schemas.
5. **PyO3 + maturin** are the Rust↔Python bridge and build tool. The boundary maps Rust
   `Result::Err` and panics to **catchable Python exceptions** — never a process abort,
   memory address, or local path leak.
6. **PostgreSQL is the only production-readiness and consumer-pilot target.** Shipped
   `mysql`/`sqlite`/`mssql` extras are best-effort thin parity, out of P0 release gates,
   and must not shape PostgreSQL SQL correctness, type fidelity, migration
   transactionality, error taxonomy, pool behavior, or tenancy APIs. Do not remove those
   extras as part of v0.1 production-readiness.
7. **No ORM feature ships without tests.** A feature without tests is not done.
8. **Public API changes require documentation updates** in the same change.
9. **No raw SQL escape hatches** (no `extra()`, string fragments, user-supplied templates, or
   production-exposed query inspection). SQL identifiers resolve only from model-metadata
   **allowlists**; values are emitted only as **bound parameters**.
10. **No per-request mutable shared state in Rust.** Model metadata is built once at class
    definition time and is thereafter read-only. Compilation is a pure function over
    `(&Metadata, QuerySetIR)` producing fresh owned output per call.

## 3. Security rules (release-qualification gates, not suggestions)

Ferrum treats SQL compilation, observability payloads, error surfaces, and migration
execution as product-level security scope. Any change touching these MUST keep these true,
and they MUST be covered by tests:

- **SQL safety:** user input is never interpolated into SQL identifier or value positions.
  Unknown fields, unsupported operators, and invalid sort directions fail with structured
  errors **before** SQL is emitted.
- **Credential handling:** connection strings, passwords, and secrets never appear in default
  hook payloads, exceptions, logs, or migration dry-run/apply output. Connection diagnostics
  are limited to an allowlist (host, port, database, username, error category) — never the
  password or full DSN.
- **Tiered observability:** default hook payloads are **Tier A only** (query fingerprint,
  operation/model metadata, duration, status, failure category). Bound parameter values never
  appear in default payloads under any key. Tier B (normalized SQL) and Tier C (full SQL +
  bound values) require **Ferrum-specific opt-in** and must never activate from a generic
  `DEBUG=1`. Tier C is local-dev only and never safe for APM, centralized logs, or production.
- **Error boundaries:** database errors map to a stable, sanitized Ferrum taxonomy. Raw
  PostgreSQL `DETAIL`/`HINT` containing row data is not exposed by default. PyO3 panics become
  catchable Python exceptions.
- **Migration safety:** dry-run is mandatory before apply. Destructive actions (column/table
  drop, type narrowing, `NOT NULL` on a populated column) require explicit confirmation.
  Non-development applies require explicit environment confirmation. Unscoped `delete()` /
  `update()` must require a named danger API (`danger_delete_all()` / `danger_update_all()`)
  and fail by default.

**Any change to auth, secrets, SQL compilation, or migration apply must be flagged for
SecurityEngineer review.** Do not self-clear security-sensitive changes.

## 4. The PyO3 boundary (how Python and Rust interact)

- The IR crossing the boundary is a **typed, versioned, serializable contract**. Values are
  carried **out-of-band from identifiers** so parameterization and allowlisting are structural,
  not convention.
- The Rust compile call is **synchronous and holds the GIL** — compilation is CPU-bound and
  sub-millisecond; do not release/reacquire the GIL for it, and do not put cancellable waiting
  inside Rust. All cancellation/timeout handling lives in Python at the driver await point.
- Build with `panic = "unwind"` for the extension; wrap the boundary so a Rust panic surfaces
  as a catchable Python exception. Error payloads carry **structured fields** (model, field,
  operator, category) — never formatted trace blobs.
- Hydration: Rust constructs typed payloads from trusted DB-origin rows. The default uses the
  Pydantic v2 **construct-without-revalidate** fast path (DB already enforced types). Document
  the trusted-source assumption and the custom-validator caveat (see ADR-003).

## 5. Architecture decisions (ADRs)

The original six ADRs established the implementation choices below. ADR-004 is reopened because
the current migration executor does not yet satisfy its transactional contract.

- **ADR-001** ✅ Resolved — Python-side `asyncpg` driver (`ferrum.drivers.postgres`; install with `ferrum-orm[pg]`).
- **ADR-002** ✅ Resolved — IR v2 JSON contract (`crates/ferrum-core/src/ir/`); version field in `QuerySet._IR_VERSION`.
- **ADR-003** ✅ Resolved — construct-without-revalidate fast path (`queryset._hydrate_rows`, `model_construct`); custom-validator caveat documented.
- **ADR-004** ⚠ Reopened — target remains transactional-by-default with explicit
  non-transactional phases, but current `orchestrator.apply()` executes operations separately.
  Production-readiness workstream W1-C must add one-connection transactionality, advisory
  locking, atomic ledger writes, and tested non-transactional failure semantics before closure.
- **ADR-005** ✅ Resolved — maturin + cibuildwheel abi3 wheels; `release.yml` builds and publishes to PyPI on `v*` tag push via OIDC trusted publishing.
- **ADR-006** ✅ Resolved — centralized redaction layer in `errors.py` (`map_db_error`/`map_native_error`); Tier A/B/C hooks in `hooks.py`.

## 5a. Wave 0 architecture and compatibility contracts (ratified)

> This section is the **ratified** W0-A contract. Named authorities recorded
> `decision: approved` at:
>
> - ProductManager — `.agent-work/production-readiness/reviews/w0-a-architecture-contracts/20260821T075800Z-product-manager.md` (resolution A)
> - ChiefArchitect — `.agent-work/production-readiness/reviews/w0-a-architecture-contracts/20260821T082329Z-chief-architect.md`
> - SecurityEngineer — `.agent-work/production-readiness/reviews/w0-a-architecture-contracts/20260821T082329Z-security-engineer.md`
> - CodeReviewer — `.agent-work/production-readiness/reviews/w0-a-architecture-contracts/20260821T082329Z-code-reviewer.md`
>
> The contract is binding for subsequent Wave 1 work. It does **not** claim that live
> retry, migration-apply, error-field, or tenancy **code** already matches it.
> Implementation remains W1-B (`run_transaction` / statement-retry disable), W1-C
> (ADR-004 / destructive confirm), W1-D (structured `sqlstate`/`category`), and W1-F
> (`schema_transaction` / `platform_admin_transaction` / `ConnectionRegistry` /
> `ShardRouter`). Do not start those workstreams from this heading flip. ADR-004
> remains reopened in §5.

### Retry scope

Current runtime: `TimedQueryExecutor._execute_with_policy` (`python/ferrum/runtime.py:197-221`)
applies the connection's opt-in `RetryPolicy` (default `None`, i.e. no retries) to every
individual `fetch`/`fetchrow`/`fetchval`/`execute` call (`runtime.py:223-233`). Allowed
categories today are `{deadlock, connection, serialization}` (`runtime.py:23-24`); default
`RetryPolicy.on` is `{deadlock}` (`runtime.py:57`). `_exception_category` maps
`TimeoutError` / `ConnectionError` / `OSError` to `connection` (`runtime.py:43-44`).
`Transaction` is constructed with the parent `Connection`'s `RuntimeConfig` — including
`retry` — (`python/ferrum/connection.py:378`, `:387`, `:487-491`); `Transaction.savepoint()`
yields another `Transaction` with the same runtime (`connection.py:534-539`). A configured
`RetryPolicy` therefore fires for statements issued through a pinned `Transaction` or
savepoint. Compiled streams (`_open_compiled_stream` / `ManagedChunkStream`) do **not** use
`_execute_with_policy`. PostgreSQL aborts the entire transaction after a deadlock (`40P01`)
or serialization failure (`40001`); further commands fail until rollback (`25P02`). Retrying
one statement on that aborted `Transaction` cannot succeed — the retry loop has no
transaction-boundary awareness. This is a known safety gap, not a supported pattern.

Ratified contract:

- Statement-level `RetryPolicy` may apply only to **discrete autocommit reads** issued
  through a `Connection` object: `fetch` / `fetchrow` / `fetchval` and QuerySet read
  terminals that use them. Default remains `retry=None`.
- If that remaining autocommit statement retry is enabled, allowed categories are
  deadlock (`40P01`) and serialization failure (`40001`) only. `connection` and timeout
  are **not** valid statement-retry categories for DML (a client timeout or drop after the
  server committed can duplicate writes). Do **not** treat today's `RetryPolicy` (which
  includes a `connection` category and wraps `execute`) as a supported autocommit contract.
- Autocommit **writes** (`execute`, QuerySet `create` / `update` / `delete` / `upsert`,
  DDL) must **not** statement-retry.
- Disable statement retry on every `Transaction` and savepoint-wrapped `Transaction`.
  This is **object-scoped**: statements issued through a `Transaction` never retry. It is
  not “once any transaction is open on this `Connection`” — `conn` terminals during
  `async with conn.transaction() as tx` use a different pooled connection.
- Streams and cursors stay out of statement-retry scope; mid-stream retry is not a safe
  read.
- This contract does **not** close ADR-004. W1-C remains the only owner of migration
  transactionality. `orchestrator.apply()` must not grow statement-retry as a stand-in for
  a migration-spanning transaction and atomic ledger write
  (`python/ferrum/migrations/orchestrator.py:1225-1241`).
- The **only write-retry story** is plan workstream W1-B `run_transaction(fn, retry=...)`:
  open a fresh transaction per attempt, replay the entire callback, restrict retries to
  allowlisted SQLSTATE (`40001` / `40P01`), use capped exponential backoff with jitter,
  honor cancellation/deadline, and require the callback to document its own idempotency.
  There is no special case for autocommit `execute` deadlocks.

### ADR-004 / migration apply notes

ADR-004 remains reopened (§5). Current JSON `orchestrator.apply()`
(`python/ferrum/migrations/orchestrator.py:1225-1241`) takes `conn._require_driver()`, then
`for op in ops: await driver.execute(sql)` with **no** wrapping `conn.transaction()`.
`record_applied(...)` runs after the loop as a separate execute. That is
autocommit-per-operation, not the transactional-by-default target.

JSON `apply()` destructive classification is kind-set-based and currently **omits**
`alter_column`: `_DESTRUCTIVE_KINDS` (`orchestrator.py:127-138`) is `drop_table`,
`drop_column`, `drop_fk`, `raw_sql`, `drop_extension`, `disable_rls`, `drop_policy`,
`drop_function`. `AlterColumn.classification` (`python/ferrum/migrations/operations.py:173-217`)
returns `"destructive"` only for `not_null is True`; type narrowing is documented as
destructive in the class docstring and is classified `"safe"`. Autodiff plans emit
`"requires_confirmation": False` (`orchestrator.py:1150-1156`). JSON `apply()` of
`alter_column` SET NOT NULL with `requires_confirmation=False` therefore does not hit the
MIG-2 confirm gate. W1-C owns closing this SET NOT NULL / type-narrowing confirm hole
before claiming the §3 destructive gate is complete.

CLI file-based postgres apply (`python/ferrum/cli/migrate_cmd.py:92-149`) uses
`op.classification == "destructive"` (would catch SET NOT NULL) and wraps ops + ledger in
`async with pool.acquire() as db_conn, db_conn.transaction()`. That is a **different**
surface from `orchestrator.apply()`. It is not one-connection advisory locking, tested
non-transactional phases, or ADR-004 closure. Non-postgres file apply is still per-op
autocommit. Do not treat the CLI postgres transaction as shipped ADR-004.

### Safe error fields

Current runtime: some Ferrum exceptions already carry structured, safe-to-log attributes —
`FerrumCompileError.{model, field, operator, category}` (`python/ferrum/errors.py:84-97`) and
`FerrumIntegrityError.{constraint, category}` (`python/ferrum/errors.py:134-143`). `map_db_error`
(`python/ferrum/errors.py:286-413`) otherwise folds driver exceptions into a message containing
only `type(exc).__name__`; `_postgres_ddl_error_detail`
(`python/ferrum/errors.py:218-236`, migration path only) additionally reads a `SQLSTATE` code and
the top-level PostgreSQL message string — never `DETAIL`/`HINT` or bound values. There is
currently **no** general `sqlstate` attribute on query-path exceptions
(`FerrumConnectionError`, `FerrumDatabaseError`, `FerrumTimeoutError`, etc.); SQLSTATE only
appears as message text, and only for migration DDL failures.

Ratified contract: the sanctioned safe-error-field set for every Ferrum exception is exactly
`sqlstate` (a structured attribute on every mapped exception, not just migration message text),
`category` (a stable string from a closed enum), `constraint` (DB-reported name only), and
`model`/`operation` (Ferrum-side metadata naming the table/model and the ORM call). PostgreSQL
`DETAIL`, `HINT`, bound parameter values, row data, and full DSNs are never included in any
field, hook payload, log line, or exception message at any tier. Plan workstream W1-D
(`python/ferrum/errors.py`) must promote `sqlstate`/`category` to a structured attribute on every
mapped exception class, not only the migration-failure message string.

### Schema tenancy and sharding boundaries

Current runtime: no `schema_transaction`, `ShardRouter`, `ConnectionRegistry`, or
`platform_admin_transaction` helper exists yet (confirmed absent from `python/ferrum/`). The only
shipped tenancy primitive is `ferrum.session.tenant_transaction()`
(`python/ferrum/session.py:113-172`), which binds a `ALLOWED_GUC_NAMES`-validated
(`python/ferrum/session.py:32-43`) PostgreSQL GUC as transaction-local state via
`set_config(..., true)`, guaranteeing reset on commit/rollback/pool reuse. There is no
schema-selection (`search_path`) primitive and no shard-routing primitive; every `QuerySet`
terminal takes an explicit `ConnectionLike` argument with no default/global connection.

Ratified W1-F contract (implementation waits for Wave 1 after Wave 0 completes):
(1) RLS tenancy — `tenant_transaction()`'s GUC pattern is the
mechanism for Ticket-Analyzer-style RLS tenancy; add a dedicated `platform_admin_transaction()`
that sets only allowlisted admin GUCs and needs no fake tenant ID. (2) Schema tenancy — add
`schema_transaction(schema, ...)` that validates the schema identifier against a strict allowlist
(never string-interpolated from untrusted input) and sets a transaction-local `search_path`,
guaranteed to reset at transaction end; this is validated schema selection on one pinned
transaction, not implicit routing. (3) Sharding — an optional `ConnectionRegistry`/`ShardRouter`
owns independently configured **PostgreSQL** pools (not a multi-database or dialect-switching
Session). The router resolves a **trusted** shard key chosen by caller/router code and
returns an explicit `Connection`/`Transaction`. QuerySet stays shard-unaware and
connection-explicit; it receives whatever `Connection`/`Transaction` the router hands off.
No implicit connection selection from model metadata, tenant id, or schema name. No
implicit multi-DB behavior, and no `platform_scoped` model flag — access control stays at
the transaction/session boundary, not on the model. ProductManager resolution A (shipped
mysql/sqlite/mssql extras remain best-effort) is not a license for W1-F to launder dialect
switching through the registry.

### Alpha-to-stable compatibility policy (ProductManager resolution A)

ProductManager recorded `decision: approved` with resolution **A** (resolution B — deprecate
and remove non-PostgreSQL extras before a stable release — is **rejected** for v0.1). Binding
product text is §2.6, `README.md`, and `CHANGELOG.md`.

Cited runtime that made the call necessary: `pyproject.toml:7,26` (`version = "0.1.17"`,
`Development Status :: 3 - Alpha`); `QuerySet._IR_VERSION: int = 4`
(`python/ferrum/queryset.py:65`); shipped `mysql`/`sqlite`/`mssql` extras and dialect DDL
(`_MSSQL_TYPE_ALLOWLIST` at `orchestrator.py:255`, `_MSSQL_UNSUPPORTED_KINDS` at `:282`,
`_map_sql_type_mssql` at `:298`).

**Policy (no guarantee before 1.0):**

1. **Public Python API** (`ferrum` package exports, documented Model / QuerySet /
   Connection / Transaction / CLI / contrib). 0.x / alpha: breaking changes are allowed in
   any 0.x release without deprecation or advance notice. SemVer 0.x MINOR and PATCH may
   both break. After 1.0: removing or breaking a documented public Python API requires a
   deprecation window of **at least one minor release and 90 days, whichever is longer**,
   recorded in `CHANGELOG.md` and `README.md`.
2. **Serialized IR** (`QuerySet._IR_VERSION` / Rust `IR_VERSION`, currently `4`). Internal
   PyO3 contract, not a user-facing API. 0.x may change without notice; mismatch fails
   closed before SQL emission. After 1.0, an IR major bump is a Ferrum major-version event.
3. **Generated SQL text** is **never** a compatibility surface, before or after 1.0. Text
   may change for correctness or performance without notice. Consumers must not parse SQL.
   Query fingerprints are observability identifiers, not API. Identifiers remain
   metadata-allowlisted and values remain bound parameters (§2.9) — that safety contract
   is not a stability promise about SQL shape.
4. **Migration file / ops format.** 0.x: no compatibility guarantee for unapplied files or
   ops schema. After 1.0: ops-format changes that break existing unapplied migration files
   follow the same public-API deprecation window and need a documented upgrade path.
5. **Deprecation windows.** None before 1.0. After 1.0: as in (1) and (4). Thin extras
   under resolution A are best-effort indefinitely and are **not** on a deprecation clock
   unless a later ProductManager verdict starts one.

### Explicit rejections

Already true of shipped code; these rejections are binding so future work cannot
reintroduce them as convenience features without a new architecture review:

- **Identity map.** No SQLAlchemy-style session-level object identity cache. No such data
  structure exists in `python/ferrum/queryset.py`, `python/ferrum/connection.py`, or
  `python/ferrum/relations.py`; every terminal returns fresh model instances.
- **Implicit lazy I/O.** Attribute access never executes a hidden query. Forward relations
  and reverse M2M raise `FerrumRelationNotLoadedError`
  (`python/ferrum/relations.py:90-108`, `:121-125`). Reverse FK/OTO may return an unbound
  `QuerySet` filtered by FK (`:126-131`) with **no I/O**; a later terminal still requires
  an explicit `ConnectionLike`. Do not change reverse FK/OTO accessors into always-raise
  as part of this rejection. `select_related()`/`prefetch_related()` remain the only
  loading path.
- **Unrestricted SQL.** No `.raw()`, `.extra()`, string-fragment filters, or production-exposed
  raw-query inspection. All identifiers come from model-metadata allowlists; all values are
  bound parameters (§2.9), enforced structurally in `queryset.py`, the `orchestrator.py`
  allowlists, and the `session.py` GUC allowlist.

## 6. Repository layout

- `.claude/docs/` — individual ADR material that exists in the repository; do not assume absent
  PRD/architecture/security/design files.
- `.claude/` — Claude Code agent config: `agents/`, `docs/`, `rules/`, `skills/`, `commands/`,
  `plans/` (plans use plain `*.md`).
- `.cursor/` — Cursor agent config mirroring `.claude/` for `agents/`, `rules/`, `skills/`,
  `commands/`, `plans/` (plans use the `*.plan.md` suffix). Documentation is not mirrored here —
  `.claude/docs/` is the single source.
- `.agent-work/production-readiness/` — durable task contracts, per-workstream state, immutable
  execution logs, and independent verification records for the production-readiness plan.
- `python/ferrum/` — the public Python package (models, QuerySet, connection, errors, hooks,
  migrations, CLI, contrib extensions).
- `crates/ferrum-core/` — pure Rust engine: IR validator, SQL compiler, row codec, migration planner.
- `crates/ferrum-sql/` — SQL emitter (PostgreSQL dialect).
- `crates/ferrum-pyo3/` — PyO3 bridge: exposes `compile_query`, `hydrate_rows`, `plan_migration`;
  maps `Result`/panics to catchable Python exceptions.
- `crates/ferrum-migrate/` — migration planning support.
- `tests/` — Python tests (`tests/python/unit/`, `tests/python/integration/`,
  `tests/python/security/`); Rust unit tests are co-located in each crate.
- `pyproject.toml` + `Cargo.toml` — Python and Rust build manifests.

## 7. How to work in this repo

- **Read the contract first.** Ground every change in the PRD + architecture review. If a
  request conflicts with them, the documents win; flag the conflict rather than silently
  diverging.
- **Prefer minimal, reviewable diffs.** Do not rewrite working modules to restyle them. Make
  the smallest change that satisfies the task and its tests.
- **Stay inside the boundary.** Put I/O, async, and orchestration in Python; put pure
  compilation/hydration in Rust. Do not leak async into Rust or SQL string-building into Python.
- **Tests are part of the change.** New behavior → new tests in the same diff. A bug fix →
  a regression test that fails before and passes after.
- **Public API change → docs change.** Update `README.md` and any affected docs in the same
  change. A public API change without docs is incomplete.
- **Errors must be actionable.** Validation, compilation, and migration errors must be
  understandable without reading Ferrum source, and must not echo submitted values or secrets.
- **Observability is a launch gate, not an afterthought.** Anything touching the query path
  must preserve the Tier A default hook contract and the redaction layer.
- **No speculative complexity (YAGNI).** Implement only consumer-backed plan requirements.
  Do not implement implicit multi-DB behavior or sync wrappers. An optional trusted
  PostgreSQL shard router with connection-explicit QuerySet is the **ratified W1-F
  contract** in §5a (`ConnectionRegistry` / `ShardRouter` / `schema_transaction` /
  `platform_admin_transaction`). Implementation still waits for Wave 1 workstreams after
  Wave 0 completes; do not implement those APIs in Wave 0.

### Production-readiness execution protocol

For `.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md`:

0. Load `.agent-work/production-readiness/PROTOCOL.md`; it is authoritative for execution.
1. Every task completes **Specify → Plan → Tasks → Implement** before code work starts.
2. Every executor run completes **Load → Execute → Validate executor output → Verify
   independently → Update state**.
3. The coordinator alone edits aggregate state; executors own one workstream and disjoint paths;
   verifiers write independent evidence and never accept executor self-verification.
4. Logs are append-only and include base revision, pre-existing changes, commands and meaningful
   output, changed files, residual risks, and the smallest safe inverse change.
5. Detailed state lives under `.agent-work/production-readiness/`; plan frontmatter changes only
   when an entire top-level todo changes status.
6. Generic verification never replaces ChiefArchitect, SecurityEngineer, ProductManager, or
   CodeReviewer authority. Loops stop at their gated surfaces until verdict artifacts exist.
   Named verdicts use
   `.agent-work/production-readiness/reviews/<task-id>/<run-id>-<authority>.md`
   and clear a gate only with `decision: approved`.

Load `.cursor/skills/ferrum-readiness-orchestration/SKILL.md` or the mirrored Claude skill when
assigning, resuming, implementing, verifying, or looping production-readiness work.

## 8. Definition of done

A change is done only when all of the following hold:

- [ ] It honors every constraint in §2 and every security rule in §3.
- [ ] It does not pre-empt an undecided ADR in §5.
- [ ] It has tests that cover the new/changed behavior (and security-relevant paths where applicable).
- [ ] Public API changes are reflected in `README.md` / docs in the same change.
- [ ] Errors are sanitized and actionable; no secrets, DSNs, bound values, or row data leak by default.
- [ ] The diff is minimal and scoped to the task.
- [ ] Lint/format/type checks (Python) and `cargo check`/`clippy` (Rust) pass for touched code.

## 9. Escalation

- **Product requirement decisions** (what to build, scope changes) → ProductManager.
- **Visual / developer-experience decisions** → ProductDesigner.
- **Architecture decisions, ADRs, service boundaries, data models** → ChiefArchitect.
- **Auth, secrets, SQL-compilation, or migration-apply changes** → notify SecurityEngineer.
- **Cost/risk decisions (e.g., CI wheel matrix breadth) or board-level technology choices** →
  ChiefArchitect escalates to CEO.

Do not implement a feature that bypasses architecture review. If you find implementation
proceeding without an approved architecture for the affected area, stop and flag it to the
ChiefArchitect.

## 10. Newly implemented capabilities (ticket-analyzer compatibility)

The following features were added to support migration of `ticket-analyzer-agent` patterns
to Ferrum. They are part of the supported public surface:

- **Composite primary keys** — `Meta.pk_fields` tuple on models; `PRIMARY KEY (col1, col2)` DDL;
  update/delete keyed by all PK columns.
- **Array / JSONB field types** — `list[T]` (`uuid[]`, `text[]`, scalar arrays) and richer JSONB
  operators (`__contains`, `__has_key`).
- **Upsert API** — `QuerySet.upsert(...)` and `bulk_upsert(...)` with explicit conflict targets,
  `DO NOTHING`, `DO UPDATE`, and `RETURNING` support.
- **RLS / tenant session helpers** — transaction-scoped `set_config` / `current_setting` helpers
  and `tenant_session` pattern on `Connection`/`Transaction`; no GUC leakage across pooled
  connections.
- **`call_function`** — structured stored-procedure calls with allowlisted function identifiers
  and bound arguments.
- **Migration ops for extensions, RLS, and function DDL** — `CreateExtension`, `EnableRLS`,
  `CreatePolicy`, `CreateFunction` migration operations with dry-run and destructive gates.
- **`vector_search` helper** — `ferrum.ext.pgvector.vector_search()` returns rows plus a
  per-row similarity score column; metric operators: `cosine` (`<=>`), `l2` (`<->`),
  `inner_product` (`<#>`).

## Learned User Preferences

- Prefer `mise.toml` tasks over `Makefile`; the project task runner is mise. Prefer `mise push "<msg>"` for commit+push when `[tasks.push]` exists.
- Use `ruff` for Python linting and formatting; do not introduce flake8 or pylint.
- Use `ty` for Python type-checking; do not use or restore mypy.
- Do not install or pin Python or Rust versions via mise — use whatever is installed on the system.
- Prefer parallel sub-agent execution when implementing large features; wave-based delegation is the expected pattern. Coalesce edits to shared files at the end of a run instead of repeatedly rewriting them mid-flight.
- Fix Ferrum defects in the ferrum library, not consumer-project configuration workarounds — reproduce against a live PostgreSQL, fix in the emitter/driver/codec layer, then bump the version and re-pin the consumer.
- Definition of done for a Ferrum feature or fix: implement, add tests, bump the version, and run `mise run ci-local` until fully green before reporting — a passing scoped subset is not enough.
- Prefer Django-style relation lookups (`filter(team__slug=...)`) in consumer code over pre-resolving foreign keys to ids.
- Prefer IDE-friendly public APIs: full type annotations, explicit class/method surfaces, and `.pyi` stubs when needed; avoid dynamic `__getattr__` / monkey-patching so Pylance/Pyright/PyCharm get reliable completion.

## Learned Workspace Facts

- Python deps managed with `uv`; `uv sync --extra dev` installs dev extras including maturin; `maturin` must be under `[project.optional-dependencies] dev`, not only `[build-system] requires`. Local-only scripts live under gitignored `.local/` (e.g. `.local/bump-version.py`); version bumps keep `pyproject.toml`, `python/ferrum/__init__.py` `__version__`, `Cargo.toml` `[workspace.package].version`, `CHANGELOG.md`, and `uv.lock` in sync.
- Python package at `python/ferrum/` (not `src/`); ships `py.typed` and `_native.pyi` for PEP 561 / native stub IntelliSense; Rust crates at `crates/ferrum-{core,sql,pyo3,migrate}/`; PyO3 extension at `crates/ferrum-pyo3/Cargo.toml` with maturin `manifest-path` in `pyproject.toml`.
- Task runner: `mise run <task>`, tasks defined in `mise.toml` at repo root; full local CI parity is `mise run ci-local`. Scoped verification: Rust-only → `test-rust lint-rust`; Python-only → `test-python-unit`; extension/boundary → `dev` plus integration or security tests. Live integration DBs come from `compose.tests.yaml` via `mise run db-up`/`db-down`; `test-integration` is PostgreSQL-only, and `test-integration-all` (in `ci-local`) runs postgres/mysql/sqlite/mssql. `cargo fmt --all` after Rust edits — `lint-rust` runs `cargo fmt --check`.
- Canonical connection env vars: `FERRUM_DATABASE_URL` (primary), `DATABASE_URL` (fallback when the former is unset). Override the env var name via `[ferrum].database_url_env` in `ferrum.toml` or `pyproject.toml`. Library `ferrum.connect()` resolves from env + project config — no dotenv in core code. `ferrum.contrib.fastapi.ferrum_lifespan` yields an open `Connection`; assign `app.state.ferrum_conn = conn` in lifespan and inject routes with `Depends(get_ferrum_conn)` (`FerrumConnRequest` protocol — no hard FastAPI import so `ty` checks without the extra). CLI bootstrap (`ferrum.cli.bootstrap`) runs before subcommands: project config, dotenv load (`override=False`), and settings/model import; discovery order is `FERRUM_SETTINGS` → `[ferrum].settings` in `ferrum.toml` / `pyproject.toml` → `ferrum_conf.py`.
- Ferrum CLI is Typer-based and requires the `ferrum[cli]` extra (typer + rich); subcommands include `makemigrations`, `migrate`, `showmigrations`, `revert`, `resetdb` (--confirm required), and `inspectdb`; `makemigrations` scans `Model.__subclasses__()` so models must be imported via bootstrap settings module. `makemigrations` autodiff replays prior ops into a `SchemaState` (not just `{table: [columns]}`) and detects changes on existing tables: `add_index` / `drop_index` for `db_index` and `Meta.indexes` flips, and `alter_column` for `db_default` / `nullable` changes (`alter_column` is PostgreSQL-only in v0.1). Type changes and renames are still not autodiffed. `ferrum revert` runs reverse ops and removes the ledger entry but leaves migration files on disk (Django-style). `inspectdb` introspects `information_schema` BASE TABLE rows only, excludes `pg_*` and `ferrum_migrations`, emits singular class names, and includes `model_config` with the explicit table name; it queries via `Connection._require_driver().fetch()` — that returns a `TimedQueryExecutor`, not a raw pool (never use `driver._pool`). Migration apply/revert failures raise `FerrumMigrationError` with the failing operation context and a sanitized PostgreSQL message — not just the driver exception class name.
- `Field(default=...)` string values are Python-side defaults only; SQL DEFAULT requires `db_default` (empty string SQL literal is `db_default="''"`, not `Field(default="")`). `uuid_generate="v7"` maps to `db_default="uuidv7()"`; v4/default UUID uses `gen_random_uuid()`.
- Implemented APIs: every QuerySet terminal requires an explicit `conn` (`.all(conn)`, `.get(conn, id=...)`, etc.) — no implicit/default connection; `Connection` and `Transaction` both satisfy `ConnectionLike`; no `QuerySet.raw()`/`extra()` escape hatches. Transaction — `Connection.transaction(isolation, readonly, deferrable, deadline)` async context manager; `Transaction.savepoint()` for nested savepoints. Query — `Q()` with `&`/`|`/`~` for predicate composition; `exclude()`, `exists()`, `values()`/`values_list()`, `only()`/`defer()` (deferred-field access raises `FerrumDeferredFieldError`), `distinct()`, `qs[a:b]` slicing as offset/limit shorthand; `select_related()` for JOIN-based eager loading, `prefetch_related()` for N+1-safe multi-query loading. Relationships — ClassVar descriptors (`ForeignKey`, `OneToOne`, `ManyToMany`); FK/OTO use `{field}_id` columns; M2M uses join tables via migration ops. Cross-dialect FTS (IR v3 `text_rank_by`) — `TSVector` fields, `FullTextIndex` migration op, `QuerySet.search()` modes (`plain`/`phrase`/`websearch`/`boolean`), `rank_by()` relevance, `ferrum.ext.fts.scored_search`. PG extras — composite PKs; `on_conflict()` upsert; `tenant_transaction()` RLS GUCs; `call_function()` for allowlisted stored procedures; pgvector: `ferrum.ext.pgvector.register_vector_codecs(conn)` after `ferrum.connect()`. Extras — `ferrum-orm[mssql]` T-SQL thin-parity via `aioodbc`; `ferrum-orm[msgpack]` wire format via `FERRUM_WIRE_FORMAT=msgpack` or config (default JSON). Relation-filter JOINs — one-level Django-style lookups (`filter(team__slug=...)`, `Q(team__id=...) | Q(team__slug=...)`); filter-only joins are `INNER` with no SELECT projection while `select_related()` stays `LEFT` + projected; nested hops (`a__b__c`) and FTS on relation lookups are rejected, and UPDATE/DELETE reject relation lookups via `_check_write_scope`. Relations must be declared as `ClassVar` descriptors and are inherited from parent-model metadata (`_build_metadata` no longer scans only `cls.__dict__`). Also `project(Model)` for same-table projection into a narrower model, `group_by()` + `aggregate()` typed aggregates, `stream(conn, chunk_size=...)` as a cursor-pinning async context manager (rejects `prefetch_related()`), and `nearest_to()` where KNN distance is the primary sort with `order_by()` secondary. Echo/verbose SQL logging — `ferrum.enable_echo(verbose=...)` / `disable_echo()`, `ferrum.connect(..., echo=True|"debug")`, or `FERRUM_ECHO=1|debug`; a generic `DEBUG=1` never enables it.
- `_hydrate_rows` JSON/msgpack encoders (`_RowEncoder`, `_msgpack_row_default`) serialize a throwaway copy for Rust structural validation only; `model_construct` still receives native driver types from `row_dicts`. Non-JSON-native scalars (`Decimal`, `date`, enums, etc.) must encode via `_mapping`/bytes handling plus `str()` fallback — never `float` for `Decimal`, and never pickle.
- PostgreSQL type fidelity is a recurring defect class: every emitted cast must match the DDL type the migration layer produces, and for primary-key columns it must equal it exactly, since a join predicate allows no implicit cast (`uuid = text` → SQLSTATE 42883 / `UndefinedFunctionError`). `bulk_update` VALUES placeholders therefore cast to `uuid`, `uuid[]`, `tsvector`, and `numeric` — never `text` or `double precision` (Ferrum binds `Decimal` as a string, which `double precision` rejects). asyncpg returns `json`/`jsonb` as `str` by default, so the driver registers JSON codecs on every pool connection in `AsyncpgDriver.open()` for JSONB to hydrate as `dict`/`list`. JSONB `__contains` must emit `@>` (and `?` / `?|`) with JSONB typing, not text `LIKE`, which produced an undefined `jsonb ~~ text` operator. Sanitized `FerrumDatabaseError ... [FERR-D001]` drops the PostgreSQL message, so reproduce these against a live database rather than reading the wrapped error.
- Ferrum is still alpha with no semver/stability contract (breaking changes until the first public release; the IR version has already moved), and `danger_delete_all` is a known `xfail` that compiles to invalid SQL with a trailing `WHERE`. `.claude/docs/` holds only ADRs — the `PRODUCT_REQUIREMENTS.md` / `ARCHITECTURE.md` / `SECURITY.md` / `PRODUCT_DESIGN.md` files referenced in §1 do not exist, so trust source plus `CHANGELOG.md` over plan documents.
- `ferrum-local-tests` sibling project at `../ferrum-local-tests/` for local CRUD/migration testing; editable install via `[tool.uv.sources] ferrum = { path = "../ferrum", editable = true }`. `ticket-analyzer-agent` consumes `ferrum-orm[cli,pg]` from PyPI by default — override with an editable `[tool.uv.sources]` path to pick up unpublished Ferrum fixes.
- GitHub Actions CI jobs require a virtualenv before `maturin develop`: `python -m venv .venv && . .venv/bin/activate` before pip/maturin/pytest steps; `release.yml` builds abi3 wheels on `v*` tag push and publishes to PyPI via OIDC trusted publishing. PR `ci.yml` runs PostgreSQL integration; nightly `integration-matrix` covers postgres/mysql/sqlite/mssql. Live MSSQL still needs `msodbcsql18` and the `integration` pytest marker.
