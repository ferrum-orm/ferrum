# Ferrum Architecture (public overview)

A developer-facing distillation of the internal architecture contract. Ferrum is an async
ORM **library** embedded in your Python process — not a standalone service. Its only
external dependencies are PostgreSQL and the host Python runtime.

> This is the public overview. The authoritative internal contract lives in
> `.claude/docs/ARCHITECTURE.md`.

---

## The layers

Ferrum splits cleanly along one seam: **Python owns ergonomics and async I/O; Rust owns
pure, synchronous compilation and codec work.**

```mermaid
flowchart TB
    subgraph App["Host Application (FastAPI / Starlette / script)"]
        Handler["Route handlers · services · CLI"]
    end

    subgraph Py["ferrum-py — Python package (async, I/O, ergonomics)"]
        Model["Model + metadata builder"]
        QS["QuerySet (lazy, chainable)"]
        Conn["Connection pool (asyncpg)"]
        Hooks["Hook dispatcher (Tier A/B/C)"]
        Errors["Error boundary + redaction"]
        Mig["Migration orchestrator + gates"]
    end

    subgraph Bridge["ferrum-pyo3 — PyO3 extension bridge"]
        Boundary["Result→exception · panic=unwind catch · GIL held"]
    end

    subgraph Core["ferrum-core — Rust engine (pure, sync, stateless)"]
        IR["IR validator (allowlists)"]
        SQL["SQL compiler (Postgres dialect)"]
        Codec["Bound-param encoder · row codec"]
        Planner["Migration diff planner"]
    end

    PG[("PostgreSQL")]

    Handler --> Model
    Handler --> QS
    QS -->|"QuerySetIR + ModelMetadata (typed, versioned)"| Boundary
    Boundary --> IR
    IR --> SQL
    SQL --> Codec
    Codec -->|"sql_text + bound_params"| Boundary
    Boundary -->|"compiled output"| QS
    QS -->|"parameterized SQL (asyncpg)"| Conn
    Conn -->|"async I/O"| PG
    Mig --> Planner
    Mig -->|"DDL"| Conn
```

**Who owns what**

| Concern | Owner | Why |
|---------|-------|-----|
| Public API, models, QuerySet | Python | Django/Pydantic mental model (Least Astonishment) |
| Connection pool, transactions, cancellation, timeouts | Python | I/O lives where failure scope is owned |
| Hook dispatch + payload redaction | Python | Non-bypassable redaction at dispatch |
| Migration apply + confirmation gates | Python | Destructive gates fire before SQL reaches the DB |
| IR validation + SQL compilation | Rust | CPU-bound, sub-millisecond, pure |
| Bound-param encoding + row hydration | Rust | Hot path, off the I/O thread |

Rust never performs I/O, never runs async, and holds no per-request mutable state. The
compile call holds the GIL and returns fresh owned output per call.

---

## The query lifecycle

How a `QuerySet` becomes rows. SQL is only ever built in Rust; Python builds the IR.

```mermaid
sequenceDiagram
    participant App
    participant QS as QuerySet (Python)
    participant Meta as ModelMetadata (allowlist)
    participant Rust as ferrum-core (via PyO3)
    participant PG as PostgreSQL (asyncpg)
    participant Hook as Hook dispatcher

    App->>QS: filter(...).order_by(...)
    Note over QS: pure chaining, no I/O
    App->>QS: await all(conn)
    QS->>Meta: validate fields / operators / sort dirs
    alt unknown field or operator
        Meta-->>App: raise FerrumCompileError (before any SQL)
    end
    QS->>QS: build IR dict (values out-of-band from identifiers)
    QS->>Rust: compile_query(metadata_json, ir_json)  [GIL held]
    Rust-->>QS: { sql_text, bound_params, fingerprint }
    QS->>Hook: query_start (Tier A: identifiers only)
    QS->>PG: pool.fetch(sql_text, *bound_params)
    PG-->>QS: rows (trusted source)
    QS->>Hook: query_success (duration, row count)
    QS->>QS: hydrate via model_construct (ADR-003 fast path)
    QS-->>App: list[Model]
```

Key invariants visible here:

- **Allowlist gate first.** Field names, operators, and sort directions are checked against
  `ModelMetadata` in Python *before* the Rust compiler runs. Bad input fails as
  `FerrumCompileError` before any SQL exists.
- **Values travel out-of-band.** The IR carries identifiers (validated) separately from
  values (bound parameters). User strings never interpolate into SQL.
- **Trusted hydration.** Rows come from the DB, so the default path uses Pydantic's
  `model_construct` (skip re-validation) for speed (ADR-003).

---

## Security model (gates)

These are release-qualification gates, enforced and test-covered:

```mermaid
flowchart LR
    In["User input (filter values, field names)"] --> G1{"Field / operator<br/>in allowlist?"}
    G1 -- no --> R1["FerrumCompileError<br/>(no SQL emitted)"]
    G1 -- yes --> IR["IR: identifiers ≠ values"]
    IR --> Comp["Rust compiler<br/>(parameterized SQL only)"]
    Comp --> Exec["asyncpg bound params"]
    Exec --> Obs{"Observability tier"}
    Obs -- "A (default)" --> TA["identifiers, duration, status<br/>NO values / DSN / rows"]
    Obs -- "B (opt-in)" --> TB["+ normalized SQL"]
    Obs -- "C (local-dev only)" --> TC["+ full SQL + bound values"]
```

| Gate | Guarantee |
|------|-----------|
| **SQL safety** | No user input in identifier or value positions; identifiers from allowlists, values as bound params; bad input fails before emission. |
| **Credential handling** | DSNs/passwords never in hooks, errors, logs, or migration output. Connection diagnostics are an allowlist: host/port/db/user/category. |
| **Tiered observability** | Tier A by default. B/C require explicit `FERRUM_OBS` opt-in (never `DEBUG=1`). Tier C is local-dev only. |
| **Error boundary** | DB/PyO3/Postgres errors map to a sanitized Ferrum taxonomy. Panics become catchable exceptions, not aborts. |
| **Migration safety** | Mandatory dry-run; destructive + non-dev applies require explicit confirmation; unscoped writes require the named danger API. |

---

## Migration flow

```mermaid
flowchart TB
    Models["Model classes"] --> CP["compute_plan(models, existing_tables)"]
    CP --> PlanJSON["plan JSON (additive ops)"]
    PlanJSON --> Apply["apply(conn, plan_json, ...)"]
    Apply --> DryGate{"dry_run?"}
    DryGate -- "True (default)" --> Print["print plan · apply nothing"]
    DryGate -- "False" --> TokGate{"token supplied<br/>(with confirm)?"}
    TokGate -- "yes & invalid" --> Err1["FerrumMigrationError"]
    TokGate -- "yes & valid" --> DestGate{"destructive op?"}
    TokGate -- "no token" --> DestGate
    DestGate -- "yes & not confirmed" --> Err2["FerrumMigrationError"]
    DestGate -- ok --> EnvGate{"non-dev & not confirmed?"}
    EnvGate -- yes --> Err3["FerrumMigrationError"]
    EnvGate -- ok --> Run["execute DDL (double-quoted identifiers)"]
    Run --> Result["MigrationResult(applied=True, ...)"]
```

The destructive gate **independently scans the ops** and never trusts the plan's own
`requires_confirmation` flag — a crafted plan JSON cannot lie its way past the gate. DDL
identifiers are always double-quoted and sourced from model-metadata allowlists; SQL types
and defaults are validated against fixed allowlists before interpolation.

---

## Crate / package map

| Component | Path | Role |
|-----------|------|------|
| `ferrum` (Python) | `python/ferrum/` | Public API: models, QuerySet, connection, errors, hooks, migrations, CLI, contrib. |
| `ferrum-pyo3` | `crates/ferrum-pyo3/` | PyO3 bridge. Exposes `compile_query`, `hydrate_rows`, `plan_migration`; maps `Result`/panics to catchable exceptions. |
| `ferrum-core` | `crates/ferrum-core/` | Pure engine: IR validator, compile + hydrate, migration planner. |
| `ferrum-sql` | `crates/ferrum-sql/` | SQL emitter (PostgreSQL dialect). |
| `ferrum-migrate` | `crates/ferrum-migrate/` | Migration planning support. |

The IR crossing the boundary is **typed, versioned, and serializable** (ADR-002 resolved),
with identifiers carried out-of-band from values so parameterization and allowlisting are
structural, not conventional.

---

## Architecture decisions (ADRs)

All original ADRs are resolved. The table below records the decisions for reference.

| ADR | Topic | Resolution |
|-----|-------|-----------|
| ADR-001 | PostgreSQL driver placement | Python-side `asyncpg` (`ferrum.drivers.postgres`; `ferrum-orm[pg]`). |
| ADR-002 | QuerySet → Rust IR contract | IR v2 JSON contract (`crates/ferrum-core/src/ir/`); versioned via `QuerySet._IR_VERSION`. |
| ADR-003 | Hydration semantics | `model_construct` (construct-without-revalidate) fast path; trusted DB-origin rows. Custom-validator caveat documented. |
| ADR-004 | Migration transactionality | Transactional by default; `non_transactional` classification in `operations.py` for `CREATE INDEX CONCURRENTLY` and certain `ALTER TYPE`/enum ops. |
| ADR-005 | Packaging / CI wheel matrix | maturin + cibuildwheel abi3 wheels; `release.yml` publishes to PyPI via OIDC on `v*` tag push. |
| ADR-006 | Centralized error/hook redaction | `errors.py` (`map_db_error`/`map_native_error`); Tier A/B/C hook payloads in `hooks.py`. |
