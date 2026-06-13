# Wave 0 Backlog — Ferrum v0.1 Implementation Order

**Author:** product-manager (Wave 0 governance)  
**Date:** 2026-06-13  
**Inputs:** [PRODUCT_REQUIREMENTS.md](../.claude/docs/PRODUCT_REQUIREMENTS.md) Must-haves, [ARCHITECTURE.md §17](../.claude/docs/ARCHITECTURE.md), parallel implementation plan

---

## Purpose

Ordered backlog mapping PRD **Must-have** requirements to architecture **implementation slices** and **implementation waves**. This is the scope contract Wave 1+ agents implement against. Wave 0 does not add features — it locks order and traceability.

---

## PRD Must-have → Slice Traceability

| # | PRD Must-have | Primary slice(s) | Wave |
|---|---------------|------------------|------|
| M1 | Async PostgreSQL CRUD (QuerySet-style API) | Slice 3 (Model/QuerySet), Slice 4 (read path), Slice 6 (write path) | 1–3 |
| M2 | Pydantic v2 model definitions drive schema/validation | Slice 3 (Model metadata builder) | 1 |
| M3 | Rust/PyO3 parameterized SQL generation | Slice 1 (ferrum-core compile), Slice 2 (PyO3 bridge) | 1–2 |
| M4 | Migration plan, dry-run, destructive gates, apply | Slice 7 (migration planner + orchestrator) | 4 |
| M5 | `ferrum init` quickstart scaffolding | CLI/init (ADR-008; parallel to Slice 7) | 4 |
| M6 | Query observability hooks (duration, safe SQL context, failure class) | Slice 5 (error boundary + Tier A hooks) | 3 |
| M7 | Security & data protection (SQL, credentials, observability, errors, migrations) | Cross-cutting; test-engineer maps §11.2 IDs every wave | 1–5 |
| M8 | Documentation (positioning vs SQLAlchemy/Tortoise/Django) | README + docs; update on public API change | 5 |

Security requirements (M7) are release-qualification gates, not a separate slice — they attach to the enforcement point in each wave (see ARCHITECTURE §11.2).

---

## Ordered Backlog (Architecture §17 + PRD)

Each item is a **mergeable unit of work**. Order respects the critical path: IR contract (Wave 0) → compile → boundary → Python builder → I/O → observability → writes → migrations → ship.

### Slice 1 — `ferrum-core` IR + compile (Wave 1, Track A)

**PRD:** M3, M7 (SQL-1, SQL-2)  
**Status:** Started (`crates/ferrum-core`, `crates/ferrum-sql`)

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 1.1 | Canonize ADR-002 IR v1 JSON contract (Wave 0) | `adr-002-ir-contract.plan.md` + architect **Aligned** verdict |
| 1.2 | Complete allowlist validation in `compile/mod.rs` | Unknown field/operator/sort → `CompileError` before SQL |
| 1.3 | Complete PostgreSQL `$N` emission in `ferrum-sql` | Values only in `bound_params`; fuzz tests pass |
| 1.4 | Add `fingerprint` to `CompiledQuery` | Tier A observability key per QUERY_ENGINE §5.2 |
| 1.5 | Rust unit + proptest rejection tests | SQL-1/2 coverage; no user input in `sql_text` |

**Verify:** `cargo test --workspace`, `cargo clippy --workspace -- -D warnings`

---

### Slice 2 — `ferrum-pyo3` boundary (Wave 2, Track A)

**PRD:** M3, M7 (ERR-2)  
**Status:** JSON interim bridge started

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 2.1 | Wire real compile path (already partial) | `compile_query(metadata_json, ir_json)` → dict with sql/params/summary |
| 2.2 | Add `hydrate_rows` with panic=unwind + catch | ERR-2 tests; catchable `FerrumInternalError` |
| 2.3 | Keep `python/ferrum/_native.pyi` in sync | mypy + integration stub-drift check |

**Verify:** boundary integration tests (Wave 2)

---

### Slice 3 — Python Model metadata + QuerySet IR builder (Wave 1, Track B)

**PRD:** M1, M2, M7 (MIG-5/QE-6)  
**Status:** Scaffold in `models.py`, `queryset.py`

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 3.1 | Immutable `ModelMetadata` builder per DATA_MODELING.md | Built at class def; read-only allowlists |
| 3.2 | `QuerySet._build_ir()` emitting ADR-002 v1 JSON | Round-trip deserializes in Rust tests |
| 3.3 | Danger API guards before IR emission | Unscoped `delete`/`update` → error, no SQL |
| 3.4 | Chain methods: `filter`, `exclude`, `order_by`, `limit`, `offset` | Immutable chaining; no I/O |

**Verify:** `mise run lint-python type-python test-python-unit`

---

### Slice 4 — Connection pool + read path (Wave 2, Track B)

**PRD:** M1, M7 (CRED-1)  
**Status:** `connection.py` scaffold

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 4.1 | asyncpg pool + redacted diagnostics | CRED-1: no password/full DSN in errors |
| 4.2 | Terminal ops: `all`, `first`, `get`, `count` | compile → execute → hydrate round trip |
| 4.3 | Cancellation/timeout at Python await only | No cancellable Rust I/O |

**Verify:** `mise run dev test-integration`

---

### Slice 5 — Error boundary + Tier A hooks (Wave 3, Track A)

**PRD:** M6, M7 (LOG-1, ERR-1, ADR-006)  
**Status:** `errors.py`, `hooks.py` scaffold

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 5.1 | Centralized Ferrum taxonomy in `errors.py` | SQLSTATE mapping; sanitized PG DETAIL/HINT |
| 5.2 | Tier A hook dispatcher in `hooks.py` | Default payloads: fingerprint, op, duration, category — no bound values |
| 5.3 | Tier B/C opt-in gates | Never activate from `DEBUG=1` alone |

**Verify:** LOG-1/ERR-1 security tests

---

### Slice 6 — Write path + danger APIs (Wave 3, Track B)

**PRD:** M1, M7 (MIG-5)  
**Status:** Danger guards started in queryset tests

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 6.1 | `create`, scoped `update`, scoped `delete` | INSERT/UPDATE/DELETE compile + execute |
| 6.2 | `danger_delete_all`, `danger_update_all` | Explicit IR flag / operation path |
| 6.3 | ADR-003 hydration on write RETURNING | construct-without-revalidate for DB-origin rows |

**Verify:** write-path unit + integration tests

---

### Slice 7 — Migration planner + dry-run + apply gates (Wave 4)

**PRD:** M4, M7 (MIG-1–MIG-8, ADR-004, ADR-007)  
**Status:** Rust `migrate/mod.rs` + Python orchestrator scaffold

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 7.1 | Rust schema diff → plan + SQL | Deterministic plan digest |
| 7.2 | Python orchestrator: dry-run mandatory | Apply without dry-run fails (MIG-1) |
| 7.3 | Destructive + non-dev confirmation gates | Token binding per ADR-007 |
| 7.4 | Per-step transactionality (ADR-004) | BEGIN…COMMIT default; documented exceptions |
| 7.5 | `ferrum init` scaffold (ADR-008) | INIT-1/INIT-2 security criteria |

**Verify:** full MIG-* and INIT-* security suite

---

### Slice 8 — Ship readiness (Wave 5)

**PRD:** M8 + full acceptance checklist

| ID | Backlog item | Acceptance |
|----|--------------|------------|
| 8.1 | PM acceptance checklist vs PRD | All Must-have acceptance criteria met |
| 8.2 | `pytest -m security` green | Release-qualification gates |
| 8.3 | README + comparison docs updated | Public API documented |
| 8.4 | `mise run ci-local` green | Full local CI gate |

---

## Cross-cutting: Test & Security Inventory (every wave)

**Owner:** test-engineer (parallel from Wave 1)

| Wave | Security ID focus |
|------|-------------------|
| 1 | SQL-1, SQL-2, SQL-3, QE-1, QE-2, QE-6 |
| 2 | CRED-1, ERR-2, integration round trip |
| 3 | LOG-1, LOG-2, ERR-1, MIG-5 |
| 4 | MIG-1–MIG-8, INIT-1, INIT-2 |
| 5 | Full security qualification |

---

## Explicitly Out of Scope (v0.1)

Per PRD Won't-have and ARCHITECTURE §17:

- Sync API / blocking wrappers
- SQLite, MySQL, multi-DB
- Relationship loaders, prefetch, `select_related`
- Raw SQL escape hatches (`extra()`, string fragments)
- `ferrum dev-db` container lifecycle
- Production HTTP query inspection (Tier C local-dev only)

---

## Wave Gate Summary

| Wave | Entry condition | Exit gate |
|------|-----------------|-----------|
| 0 | Plan approved | Architect **Aligned** + ADR-002 canonized |
| 1 | Wave 0 exit | Chief-architect IR compatibility; security-engineer SQL-1/2/3 |
| 2 | Wave 1 merge | Security + code-reviewer on boundary/connection |
| 3 | Wave 2 merge | Security on hooks/errors/write SQL |
| 4 | Wave 3 merge | Security on migrations/init (mandatory) |
| 5 | Wave 4 merge | PM acceptance + `ci-local` green |

---

## Recommended First Sprint (Wave 1)

After Wave 0 **Aligned** verdict, launch in parallel:

1. **rust-core-engineer:** Items 1.2–1.5  
2. **python-orm-engineer:** Items 3.1–3.4  
3. **test-engineer:** Security ID → test map; failing-until-ready gates  
4. **product-designer:** Model/QuerySet API vs PRODUCT_DESIGN.md

**Blocker removed:** ADR-002 IR v1 JSON contract is canonized; Python and Rust may proceed against shared schema.
