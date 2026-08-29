---
task_id: w0-a-architecture-contracts
run_id: 20260821T075800Z
authority: ProductManager
reviewer: product-manager
reviewed_at: 2026-08-21T08:12:00Z
base_revision: 768ec1f3013f6d0eccd7c8b590ba36b54b12d23e
decision: approved
scope:
 - alpha-to-stable compatibility policy
 - AGENTS.md §2.6 PostgreSQL-only invariant vs shipped thin extras
 - public Python API, serialized IR, generated SQL, migration files, deprecation windows
 - product non-goals: identity map, implicit lazy I/O, unrestricted SQL, implicit multi-DB
---

# Named Authority Verdict

## Authority

`ProductManager`

## Claims reviewed

1. The §5a draft correctly surfaces a real, shipped contradiction between `AGENTS.md` §2.6 ("PostgreSQL is the first and only supported database for the MVP. No multi-database abstraction, no SQLite shortcut, no MySQL fallback") and the already-published MySQL / SQLite / SQL Server extras, drivers, dialect DDL, README thin-parity matrix, and CHANGELOG entries.
2. The draft presents two mutually exclusive resolutions (A: reclassify §2.6; B: deprecate/remove non-PostgreSQL extras) and does not itself pick one. ProductManager must pick exactly one.
3. Until 1.0 there is no compatibility guarantee covering the public Python API, serialized IR, generated SQL text, or migration file/ops format; that absence must be documented rather than assumed. A deprecation window exists only after 1.0.
4. Identity map, implicit lazy I/O, unrestricted SQL (`raw()` / `extra()` / string fragments), and implicit multi-DB / global-session behavior are product non-goals. An explicit trusted shard router with QuerySet remaining connection-explicit is in plan scope (W1-F), not implicit multi-DB.
5. Production-readiness consumers and release gates are PostgreSQL-only (Ticket Analyzer, Org AI Platform). Thin extras must not shape or delay PostgreSQL correctness.

## Evidence

Inspected current source and docs directly. The executor log was used only as a pointer to owned paths; every claim below is cited from files on disk.

**Packaging / alpha status**

- `pyproject.toml:7` — `version = "0.1.17"`.
- `pyproject.toml:26` — `Development Status :: 3 - Alpha`.
- `pyproject.toml:17-19` — keywords include `mysql`, `sqlite`, `mssql`.
- `pyproject.toml:46-50` — real extras: `pg = ["asyncpg>=0.29"]`, `mysql = ["asyncmy>=0.2"]`, `sqlite = ["aiosqlite>=0.19"]`, `mssql = ["aioodbc>=0.5"]`.
- `pyproject.toml:60-64` — `[all]` installs all four driver extras.
- `python/ferrum/__init__.py:9` — `__version__ = "0.1.17"`.

**Shipped non-PostgreSQL runtime (not hypothetical)**

- `python/ferrum/drivers/` contains `postgres.py`, `mysql.py`, `sqlite.py`, `mssql.py`.
- `python/ferrum/drivers/__init__.py:14-29` — `get_driver_for_dsn` dispatches `postgresql`/`postgres`, `mysql`/`mysql+asyncmy`, plus sqlite/mssql schemes.
- `python/ferrum/connection.py:147-159` — `Connection.dialect` is `"postgres" | "mysql" | "sqlite" | "mssql"`.
- `python/ferrum/migrations/orchestrator.py:243-248, 255-295, 298-351` — dialect identifier quoting; `_MSSQL_TYPE_ALLOWLIST`; `_MSSQL_UNSUPPORTED_KINDS`; `_map_sql_type_mssql`.
- `python/ferrum/queryset.py:2715-2719` — upsert is PostgreSQL `ON CONFLICT` only; `mysql`/`sqlite`/`mssql` raise `FerrumConfigError` rather than emitting non-portable SQL.
- `python/ferrum/migrations/fts/__init__.py` — per-dialect FTS backends including mysql/sqlite/mssql.

**Serialized IR**

- `python/ferrum/queryset.py:64-65` — `_IR_VERSION: int = 4`.
- `crates/ferrum-core/src/ir/mod.rs:18` — `pub const IR_VERSION: u32 = 4`.
- `crates/ferrum-core/src/compile/mod.rs:56-58` — mismatch is rejected before compilation.

**Public docs vs §2.6**

- `AGENTS.md:46-47` — §2.6 still states PostgreSQL is the only supported MVP database and forbids multi-database abstraction / SQLite shortcut / MySQL fallback.
- `AGENTS.md:195-220` — §5a compatibility draft: disputed claim, options (a) and (b), policy checklist, "Until 1.0, no compatibility guarantee".
- `README.md:27` — "PostgreSQL-first architecture (MySQL, SQLite, and SQL Server via optional extras)".
- `README.md:46-50` — unratified compatibility note flagging the §2.6 contradiction; thin-parity wording left unchanged pending this decision.
- `README.md:234-235, 316` — cross-dialect FTS claimed as shipped.
- `README.md:343-347` — Project Status: API not stable; breaking changes expected until the first public release.
- `README.md:358-409` — install extras plus a capability matrix labeling MySQL/SQLite/SQL Server as **thin-parity** backends; PRs exercise PostgreSQL+SQLite; nightly matrix runs all four.
- `CHANGELOG.md:5-6` — claims Keep a Changelog + Semantic Versioning, with no 0.x-is-unstable qualifier.
- `CHANGELOG.md:14-29` (Unreleased) — live multi-driver coverage and capability-gated upsert failures on non-PostgreSQL backends.
- `CHANGELOG.md:290-293` (`[0.1.3]`) — SQL Server thin parity shipped; MySQL/SQLite already present (`CHANGELOG.md:365`).

**Approved plan (already chose the shape of A)**

- `.cursor/plans/ferrum-production-readiness_6b5f422d.plan.md:49-53` — "PostgreSQL is the production target. Existing thin dialect support must not shape or delay PostgreSQL correctness." Preserve explicit `ConnectionLike`. No identity map. No raw-SQL escape hatches. Trusted shard routing is later W1-F, QuerySet connection-explicit.
- Same plan `:61-69` — P0 release gates are live PostgreSQL.
- Same plan `:333-355` — consumer pilots are Ticket Analyzer (PostgreSQL RLS) and Org AI Platform (PostgreSQL schema-per-tenant + shard pools).
- `.cursor/goals/ferrum-production-readiness.md:10-11` — success is a production-grade **async-native PostgreSQL** ORM for those two consumers.

**Consumer pin (PostgreSQL extra only)**

- Ticket Analyzer `packages/domain/pyproject.toml:15` — `ferrum-orm[cli,pg]>=0.1.17`. No mysql/sqlite/mssql extra.
- Org AI Platform has no `ferrum-orm` pin in-tree; the plan treats it as a future PostgreSQL consumer, not a thin-extra consumer.

**Stale conflicting product text (outside this run's owned paths; residual)**

- `.cursor/plans/wave-0-backlog.md:173-178` — "Explicitly Out of Scope (v0.1): SQLite, MySQL, multi-DB". That line is false as a statement of shipped code; under resolution A it must be rewritten to "out of production-readiness release gates / must not shape PostgreSQL correctness", not "must not exist".

**Task contract**

- `.agent-work/production-readiness/tasks/w0-a-architecture-contracts.md:69-74` — ProductManager records the compatibility-policy decision. This verdict is that record. Applying the wording to `AGENTS.md` / `README.md` / `CHANGELOG.md` remains W0-A follow-through after named review (W0-A still holds the shared-path lease).

## Findings

### Product decision (binding) — Resolution A

Reclassify `AGENTS.md` §2.6. PostgreSQL remains the only database targeted by the production-readiness plan, P0 release gates, and consumer pilots. The existing MySQL / SQLite / MSSQL extras, drivers, and thin-parity capability matrix **remain shipped**, **best-effort**, and **out of scope for production-readiness release gates**. They must not shape PostgreSQL SQL correctness, type fidelity, migration transactionality, error taxonomy, pool behavior, or tenancy APIs, and they must not delay those gates.

Resolution B (deprecate/remove non-PostgreSQL extras before a stable release) is **rejected** for v0.1 production-readiness. Removal would be net-new product work with no Ticket Analyzer or Org AI Platform consumer, would contradict the approved plan's fixed decision that existing thin dialect support stays, and would collide with already-shipped 0.1.x extras plus in-flight multi-driver CI. A future ProductManager decision may still start a deprecation clock; none is started now.

### Compatibility policy (binding; no guarantee before 1.0)

Document all five surfaces explicitly. Until the first `1.0` release, **none** of them carries a compatibility guarantee.

1. **Public Python API** (`ferrum` package exports, documented Model / QuerySet / Connection / Transaction / CLI / contrib surfaces).
 - **0.x / alpha:** breaking changes are allowed in any 0.x release without deprecation or advance notice. `README.md:343-347` already states this; keep it.
 - **Qualify CHANGELOG SemVer:** `CHANGELOG.md:6` must state that 0.x follows SemVer's unstable-major rule: MINOR and PATCH may both break. Do not imply 0.1.x patch-only stability.
 - **After 1.0:** SemVer. Removing or breaking a documented public Python API requires a deprecation window of **at least one minor release and 90 days, whichever is longer**, recorded in `CHANGELOG.md` and `README.md`.

2. **Serialized IR** (`QuerySet._IR_VERSION` / Rust `IR_VERSION`, currently `4`).
 - Internal PyO3 contract, not a user-facing API. Users must not construct or persist IR payloads.
 - **0.x:** may change without notice. Incompatible changes bump the version field; mismatch fails closed before SQL emission (`crates/ferrum-core/src/compile/mod.rs:56-58`).
 - **After 1.0:** an IR major bump is a Ferrum major-version event. JSON/msgpack wire-format changes also require a versioned compatibility decision (plan W4-B).

3. **Generated SQL text**.
 - **Never a compatibility surface**, before or after 1.0. Text may change for correctness or performance without notice. Consumers must not parse SQL. Query fingerprints are observability identifiers, not API. Identifiers remain metadata-allowlisted; values remain bound parameters (`AGENTS.md` §2.9) — that safety contract is not a stability promise about SQL shape.

4. **Migration file / ops format**.
 - **0.x:** no compatibility guarantee for unapplied files or ops schema. Applied-file checksum rejection remains an operational safety gate, not a stability promise.
 - **After 1.0:** ops-format changes that break existing unapplied migration files follow the same public-API deprecation window and need a documented upgrade path.

5. **Deprecation windows**.
 - **None before 1.0.**
 - **After 1.0:** as in (1) and (4). Thin extras under A are best-effort indefinitely and are **not** on a deprecation clock unless a later ProductManager verdict starts one.

### Product non-goals (binding; reject as YAGNI)

These remain out of v0.1 and must not be added as convenience features without a new ProductManager + ChiefArchitect decision:

- **Identity map** / SQLAlchemy-style session identity cache.
- **Implicit lazy I/O** on unloaded relations (must keep raise-on-access; `select_related` / `prefetch_related` are the only loaders).
- **Unrestricted SQL** (`raw()`, `extra()`, string-fragment filters, production-exposed query inspection).
- **Implicit multi-DB behavior**, global/default connections, or QuerySet-owned shard routing.
- **Sync API**, greenlet bridges, or blocking wrappers.

**In scope and already plan-approved:** an optional explicit trusted `ConnectionRegistry` / `ShardRouter` that resolves a caller-chosen shard key and hands QuerySet an explicit `Connection` / `Transaction`. QuerySet stays connection-explicit and shard-unaware. That is not implicit multi-DB.

### Follow-through required of W0-A after this verdict (does not reopen the product call)

The draft packet is accepted. Contract files still contradict A until the executor applies this decision. Required wording, not a new product fork:

- **High — `AGENTS.md` §2.6:** replace "first and only supported database / no SQLite shortcut / no MySQL fallback" with: PostgreSQL is the only production-readiness and consumer-pilot target; shipped `mysql`/`sqlite`/`mssql` extras are best-effort thin parity, out of P0 gates, and must not shape PostgreSQL correctness.
- **High — `README.md:27` and `:358-409`:** keep the capability matrix (it is accurate) but label extras **best-effort / not production-supported / not release gates**. Remove the unratified-draft blockquote once the applied text matches this verdict. Cross-dialect FTS remains a thin-extra capability, not a P0 gate.
- **Medium — `CHANGELOG.md:5-6` plus a short Unreleased note:** 0.x has no compatibility guarantee; 1.0 starts the deprecation window above.
- **Medium — residual, outside W0-A lease:** `.cursor/plans/wave-0-backlog.md:178` still lists "SQLite, MySQL, multi-DB" as v0.1 out of scope. Coordinator should route a doc-sync so it matches A (out of *gates*, not absent from the tree).

No production-code change is authorized by this verdict. SecurityEngineer / ChiefArchitect gates on retry, errors, tenancy, and ADR-004 are out of this authority.

## Decision

`approved`

This record approves the §5a compatibility draft **as a decision packet** and records the product resolution the draft left open:

**Chosen resolution: A.** Reclassify `AGENTS.md` §2.6. PostgreSQL remains the production target. Documented thin extras stay shipped, non-blocking, and must not shape PostgreSQL correctness. **B is rejected** for v0.1 production-readiness.

**Policy:** no compatibility guarantee before 1.0 for public Python APIs, serialized IR, generated SQL, or migration files. After 1.0, public API and migration-ops breaks require at least one minor release and 90 days. Generated SQL is never a compatibility surface. IR remains an internal versioned PyO3 contract.

**Non-goals:** identity map, implicit lazy I/O, unrestricted SQL, implicit multi-DB / global session. Explicit trusted shard router with connection-explicit QuerySet remains in plan scope.

This grants only the ProductManager gate. It does not substitute for ChiefArchitect, SecurityEngineer, CodeReviewer, or independent verification. W0-A should apply the follow-through wording under its existing shared-path lease; it must not treat §5a as settled until those owned docs match this verdict.
