---
task_id: w4-b-performance
wave: wave-4
owner: production-readiness-executor
status: in_progress
run_id: 20260829T095632Z
shared_path_lease: null
dependencies:
  - w1-a-query-correctness
  - w1-e-pool-lifecycle
  - w4-a-observability
owned_paths:
  - crates/ferrum-core/benches/hydrate.rs
  - crates/ferrum-sql/benches/compile.rs
  - tests/python/integration/test_performance.py
  - tests/python/integration/test_benchmarks.py
security_triage_complete: true
security_surfaces:
  sql_compilation: false
  migration_apply: false
  errors_redaction: false
  auth_secrets: false
  rls_admin_gucs: false
  schema_selection: false
security_review: false
security_review_justification: Performance benchmarks and regression gates; no security surfaces
architecture_review: true
product_review: false
code_review: true
---

# Task: End-to-end performance and regression gates

## Specify

### Problem

There are no benchmarks comparing Ferrum versus raw asyncpg and SQLAlchemy async on
identical schemas/workloads. No throughput, p50/p95/p99, CPU, allocations, memory peak,
pool saturation, or compile/hydration split measurements. No stable threshold/regression
reporting in nightly CI.

### Scope

`crates/ferrum-core/benches/hydrate.rs`, `crates/ferrum-sql/benches/compile.rs`,
`tests/python/integration/test_performance.py`, `tests/python/integration/test_benchmarks.py`.
Do NOT modify `observability.py` (W4-A owns, complete — import only for metrics).

### Non-goals

No `observability.py` / `hooks.py` edits (W4-A owns, complete — import only). No
`queryset.py` / `connection.py` / `drivers/` edits (W1/W2 own — import only). No
`pyproject.toml` / `Cargo.toml` edits (shared paths — not leased; record findings in
log). No `__init__.py` edits. No `README.md` / `CHANGELOG.md`. No packaging changes (W4-D).

### Invariants and failure modes

Benchmark Ferrum versus raw asyncpg and SQLAlchemy async. Measure throughput, p50/p95/p99,
CPU, allocations, memory peak, pool saturation, compile/hydration split. Profile the
JSON/msgpack boundary and hydration validation copy. Stable threshold/regression reporting
in nightly CI. Avoid flaky hard gates until variance is characterized.

### Acceptance criteria

- Benchmarks for: point reads, filtered pages, relation loads, writes, bulk operations,
  JSONB, vector KNN, streaming, and transactions.
- Measure throughput, p50/p95/p99, CPU, allocations, memory peak, pool saturation.
- Compile/hydration split measurements.
- Profile JSON/msgpack boundary and hydration validation copy.
- Stable threshold/regression reporting in nightly CI.
- Avoid flaky hard gates until variance is characterized.
- No unexplained material regression versus raw asyncpg/SQLAlchemy async baselines.

## Plan

Extend Rust benchmarks (hydrate, compile). Add Python integration performance tests.
ChiefArchitect for the benchmark architecture; CodeReviewer required. SecurityEngineer
not required.

## Tasks

1. Audit existing Rust benchmarks and identify gaps.
2. Extend `hydrate.rs` benchmark for hydration performance.
3. Extend `compile.rs` benchmark for SQL compilation performance.
4. Add Python performance tests: point reads, filtered pages, relation loads, writes,
   bulk, JSONB, vector KNN, streaming, transactions.
5. Measure throughput, p50/p95/p99, CPU, allocations, memory peak, pool saturation.
6. Profile compile/hydration split and JSON/msgpack boundary.
7. Add stable threshold/regression reporting (avoid flaky hard gates).
8. Focused checks plus `mise run ci-local`.

## Implement

Coordinator marked `in_progress` at `20260829T095632Z` with exclusive owned paths and
no shared-path lease. Import from `observability.py` for metrics — do NOT modify it.

## Validation contract

Rust benchmark compilation (`cargo bench --no-run`), Python performance tests, then
`mise run ci-local`.

## Independent verification contract

Verifier proves benchmarks run, measurements are recorded, and no flaky hard gates.
Named gates: ChiefArchitect, CodeReviewer. SecurityEngineer `not_required`.
ProductManager `not_required`.

## Revert contract

Revert only owned bench/test files from this run. Preserve all other workstreams.
