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

Read these before doing substantial work — they are the product and architecture contract:

- `.claude/docs/PRODUCT_REQUIREMENTS.md` — the v0.1 product contract (scope, security, acceptance criteria).
- `.claude/docs/ARCHITECTURE.md` — the architecture contract: invariants, component boundaries, and the ADRs.
- `.claude/docs/SECURITY.md` — security requirements that are release-qualification gates.
- `.claude/docs/PRODUCT_DESIGN.md` — developer-experience and onboarding decisions.
- `README.md` — the external pitch and the API shape we are committing to.

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
6. **PostgreSQL is the first and only supported database** for the MVP. **No multi-database
   abstraction, no SQLite shortcut, no MySQL fallback** before the PostgreSQL MVP is stable.
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

## 5. Architecture decisions (ADRs) — resolved

The original six ADRs are now closed. The implementation choices are recorded below for
reference. New ADRs should be opened here if future decisions warrant them.

- **ADR-001** ✅ Resolved — Python-side `asyncpg` driver (`ferrum.drivers.postgres`; install with `ferrum-orm[pg]`).
- **ADR-002** ✅ Resolved — IR v2 JSON contract (`crates/ferrum-core/src/ir/`); version field in `QuerySet._IR_VERSION`.
- **ADR-003** ✅ Resolved — construct-without-revalidate fast path (`queryset._hydrate_rows`, `model_construct`); custom-validator caveat documented.
- **ADR-004** ✅ Resolved — transactional by default; non-transactional classification in `operations.py` for `CREATE INDEX CONCURRENTLY` and certain `ALTER TYPE`/enum ops.
- **ADR-005** ✅ Resolved — maturin + cibuildwheel abi3 wheels; `release.yml` builds and publishes to PyPI on `v*` tag push via OIDC trusted publishing.
- **ADR-006** ✅ Resolved — centralized redaction layer in `errors.py` (`map_db_error`/`map_native_error`); Tier A/B/C hooks in `hooks.py`.

## 6. Repository layout

- `.claude/docs/` — authoritative project documentation: PRD, architecture, data model, migrations,
  query engine, project structure, security, product design. Single source of truth.
- `.claude/` — Claude Code agent config: `agents/`, `docs/`, `rules/`, `skills/`, `commands/`,
  `plans/` (plans use plain `*.md`).
- `.cursor/` — Cursor agent config mirroring `.claude/` for `agents/`, `rules/`, `skills/`,
  `commands/`, `plans/` (plans use the `*.plan.md` suffix). Documentation is not mirrored here —
  `.claude/docs/` is the single source.
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
- **No speculative complexity (YAGNI).** Do not add relationship loaders, sharding, multi-DB
  abstractions, sync wrappers, or config knobs that v0.1 does not require.

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

- Prefer `mise.toml` tasks over `Makefile`; the project task runner is mise.
- Use `ruff` for Python linting and formatting; do not introduce flake8 or pylint.
- Use `ty` for Python type-checking; do not use or restore mypy.
- Do not install or pin Python or Rust versions via mise — use whatever is installed on the system.
- Prefer parallel sub-agent execution when implementing large features; wave-based delegation is the expected pattern.

## Learned Workspace Facts

- Task runner: `mise run <task>`; tasks defined in `mise.toml` at repo root.
- Python deps managed with `uv`; `uv sync --extra dev` installs dev extras including maturin; `maturin` must be under `[project.optional-dependencies] dev`, not only `[build-system] requires`.
- Python package at `python/ferrum/` (not `src/`); Rust crates at `crates/ferrum-{core,sql,pyo3,migrate}/`; PyO3 extension at `crates/ferrum-pyo3/Cargo.toml` with maturin `manifest-path` in `pyproject.toml`.
- Full local CI parity: `mise run ci-local`; scoped verification: Rust-only → `test-rust lint-rust`; Python-only → `test-python-unit`; extension/boundary → `dev` plus integration or security tests.
- Canonical connection env vars: `FERRUM_DATABASE_URL` (primary), `DATABASE_URL` (fallback when the former is unset). Override the env var name via `[ferrum].database_url_env` in `ferrum.toml` or `pyproject.toml`. Library `ferrum.connect()` resolves from env + project config — no dotenv in core code. `ferrum.contrib.fastapi.ferrum_lifespan` yields an open `Connection`; assign `app.state.ferrum_conn = conn` in lifespan and inject routes with `Depends(get_ferrum_conn)`. CLI bootstrap (`ferrum.cli.bootstrap`) runs before subcommands: project config, dotenv load (`override=False`), and settings/model import; discovery order is `FERRUM_SETTINGS` → `[ferrum].settings` in `ferrum.toml` / `pyproject.toml` → `ferrum_conf.py`.
- Ferrum CLI is Typer-based and requires the `ferrum[cli]` extra (typer + rich); subcommands include `makemigrations`, `migrate`, `showmigrations`, `revert`, `resetdb` (--confirm required), and `inspectdb`; `makemigrations` scans `Model.__subclasses__()` so models must be imported via bootstrap settings module. `ferrum revert` runs reverse ops and removes the ledger entry but leaves migration files on disk (Django-style). `inspectdb` introspects `information_schema` BASE TABLE rows only, excludes `pg_*` and `ferrum_migrations`, emits singular class names, and includes `model_config` with the explicit table name.
- Migration apply/revert failures raise `FerrumMigrationError` with the failing operation context and a sanitized PostgreSQL message — not just the driver exception class name.
- `Field(default=...)` string values are Python-side defaults only; SQL DEFAULT requires `db_default` (empty string SQL literal is `db_default="''"`, not `Field(default="")`). `uuid_generate="v7"` maps to `db_default="uuidv7()"`; v4/default UUID uses `gen_random_uuid()`.
- Implemented APIs: every QuerySet terminal requires an explicit `conn` (`.all(conn)`, `.get(conn, id=...)`, etc.) — no implicit/default connection; `Connection` and `Transaction` both satisfy `ConnectionLike`. Transaction — `Connection.transaction(isolation, readonly, deferrable, deadline)` async context manager; `Transaction.savepoint()` for nested savepoints. Query — `Q()` with `&`/`|`/`~` for predicate composition; `exclude()`, `exists()`, `values()`/`values_list()`, `only()`/`defer()` (deferred-field access raises `FerrumDeferredFieldError`), `distinct()`, `qs[a:b]` slicing as offset/limit shorthand; `select_related()` for JOIN-based eager loading, `prefetch_related()` for N+1-safe multi-query loading. PG extras — composite PKs supported; `on_conflict()` for upsert; `tenant_transaction()` sets RLS GUC session variables; `call_function()` for safe stored-procedure invocation; pgvector: call `ferrum.ext.pgvector.register_vector_codecs(conn)` after `ferrum.connect()`.
- `ferrum-local-tests` sibling project at `../ferrum-local-tests/` for local CRUD/migration testing; editable install via `[tool.uv.sources] ferrum = { path = "../ferrum", editable = true }`.
- GitHub Actions CI jobs require a virtualenv before `maturin develop`: `python -m venv .venv && . .venv/bin/activate` before pip/maturin/pytest steps; `release.yml` builds abi3 wheels on `v*` tag push and publishes to PyPI via OIDC trusted publishing.
- Model relationships use ClassVar descriptors (`ForeignKey`, `OneToOne`, `ManyToMany`) on the model class; FK/OTO use `{field}_id` columns and M2M uses join tables via migration ops.
- `ferrum-orm[mssql]` extra adds MSSQL (T-SQL) thin-parity via `aioodbc`; T-SQL dialect uses `?` placeholders, `[bracket]` identifier quoting, `OUTPUT INSERTED.*` instead of `RETURNING`, and `OFFSET x ROWS FETCH NEXT y ROWS ONLY` pagination. `ferrum-orm[msgpack]` extra adds MessagePack wire format (`rmp-serde` is always compiled into the wheel; Python `msgpack` is gated); select format via `FERRUM_WIRE_FORMAT=msgpack` env var or `[ferrum] wire_format = "msgpack"` in ferrum.toml/pyproject.toml; default stays JSON. Live MSSQL integration tests require the `msodbcsql18` system ODBC driver and are gated behind the `integration` pytest marker — not in default CI.
- `cargo fmt --all` must be run after any Rust edits; the `lint-rust` CI task runs `cargo fmt --check` and will fail on unformatted code — always format before considering Rust work done.
