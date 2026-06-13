# Ferrum Architecture

**Version:** v0.1 architecture  
**Inputs:** [PRODUCT_REQUIREMENTS.md](./PRODUCT_REQUIREMENTS.md), [SECURITY.md](./SECURITY.md), [PRODUCT_DESIGN.md](./PRODUCT_DESIGN.md)

---

## 1. Purpose

This document defines how Ferrum is structured before implementation begins. It is the authoritative architecture contract for v0.1: component boundaries, async execution model, package layout, integration contracts, security invariants, and the ADR decisions engineers must implement against.

---

## 2. System Context

Ferrum is an async ORM library embedded in Python application processes. It is not a standalone service. External dependencies are limited to PostgreSQL and the host Python runtime.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                     Host Application (FastAPI / Starlette)              │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                    ferrum-py (public Python API)                   │  │
│  │   Model · QuerySet · Connection · Migrations · Hooks · CLI      │  │
│  └───────────────────────────────┬───────────────────────────────────┘  │
│                                  │ PyO3 (sync, GIL-held)               │
│  ┌───────────────────────────────▼───────────────────────────────────┐  │
│  │              ferrum-pyo3 (extension bridge)                        │  │
│  │   compile_query · hydrate_rows · plan_migration · map_errors      │  │
│  └───────────────────────────────┬───────────────────────────────────┘  │
│                                  │ native calls                          │
│  ┌───────────────────────────────▼───────────────────────────────────┐  │
│  │              ferrum-core (Rust engine, pure sync)                  │  │
│  │   SQL compiler · IR validator · row codec · migration planner     │  │
│  └───────────────────────────────┬───────────────────────────────────┘  │
│                                  │ asyncpg protocol (Python-owned)       │
└──────────────────────────────────┼──────────────────────────────────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │     PostgreSQL       │
                        │   (single primary)   │
                        └──────────────────────┘
```

**External integrations (v0.1):**

| Integration | Role | Contract owner |
|-------------|------|----------------|
| PostgreSQL | Persistence target | Ferrum driver adapter (Python) |
| Pydantic v2 | Model validation/serialization | ferrum-py |
| asyncpg | Async I/O to PostgreSQL | ferrum-py connection layer |
| PyO3 / maturin | Rust↔Python bridge | ferrum-pyo3 |

---

## 3. Architecture Invariants

These are non-negotiable for v0.1. Violating any invariant is an architecture defect, not an implementation detail.

1. **Python owns developer ergonomics and async I/O.** Public API, connection pool, transactions, hook dispatch, and migration apply orchestration live in Python.
2. **Rust owns performance-critical, pure, synchronous compilation and codec work.** No async I/O, no connection pool, no per-request mutable shared state in Rust.
3. **Async-first only.** Every core API is awaitable. No sync compatibility layer.
4. **PostgreSQL only.** No multi-database abstraction in v0.1.
5. **Immutable shared metadata.** Model metadata (table/column/operator allowlists) is built once at class definition and is read-only thereafter.
6. **Per-call compilation output.** Each compile call produces fresh owned output; concurrent tasks never mutate shared compiled state.
7. **Parameterized SQL only.** Values travel out-of-band from identifiers in the IR; user strings never interpolate into SQL.
8. **Tier A observability by default.** Bound values, DSNs, and row bodies never appear in default hook payloads.
9. **Centralized error and redaction boundary.** One Python component maps driver/PyO3/PostgreSQL errors to the Ferrum taxonomy and shapes hook payloads.

---

## 4. High-Level Component Diagram

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                           APPLICATION LAYER                               │
│  Route handlers · services · migration CLI · observability consumers     │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│                         ferrum-py (Python package)                        │
├──────────────────────────────────────────────────────────────────────────┤
│ Model registry & metadata builder   │ QuerySet (lazy, chainable)        │
│ Connection pool & config              │ Migration orchestrator            │
│ Hook dispatcher (Tier A/B/C)        │ Error boundary & redaction        │
│ Danger API guards                   │ CLI entrypoints                   │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │ QuerySetIR / ModelMetadata (typed)
┌────────────────────────────────▼─────────────────────────────────────────┐
│                        ferrum-pyo3 (PyO3 extension)                       │
├──────────────────────────────────────────────────────────────────────────┤
│ Boundary: Result→Python exceptions    │ panic = unwind + catch wrapper   │
│ GIL: held for sync compile/hydrate  │ No I/O, no tokio runtime          │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│                         ferrum-core (Rust crate)                          │
├──────────────────────────────────────────────────────────────────────────┤
│ IR validator (allowlists)           │ SQL compiler (PostgreSQL dialect)  │
│ Bound-param encoder                 │ Row decoder / hydration payload    │
│ Migration diff planner            │ Migration SQL emitter              │
│ Structured compile errors           │ (no network, no async runtime)     │
└──────────────────────────────────────────────────────────────────────────┘
```


---

## 5. Python/Rust Boundary

### 5.1 What Lives in Python

| Responsibility | Rationale (lens) |
|----------------|------------------|
| Public API (`Model`, `QuerySet`, managers) | Least Astonishment — Django/Pydantic mental model |
| Async connection pool and query execution | Event-Driven vs Request-Response — cancellation/timeouts at await points |
| Transaction boundaries | Blast Radius — failure scope owned where I/O lives |
| Hook dispatch and Tier A/B/C payload shaping | Observability First — non-bypassable redaction at dispatch |
| Migration apply orchestration and confirmation gates | Defense in Depth — destructive gates before SQL reaches DB |
| Danger API enforcement (`danger_delete_all`, etc.) | Defense in Depth — fail before IR emission |
| Centralized error mapping and sanitization | Blast Radius — single hardened boundary |
| CLI and config loading | Separation of Concerns — developer ergonomics |

### 5.2 What Lives in Rust

| Responsibility | Rationale (lens) |
|----------------|------------------|
| QuerySet IR → parameterized SQL | Performance + Defense in Depth — allowlist validation before emission |
| Row bytes → typed hydration payloads | Data Gravity — decode close to wire format |
| Schema diff → migration plan/SQL | Schema Evolution — deterministic planner |
| Structured compile/plan errors | Single Responsibility — pure validation/compiler |
| Operator/field/sort allowlist enforcement | Defense in Depth — structural, not conventional |

### 5.3 What Does Not Cross the Boundary

- Raw user strings as SQL identifiers
- Connection handles, sockets, or async futures
- Mutable per-request shared state
- Full trace strings or PostgreSQL `DETAIL` blobs (structured fields only)
- Bound parameter values in default observability payloads

### 5.4 Boundary Contract Shape (ADR-002)

The PyO3 boundary uses a **typed, versioned, serializable IR**:

```text
Python                          Rust                           Python
───────                         ────                           ──────
QuerySet (lazy)  ──build──►  QuerySetIR v1  ──compile──►  CompiledQuery
                               + ModelMetadata                 { sql_text
                                                                 , bound_params[]
                                                                 , param_type_summary[]
                                                                 , fingerprint }
asyncpg rows[]   ─────────►  HydrateRequest ──decode──►   RowPayload[]
                               + ModelMetadata                 (typed column map)
```

**IR design rules:**

- Identifiers (table, column, operator, sort direction) are enums/indices resolved from metadata allowlists — never runtime strings in SQL positions.
- Values are carried in a separate bound-parameter array with positional placeholders in SQL text.
- IR carries a `version` field; incompatible versions fail fast at the boundary.
- Compilation is a pure function: `compile(&Metadata, &QuerySetIR) -> Result<CompiledQuery, CompileError>`.

### 5.5 Alternatives Considered

| Alternative | Why rejected |
|-------------|--------------|
| Rust owns async driver (sqlx/tokio) | Two runtimes under one GIL; harder cancellation; larger native surface (Blast Radius) |
| Dict-based IR across boundary | No structural guarantee of parameterization; harder to evolve (Evolutionary Architecture) |
| SQL compilation in Python | Slower; harder to enforce allowlisting structurally; splits security model |
| Release GIL during compile | Sub-ms compile cost; GIL release adds overhead and complicates panic/cancel semantics |

**Default decision (ADR-001):** Python-side `asyncpg` driver; Rust as pure compiler/codec.

---

## 6. Async Execution Model

### 6.1 Runtime Shape

Ferrum runs inside the host application's `asyncio` event loop. There is **no tokio runtime** in v0.1.

```text
async def handler():
    qs = User.objects.filter(is_active=True).limit(10)   # sync, lazy — no I/O
    users = await qs.all()                                # async terminal — I/O here
```

**Execution phases:**

1. **Build (sync, Python):** QuerySet chaining builds an in-memory IR. No database contact.
2. **Compile (sync, GIL-held, Rust):** IR + metadata → `{sql, params}`. Sub-millisecond, not cancellable.
3. **Execute (async, Python/asyncpg):** Pool acquire → `connection.fetch(sql, *params)` → cancellable/timeoutable.
4. **Hydrate (sync, GIL-held, Rust→Python):** Raw rows → typed payloads → Pydantic instances.
5. **Observe (sync, Python):** Hook dispatch with Tier-selected payload.

### 6.2 GIL Considerations

| Phase | GIL | Cancellable | Notes |
|-------|-----|-------------|-------|
| QuerySet build | Held | N/A | Pure Python object graph |
| Rust compile | Held | No | Must complete in <1ms typical; no I/O inside Rust |
| asyncpg I/O | Released | Yes | Timeouts and `CancelledError` mapped here |
| Rust hydrate | Held | No | Batch decode; bounded by row count |
| Hook dispatch | Held | N/A | Tier A default; no bound values |

**Hard rule:** All cancellable waiting happens at the Python driver await point. Rust calls are short, synchronous, and bounded.

### 6.3 Concurrency Model

- **Shared-immutable metadata** (model allowlists, column maps) is safe across concurrent `asyncio` tasks without locks.
- **Per-call outputs** (compiled SQL, hydration buffers) are owned and never shared between in-flight requests.
- **Connection pool** is the only shared mutable runtime resource; it is owned and synchronized by asyncpg/Python.
- Independent `await qs.all()` calls on different QuerySets do not mutate each other's compiled state.

### 6.4 Failure Modes

| Failure | Detection | Surface to caller |
|---------|-----------|-------------------|
| Invalid field/operator | Rust compile (pre-SQL) | `FerrumCompileError` with field/operator context |
| Pool exhaustion | asyncpg acquire timeout | `FerrumConnectionError` |
| Query timeout | asyncio/asyncpg cancel | `FerrumTimeoutError` |
| PG constraint violation | asyncpg execution | Sanitized `FerrumIntegrityError` (no row values) |
| Rust panic | PyO3 boundary wrapper | Catchable `FerrumInternalError` (no addresses/paths) |


---

## 7. Package Layout

Ferrum ships as three versioned artifacts with independent release cadence where practical.

```text
ferrum/                          # repository root
├── python/
│   └── ferrum/                  # ferrum-py — published as `ferrum` on PyPI
│       ├── __init__.py          # public re-exports
│       ├── models.py            # Model base, metadata builder
│       ├── queryset.py          # lazy QuerySet, terminal ops
│       ├── connection.py        # pool, config, DSN parsing (redacted diagnostics)
│       ├── migrations/          # orchestrator, ledger, confirmation gates
│       ├── hooks.py             # Tier A/B/C dispatcher
│       ├── errors.py            # Ferrum taxonomy + boundary mapper
│       └── cli/                 # migration/init commands (scope per PM)
├── rust/
│   ├── ferrum-core/             # pure Rust engine (no PyO3)
│   │   ├── src/
│   │   │   ├── ir/              # QuerySetIR, ModelMetadata types
│   │   │   ├── compile/         # SQL compiler
│   │   │   ├── hydrate/         # row decoder
│   │   │   └── migrate/       # diff planner + SQL emitter
│   │   └── Cargo.toml
│   └── ferrum-pyo3/             # PyO3 extension crate
│       ├── src/lib.rs           # #[pymodule] boundary
│       └── Cargo.toml           # depends on ferrum-core
├── pyproject.toml               # maturin build, Python package metadata
└── tests/
    ├── python/                  # integration + security qualification
    └── rust/                    # compiler unit tests (with ferrum-core)
```

### 7.1 Package Responsibilities

| Package | Published name | Responsibility |
|---------|----------------|----------------|
| **ferrum-core** | (Rust crate, not on PyPI) | Pure compiler/codec: IR validation, SQL generation, hydration payloads, migration planning. No Python, no I/O. |
| **ferrum-pyo3** | Native extension wheel (`ferrum._native` or similar) | PyO3 bridge: type conversion, `Result`→exception mapping, panic catching. Thin — delegates logic to ferrum-core. |
| **ferrum-py** | `ferrum` on PyPI | Public API, async runtime, connection pool, hooks, errors, CLI. Depends on the native wheel. |

### 7.2 Versioning Strategy

- **ferrum-core** and **ferrum-pyo3** share a semver aligned with the Python package release.
- IR contract carries an explicit `version` integer; breaking IR changes require a major bump.
- Wheels built with `abi3` (CPython 3.10+) so one wheel per platform covers multiple Python minor versions (ADR-005).
- sdist fallback requires Rust toolchain for platforms without prebuilt wheels.

### 7.3 Build & Packaging (ADR-005)

| Target | v0.1 scope | Rationale |
|--------|------------|-----------|
| Linux manylinux x86_64 | Wheel | Primary server target |
| Linux manylinux aarch64 | Wheel | ARM server adoption |
| macOS arm64 | Wheel | Developer laptops |
| macOS x86_64 | Wheel | Legacy dev machines |
| Windows | sdist only | Defer wheel matrix cost to v0.2 |

Tooling: `maturin` for local/extension builds; `cibuildwheel` for release matrix.

---

## 8. Key Component Responsibilities

### 8.1 Model & Metadata Builder (Python)

- Intercepts Pydantic v2 model class definition.
- Derives persistence metadata: table name, column map, type mapping, allowlists.
- Produces immutable `ModelMetadata` snapshot registered at class creation time.
- Rejects unsupported field types at definition time where possible.

### 8.2 QuerySet (Python)

- Lazy, chainable filter/order/limit/offset builder.
- Terminal operations (`all`, `get`, `create`, `update`, `delete`, `count`) are coroutines.
- Builds `QuerySetIR` without hitting the database.
- Enforces danger API policy before IR emission for unscoped mutations.

### 8.3 SQL Compiler (Rust / ferrum-core)

- Validates IR against metadata allowlists.
- Emits PostgreSQL-parameterized SQL (`$1`, `$2`, …).
- Returns `sql_text`, `bound_params`, `param_type_summary`, and `fingerprint` (Tier A).
- Fails with structured `CompileError` before any SQL exists.

### 8.4 Connection Pool & Executor (Python)

- Wraps `asyncpg` pool with Ferrum config (DSN parsing, SSL mode, timeouts).
- Acquires connection, executes compiled query, returns raw rows.
- Maps driver exceptions through the error boundary.
- Never logs or hooks full DSN or passwords.

### 8.5 Hydration Pipeline (Rust + Python)

- Rust decodes asyncpg row representation into column-typed payloads.
- Python constructs Pydantic v2 instances via construct-without-revalidate fast path (ADR-003).
- Custom validators with side effects are not re-run on DB-origin data by default.

### 8.6 Migration Orchestrator (Python + Rust)

- Rust: schema diff → migration plan + SQL statements; emits the canonical plan digest used for confirmation-token binding.
- Python: dry-run output, destructive classification, confirmation gates, apply sequencing, and confirmation-token emit/validate (ADR-007).
- Transactional wrapper per migration step (ADR-004); non-transactional exceptions documented per step.
- Non-interactive destructive/non-dev apply is authorized only by a live-state-bound dry-run token (`--confirm-plan`) plus `--confirm-environment`; no generic `--force`/`--yes`/env-var-only bypass exists (ADR-007).

### 8.7 Hook Dispatcher & Redaction Layer (Python)

- Emits `query_start`, `query_success`, `query_failure`, `hydration_failure`, `migration_*` events.
- Default path emits Tier A only; Tier B/C require explicit Ferrum config keys.
- Centralized allowlist of hook payload keys; dev-only keys blocked in default path.

### 8.8 Error Boundary (Python)

- Single module maps: compile errors, asyncpg errors, PostgreSQL SQLSTATE, PyO3 panics.
- Produces stable Ferrum exception types with structured fields (model, field, category).
- Strips row values from PostgreSQL `DETAIL`/`HINT` by default.

---

## 9. Data Flow & State Ownership

### 9.1 Read Path

```text
User.objects.filter(x=1).limit(10).all()
  │
  ├─[Python] Build QuerySetIR (no I/O)
  ├─[Rust]   compile(metadata, ir) → CompiledQuery
  ├─[Python] hook: query_start (Tier A fingerprint)
  ├─[Python] pool.fetch(sql, *params)  ← async, cancellable
  ├─[Rust]   hydrate(metadata, rows) → RowPayload[]
  ├─[Python] construct Pydantic instances
  └─[Python] hook: query_success (duration, Tier A)
```

### 9.2 Write Path

```text
await User.objects.create(email="...")
  │
  ├─[Python] Pydantic validation (untrusted input)
  ├─[Python] Build insert IR from validated model
  ├─[Rust]   compile → INSERT ... RETURNING
  ├─[Python] execute + hydrate returned row
  └─[Python] return typed User instance
```

### 9.3 State Ownership

| State | Owner | Lifetime | Mutability |
|-------|-------|----------|------------|
| Model metadata allowlists | Python (built at class def) | Process lifetime | Immutable after build |
| QuerySet IR | Python (per chain) | Per QuerySet instance | Immutable after terminal op starts |
| Compiled query output | Rust (per compile call) | Per request/operation | Owned, not shared |
| Connection pool | Python/asyncpg | Process lifetime | Pool-managed |
| Migration ledger | Python (DB table or file) | Persistent | Append-only apply log |
| Hook config (Tier level) | Python app config | Process lifetime | Set at startup |

---

## 10. Persistence & Data Model (Overview)

Detailed field-level modeling is specified in `./DATA_MODELING.md`. This section defines architectural persistence boundaries.

### 10.1 Application Data

- Application tables are defined by user `Model` classes.
- Ferrum derives PostgreSQL DDL from Pydantic field annotations and Ferrum metadata.
- v0.1 supports scalar fields and explicit foreign-key integer columns (no relationship helpers).

### 10.2 Ferrum-Managed Schema

| Artifact | Purpose |
|----------|---------|
| Migration history table | Records applied migrations; prevents double-apply |
| (Optional) migration lock | Serialize apply in multi-process deploys |

### 10.3 Type Mapping Contract (v0.1 subset)

| Python/Pydantic | PostgreSQL | Notes |
|-----------------|------------|-------|
| `int` | `BIGINT` or `INTEGER` | PK default BIGINT |
| `str` | `TEXT` or `VARCHAR(n)` | Length from metadata if specified |
| `bool` | `BOOLEAN` | |
| `datetime` | `TIMESTAMPTZ` | UTC-normalized |
| `UUID` | `UUID` | |
| `Optional[T]` | nullable column | |
| `bytes` | `BYTEA` | |

### 10.4 Schema Evolution Rules

- Additive changes (new nullable column, new table) are non-destructive.
- Destructive changes (drop column/table, type narrowing, `NOT NULL` on populated column) require explicit confirmation gate.
- Migration planner classifies each operation; Python enforces gate before apply.


---

## 11. Security Architecture

Security requirements from the PRD are release-qualification gates. Architecture assigns enforcement points.

### 11.1 Defense in Depth Layers

```text
Layer 1: API guards (Python)
  └─ Danger APIs, unscoped mutation blocks, config gates for Tier B/C

Layer 2: IR validation (Rust)
  └─ Allowlist fields/operators/sorts; reject before SQL exists

Layer 3: SQL emission (Rust)
  └─ Parameterized values only; identifiers from metadata indices

Layer 4: Error sanitization (Python boundary)
  └─ Strip DETAIL/HINT row data; no DSN/password in exceptions

Layer 5: Observability redaction (Python hooks)
  └─ Tier A default; bound values never in default payloads

Layer 6: Migration gates (Python orchestrator)
  └─ Dry-run mandatory; destructive + non-dev confirmation
```

### 11.2 Security Requirements for Engineers

| ID | Requirement | Enforcement point | Test |
|----|-------------|-------------------|------|
| SQL-1 | Identifiers from metadata allowlists only | Rust compiler | Fuzz unknown fields → compile error, no SQL |
| SQL-2 | Values as bound parameters only | Rust compiler + IR shape | Assert no user input in sql_text |
| SQL-3 | No raw SQL escape hatches | API surface | No `extra()`, no string fragments |
| CRED-1 | No DSN/password in default hooks/errors | Python error + hook layers | Fixture DSN scan |
| LOG-1 | Tier A default; no bound values in hooks | Hook dispatcher | Payload schema tests |
| LOG-2 | Validation errors omit submitted values | Pydantic error mapper | Assert no echo |
| ERR-1 | Sanitized PG errors | Error boundary | SQLSTATE mapping tests |
| ERR-2 | PyO3 panics → catchable exceptions | ferrum-pyo3 wrapper | Panic injection tests |
| MIG-1 | Dry-run before apply | Migration orchestrator | Apply without dry-run fails |
| MIG-2 | Destructive confirmation gate | Migration orchestrator | Drop without confirm fails |
| MIG-5 | Unscoped bulk mutation danger API | QuerySet guards | `delete()` without filter fails |
| MIG-6 | Token unforgeable; replay after successful apply fails (ADR-007) | Live-state-bound, single-use/ledger token | Replay token post-apply → fail before mutation |
| MIG-7 | Token marked sensitive; CI uses secret injection (ADR-007) | Migration CLI docs + `migration_dry_run` hook | Docs/hook classify token sensitive; no token in public-log examples |
| MIG-8 | Token via secret channel (env/stdin), not argv; not an env-var *bypass* (ADR-007) | Migration apply CLI | Token readable from stdin/secret-env; no `--force`/`--yes`/env-only bypass exists |
| INIT-1 | Generated compose binds Postgres to `127.0.0.1` (ADR-008) | `ferrum init` compose template | Generated `docker-compose.yml` pins `127.0.0.1`, not `0.0.0.0` |
| INIT-2 | `.gitignore` excludes `.env`; init write-path allowlist (ADR-008) | `ferrum init` scaffold | Generated `.gitignore` ignores `.env`; init refuses writes outside cwd allowlist |

**SecurityEngineer notification:** SQL compilation, migration apply, hook payload schema, and TLS/`sslmode` connection hardening require SecurityEngineer review before v0.1 release qualification.

### 11.3 Connection Security

- DSN parsed in Python; password held in memory only for pool creation.
- Diagnostics allowlist: host, port, database, username, error category — never password or full DSN.
- TLS/`sslmode` documented in connection ADR follow-up (architecture-phase deliverable; see Open Items §16).

---

## 12. Architecture Decision Records (ADRs)

These decisions govern engineering implementation. ADR-001–006 are foundational architecture decisions; ADR-007 and ADR-008 cover the CLI/migration product contract and security review. ADR bodies and decisions are the authoritative record (see §12).

### ADR-001: PostgreSQL Driver Placement

**Decision:** Python-side `asyncpg` driver; Rust stays off the I/O path.

**Alternatives:** Rust async driver (sqlx/tokio-postgres) bridged to asyncio.

**Rationale:** Avoids dual-runtime complexity (Blast Radius), simplest cancellation model, smallest native dependency surface. Strangler-friendly: Rust can absorb driver later if benchmarks demand.

### ADR-002: QuerySet IR Contract

**Decision:** Typed, versioned `QuerySetIR` struct boundary; values out-of-band from identifiers.

**Alternatives:** Dict-based IR; SQL compilation in Python.

**Rationale:** Structural parameterization guarantee (Defense in Depth); Evolutionary Architecture for v0.2 relationships.

### ADR-003: Hydration Semantics

**Decision:** Pydantic v2 construct-without-revalidate for DB-origin rows by default.

**Alternatives:** Full Pydantic validation on every hydrate.

**Rationale:** Performance (Doherty threshold); DB is source of truth for stored types.

**Caveat:** Custom validators with side effects are skipped — document and offer opt-in full validation if needed.

### ADR-004: Migration Transactionality

**Decision:** Per-migration `BEGIN…COMMIT` wrapper for transactional DDL.

**Non-transactional exceptions (explicit per-step marking):**

- `CREATE INDEX CONCURRENTLY`
- Certain `ALTER TYPE` / enum operations
- Any PostgreSQL operation forbidden inside a transaction block

**Recovery:** Failure output states whether DB changed, which step failed, and documented recovery action.

**Related:** Confirmation gating for non-interactive apply is specified in ADR-007. ADR-004 governs *how* statements execute (transactionality); ADR-007 governs *whether* a destructive/non-dev apply is authorized to run.

### ADR-007: Non-Interactive Migration Confirmation Token

**Context:** `./PRODUCT_REQUIREMENTS.md` §Migration Safety puts headless CI/CD destructive apply in v0.1 scope, but **only** through a dry-run-scoped confirmation token. Security criteria (see `./SECURITY.md`) with residual-risk criteria are folded into the architecture here.

**Decision:** Destructive or non-development migration apply is authorized non-interactively only by an **opaque confirmation token** emitted by a prior `dry-run`, bound to (a) the exact reviewed plan, (b) the target environment, and (c) the sanitized database identity. The CLI contract is fixed by the PM decision:

- `ferrum migrations dry-run --env <target> --format json` emits the token.
- `ferrum migrations apply --env <target> --non-interactive --confirm-plan <token> --confirm-environment <target>` applies only when the token matches the current plan **and** target.
- Non-development apply requires `--confirm-environment` even when the plan is non-destructive (dual gate).

**Token mechanism (architecture, preserving product invariants):**

- The token is a deterministic binding over the canonicalized plan, normalized target identity, and a server-derived dry-run state marker. Validation re-derives the binding from live database/plan state at apply time and rejects on any mismatch. Concretely: `token = HMAC-or-hash(canonical_plan_digest ‖ target_env ‖ sanitized_db_identity ‖ dry_run_state_marker)`.
- Validation is a pure, cheap comparison relative to plan generation; **correctness over deploy speed** — drift detection always wins (CAP framing: prefer apply unavailability over applying a stale/inconsistent plan).
- The token carries **no secrets**: no password, full DSN, bind value, or row data. Database identity in the binding is the same allowlist used for connection diagnostics (host, port, database, username) — never the password.

**Failure modes (all fail *before* mutation, with re-run-dry-run guidance):** missing token · malformed token · token for another target · token for another plan · token stale after schema drift.

**Folds in security residual-risk criteria (release-qualification):**

| ID | Criterion | Architecture enforcement point |
|----|-----------|--------------------------------|
| MIG-6 | Token unforgeable without live dry-run state; replay after a successful apply fails | Bind `dry_run_state_marker` to pre-apply schema state; advance/consume marker on successful apply so the same token cannot re-authorize (single-use / ledger-bound) |
| MIG-7 | Dry-run docs mark the token as a sensitive capability; CI examples use secret injection, not public logs | Migration CLI docs + `migration_dry_run` hook classify token as sensitive; docs show secret-store injection |
| MIG-8 | Apply accepts the token via a secret channel (env/stdin) to avoid `ps`/argv leakage — distinct from the **banned** env-var-only *bypass* | CLI accepts `--confirm-plan` value via stdin or a named secret env reference; the env var supplies the *token*, never a destructive *bypass* |

**Anti-bypass invariant:** There is no generic `--force`, `--yes`, or env-var-only destructive bypass. MIG-8's secret channel transports the dry-run-derived token only; it never substitutes for a token. Any implementation that lets apply proceed destructively without a matching live-state token is an architecture defect, not an implementation shortcut.

**Alternatives considered:**

| Alternative | Why rejected |
|-------------|--------------|
| Generic `--force` / `--yes` flag | PM- and Security-banned; normalizes unsafe bypass (Defense in Depth) |
| Env-var-only confirmation (e.g. `FERRUM_CONFIRM=1`) | No binding to plan/target; trivially set in CI; banned by contract |
| Long-lived signed token (no live-state binding) | Replayable after drift or after a prior apply (MIG-6 violation) |
| Interactive prompt only (no headless path) | Blocks the in-scope automated CI/CD deploy outcome |

### ADR-008: `ferrum init` CLI Scaffold Scope

**Context:** `ferrum init` is in v0.1 scope as a Must-have for the under-30-minute prototype outcome; `ferrum dev-db` / Ferrum-managed container lifecycle is explicitly excluded.

**Decision:** `ferrum init` scaffolds **local files only** — a Ferrum config stub, a secret-free `.env.example`, and a local PostgreSQL `docker-compose.yml`. Ferrum does not own Docker lifecycle in v0.1; quickstart docs use standard `docker compose up -d postgres`. There is no `ferrum dev-db` command.

**Scaffold safety contract (preserves product invariants + folds in SecurityEngineer criteria):**

- **No silent overwrite:** running in a directory with existing target files previews planned actions and exits without writing; overwrite requires explicit confirmation or a scaffolding-only overwrite flag.
- **Secret-free output:** generated files contain placeholders or local-only synthetic defaults — never production hostnames, real credentials, tokens, DSNs, or PII.

| ID | Criterion | Architecture enforcement point |
|----|--------------------------|--------------------------------|
| INIT-1 | Generated `docker-compose.yml` binds Postgres to `127.0.0.1` | Compose template pins the published port to `127.0.0.1:5432:5432`, not `0.0.0.0` |
| INIT-2 | Generated `.gitignore` excludes `.env`; init writes are confined to a documented cwd allowlist | Scaffold ships a `.gitignore` that ignores `.env`; init writes only to the documented relative paths under cwd (no traversal/absolute targets) |

**Alternatives considered:**

| Alternative | Why rejected |
|-------------|--------------|
| `ferrum dev-db` managed container lifecycle | Out of v0.1 scope (YAGNI); standard `docker compose` is sufficient for quickstart |
| No scaffold command (manual setup) | Misses the under-30-minute first-query outcome (Least Astonishment for newcomers) |
| Scaffold real/shared DSN defaults | Credential sprawl / secret-commit risk (Defense in Depth) |

### ADR-005: Packaging Targets & CI Matrix

**Decision:** maturin + cibuildwheel; `abi3` wheels for Linux (x86_64, aarch64) and macOS (arm64, x86_64); Windows sdist-only in v0.1.

**Alternatives:** Full Windows wheels; pure-Python fallback without Rust.

**Rationale:** Balances addressable audience vs CI cost; packaging/CI matrix decision still open.

### ADR-006: Centralized Error & Hook Boundary

**Decision:** Single Python `errors` + `hooks` module owns SQLSTATE mapping, panic capture, and Tier A/B/C redaction. Non-bypassable for default path.

**Alternatives:** Per-component error formatting; hook consumers responsible for redaction.

**Rationale:** Defense in Depth — one hardened layer (Observability First).

---

## 13. Observability Architecture

### 13.1 Hook Events (v0.1)

| Event | When | Default tier |
|-------|------|--------------|
| `query_start` | Before asyncpg execute | Tier A |
| `query_success` | After hydrate | Tier A |
| `query_failure` | On compile/execute/hydrate error | Tier A |
| `hydration_failure` | Row decode/type mismatch | Tier A |
| `migration_dry_run` | After plan generation | Tier B (SQL text, no credentials) |
| `migration_apply_start/success/failure` | Apply lifecycle | Tier A |

### 13.2 Tier Contract

| Tier | Contents | Activation | Safe for |
|------|----------|------------|----------|
| A | fingerprint, operation, model/table, duration, status, failure category, param_count, param_type_summary | Default | Production / APM |
| B | normalized SQL with placeholders | `FERRUM_OBSERVABILITY_TIER=b` | Staging / dev |
| C | full SQL + bound values | `FERRUM_OBSERVABILITY_TIER=c` + local-dev guard | Local only — never APM |

Tier B/C never activate from `DEBUG=1` alone.

### 13.3 Failure Classification

`validation` · `compilation` · `connection` · `execution` · `migration` · `internal`

Each maps to a stable Ferrum exception type and hook `failure_category`.

---

## 14. Scaling Assumptions

Ferrum v0.1 targets single-process async Python services talking to a single-primary PostgreSQL instance.

| Dimension | v0.1 assumption | Growth path |
|-----------|-----------------|-------------|
| Concurrent requests | 100–1,000 async tasks per process | Scale horizontally via more app instances |
| Queries per request | 1–20 typical | N+1 is app responsibility (no prefetch in v0.1) |
| Connection pool | 10–50 connections per process | Tune via config; pool owned by Python |
| Compile latency | <1ms p99 per query | Rust compiler; no network |
| Hydration | <5ms p99 for 100-row result sets | Batch decode in Rust |
| Migration apply | Rare; serialized | Migration lock for multi-instance deploys |
| Data volume | OLTP row sizes; no bulk analytics path | Bulk ops deferred to v0.2 |

**CAP framing:** Single-primary PostgreSQL = CP under partition (prefer unavailability over inconsistency). Correct for an ORM; document so teams do not expect multi-region active-active semantics.

---

## 15. Extensibility Points

v0.1 defines extension seams without implementing full plugin machinery (YAGNI).

### 15.1 Custom Field Types (v0.2+ seam)

- Architecture reserves a `FieldCodec` trait (Rust) and Python registration hook.
- v0.1 ships built-in scalar codecs only.
- Custom types register: Python type ↔ PostgreSQL OID ↔ Rust encode/decode.

### 15.2 Custom Backends (out of v0.1 scope)

- IR compiler and migration planner are PostgreSQL-dialect-specific in v0.1.
- A future backend would require: dialect-specific compiler module, driver adapter interface, and migration planner swap.
- No multi-database abstraction layer in v0.1 — explicit non-goal.

### 15.3 Hook Consumers

- Applications register callables on the hook dispatcher.
- Contract: Tier A payloads only unless app opts into receiving Tier B/C from Ferrum config.
- Hook integration guide warns against logging bound values or model bodies.

---

## 16. Open Items

- **Packaging/CI matrix (ADR-005):** final wheel targets not yet locked; implementation can proceed with Linux/macOS dev builds.
- **TLS/`sslmode` connection hardening:** connection security docs and `sslmode` defaults still needed; blocks release qualification.
- **ADR records to be finalized:** ADR-001–ADR-008 bodies live in §12; a separate formal record (e.g. `DECISIONS.md`) has not yet been produced.

---

## 17. Engineer Handoff

**First implementation slices (recommended order):**

1. `ferrum-core` IR types + compile unit tests (no PyO3).
2. `ferrum-pyo3` boundary with panic-safe wrapper.
3. `ferrum-py` Model metadata builder + QuerySet lazy builder.
4. Connection pool + read path (`filter` → `all`).
5. Error boundary + Tier A hooks.
6. Write path (`create`, `update`, `delete` + danger API).
7. Migration planner + dry-run + apply gates.

**Out of scope for v0.1:**

- Relationship loaders or prefetch
- Sync API wrappers
- Raw SQL escape hatches
- Multi-database drivers
- Production HTTP query inspection
