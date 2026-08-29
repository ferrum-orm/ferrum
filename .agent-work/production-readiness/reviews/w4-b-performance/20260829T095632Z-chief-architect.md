---
task_id: w4-b-performance
run_id: 20260829T095632Z
authority: ChiefArchitect
reviewer: ChiefArchitect
reviewed_at: 2026-08-29T14:00:00Z
base_revision: 612f476c32fa7b1fbd38e4dc9f4c689d05b72191
decision: approved
scope:
  - crates/ferrum-core/benches/hydrate.rs
  - crates/ferrum-sql/benches/compile.rs
  - tests/python/integration/test_performance.py
  - tests/python/integration/test_benchmarks.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

- Benchmark architecture covers the workload classes named in the task contract
  (point reads, filtered pages, relation loads, writes, bulk, JSONB, vector KNN,
  streaming, transactions) with a compile/hydration split.
- Measurement metrics include throughput, p50/p95/p99, CPU, memory peak, and pool
  saturation.
- The JSON/msgpack PyO3 wire-format boundary and hydration validation copy are
  profiled as distinct cost centers.
- Stable threshold/regression reporting is soft (no flaky hard CI gates until
  variance is characterized).
- The benchmark design honors the §2 architectural constraints (Rust owns
  pure compile/hydrate; Python owns I/O and orchestration; no raw SQL escape
  hatch in the Ferrum path; no per-request mutable Rust state).

## Evidence

### Rust benchmark compilation (fresh run)

```
$ cargo bench --no-run --bench hydrate --bench compile
  Compiling ferrum-core v0.1.17
  Compiling ferrum-sql v0.1.17
  Finished `bench` profile [optimized] target(s) in 2.81s
  Executable benches/hydrate.rs (target/release/deps/hydrate-ca7076ed5b2f02ff)
  Executable benches/compile.rs (target/release/deps/compile-15ac6a64eae3ce62)
```

Both bench binaries compile against the base revision's IR. Confirmed via the
verifier that all IR types used (`Predicate`, `Aggregation`, `BulkUpdateRow`,
`JoinSpec`, `JoinKind`, `JoinFieldRef`, `GroupExpression`, `Having`,
`HavingOperator`, `TextRankBy`, `TextSearchMode`, `VectorMetric`,
`VectorOrderBy`, `AggregateExpression`, `AggregateFunction`,
`FieldType::Vector`, `FieldType::TsVector`, `FieldType::ArrayText`,
`emit_insert`, `emit_update`, `emit_delete`, `emit_bulk_insert`,
`emit_bulk_update`, `emit_bulk_delete`) exist at `612f476`. No hidden dependency
on parallel-workstream IR changes.

### Workload coverage (architecture assessment)

**`crates/ferrum-core/benches/hydrate.rs`** — 5 criterion groups:
- `hydrate_rows` (1/10/100/1000 rows, 4-field Post model) — original, retained.
- `hydrate_rows_wide` (20-column row, mixed Int/Text/Bool/Float/Json) —
  per-field validation cost scaling with column count.
- `hydrate_rows_jsonb` (1/100 rows, UUID + ArrayText + Json + Vector(8)) —
  ticket-analyzer workload shape; type fidelity on the hydration codec.
- `validation_copy` (same wide rows, all-nullable vs all-non-nullable metadata)
  — the delta isolates the `validate_row` walk over non-nullable fields. Sound
  differential design: identical input rows, only metadata varies.
- `json_boundary` (encode/decode at 1/100/1000) — the throwaway `serde_json`
  round-trip the PyO3 bridge makes for Rust structural validation, isolated from
  `hydrate_rows`. This is the §4 PyO3-boundary copy cost (`_RowEncoder`).

**`crates/ferrum-sql/benches/compile.rs`** — 3 criterion groups, 13 scenarios:
- `compile_select`: filtered, join (`select_related` LEFT, project_remote=true),
  relation_filter (INNER, project_remote=false), aggregate (GROUP BY + COUNT/SUM
  + HAVING), vector_knn (pgvector KNN ORDER BY), text_rank (FTS `rank_by`),
  predicate (`Q` AND/OR/NOT tree).
- `compile_write`: insert (RETURNING), update (scoped), delete (scoped).
- `compile_bulk`: bulk_insert / bulk_update / bulk_delete at 10/100/1000 rows.

The compile/hydration split is architecturally clean: compile benches live in
`ferrum-sql` (the emitter), hydrate benches live in `ferrum-core` (the codec).
No cross-crate coupling; each bench exercises exactly one pure function over
`(&Metadata, QuerySetIR)` or `(&Metadata, Vec<RowPayload>)`, consistent with §2.10
(compilation is a pure function, fresh owned output per call) and §2.2 (Rust
owns pure compile/codec, off the async I/O path).

**`tests/python/integration/test_performance.py`** — 14 workloads:
point_read, filtered_page, select_related, prefetch_related, write, bulk_create,
jsonb_read, vector_knn, streaming, transaction, wire_json, wire_msgpack,
compile_hydration_split, sqlalchemy_comparison (conditional). Each records a
`Sample` with throughput, p50/p95/p99, mean, cpu_user_ms, cpu_sys_ms,
mem_peak_kb, pool_acquired_peak.

**`tests/python/integration/test_benchmarks.py`** — 6 regression-gate tests:
rust_benchmarks_compile (hard, deterministic), rust_benchmarks_run_and_record
(opt-in via `FERRUM_BENCH_RUN_RUST=1`), perf_results_structure (structural),
regression_report_is_soft (soft — asserts only that the report ran),
no_hard_latency_gates (meta-check), create_baseline_if_requested (helper).

### Measurement metrics (fresh run)

```
$ FERRUM_TEST_DSN=... uv run pytest ... -m benchmark
  [ferrum  ] point_read   thr=1056.6 ops/s p50=883.2us p95=1245.6us p99=1724.9us cpu=122.7ms mem=444.8KB pool=0
  [asyncpg ] point_read   thr=1353.8 ops/s p50=636.6us p95=1376.7us p99=1602.7us cpu=48.3ms  mem=275.1KB pool=0
  ... (12 workloads, all produce complete Sample records)
  14 passed, 4 skipped in 26.25s
```

All six required metrics (throughput, p50, p95, p99, CPU, memory peak, pool
saturation) are captured for every workload. Ferrum-vs-asyncpg ratios are
ORM-expected (1.3x–10x slower; no catastrophic regression).

### JSON/msgpack boundary (architecture assessment)

- Rust `json_boundary` bench isolates `serde_json::to_vec` / `from_slice` —
  the throwaway PyO3-boundary copy for Rust structural validation (§4 / AGENTS.md
  learned-fact: `_RowEncoder`).
- Python `test_bench_wire_format_boundary` toggles `FERRUM_WIRE_FORMAT` between
  `json` and `msgpack` on a fresh connection, comparing hydration throughput.
- msgpack is NOT measured in the Rust bench because `rmp-serde` is a
  `ferrum-sql` dev-dep, unavailable to `ferrum-core` benches. This is
  architecturally correct: msgpack lives in the Python wire-format layer; the
  Rust codec is wire-format-agnostic. The JSON boundary is the default and the
  one that crosses the PyO3 boundary; profiling it in Rust is the right layer.

### No flaky hard gates (fresh run)

```
$ uv run pytest tests/python/integration/test_benchmarks.py::test_no_hard_latency_gates ...
  2 passed in 2.32s
```

`test_no_hard_latency_gates` scans both bench modules for
`assert ... (p50|p95|p99|mean|duration|latency) ... [<>] [1-9]` and rejects any
match. The only hard assertions in the suite are: compile success
(`result.returncode == 0`), nonzero throughput (`throughput_ops_per_s > 0`), and
structural field presence (`not missing`). None are latency-bound. The
regression report (`test_regression_report_is_soft`) asserts only that the
report path executed — a regression beyond `REGRESSION_FACTOR` (default 3x) is
flagged in output, never fails CI. This satisfies the task contract's "Avoid
flaky hard gates until variance is characterized."

### §2 constraint adherence

- **§2.2 (Rust owns pure compile/codec, off async I/O):** Rust benches call
  `emit_*` / `hydrate_rows` synchronously with `black_box`; no async, no I/O.
  Python perf tests own the asyncpg pool, Ferrum `Connection`, and timing.
- **§2.9 (no raw SQL escape hatches):** The Ferrum query path in perf tests uses
  `Item.objects.get(...)`, `.filter(...).all(conn)`, `.bulk_create(...)` —
  the typed QuerySet surface. The raw SQL strings (`f"SELECT * FROM {names[...]}"`)
  are the **asyncpg comparison baseline**, not the Ferrum path. Table identifiers
  are test-controlled `os.urandom(4).hex()` suffixes, not user input; the
  `# ruff: noqa: S608` annotation documents the intentional lint suppression.
  This is architecturally sound — the benchmark compares Ferrum against raw
  asyncpg on identical schemas, and raw asyncpg requires raw SQL by definition.
- **§2.10 (no per-request mutable Rust state):** Bench metadata is constructed
  once before the benchmark loop; `black_box` prevents the optimizer from
  eliding the pure compile/hydrate call. `iter_batched` with
  `BatchSize::SmallInput` clones the input per iteration so the measurement
  captures fresh owned output, matching the runtime contract.
- **§2.7 (no feature without tests):** The benchmarks ARE the tests for
  performance characteristics; the regression-gate layer (`test_benchmarks.py`)
  is the durable CI contract.

## Findings

| # | Severity | Evidence | Required correction |
|---|---|---|---|
| A1 | NOTE (follow-up, non-blocking) | `pool_acquired_peak=0` for all single-connection workloads in `test_performance.py`. Pool saturation is measured but does not exercise concurrent acquisition. | Follow-up: add a dedicated concurrent pool-stress workload (not a W4-B blocker — the metric is captured; the workload that exercises it is a separate test). |
| A2 | NOTE (process, non-blocking) | `.bench-results/` is not in `.gitignore` (confirmed: `git check-ignore .bench-results/perf.json` → exit 1). The executor correctly did not modify the shared `.gitignore` (not leased). | Coordinator action: add `.bench-results/` to `.gitignore` under a shared-path lease. Not a W4-B code defect. |
| A3 | NOTE (follow-up, non-blocking) | `REGRESSION_FACTOR=3.0` is a placeholder until nightly CI establishes a stable baseline via `FERRUM_BENCH_CREATE_BASELINE=1`. | Follow-up: nightly CI should set `FERRUM_BENCH_RUN_RUST=1` and create a baseline; tighten the factor once variance is characterized. Documented in executor log risks. |

## Decision

**approved**

The benchmark architecture is sound and satisfies every acceptance criterion in
the task contract:

- Workload coverage spans all nine required classes (point reads, filtered pages,
  relation loads, writes, bulk, JSONB, vector KNN, streaming, transactions) in
  Python, plus 13 SQL-compile scenarios and 5 hydration scenarios in Rust.
- Measurement metrics (throughput, p50/p95/p99, CPU, memory peak, pool
  saturation) are captured for every workload.
- Compile/hydration split is architecturally clean: separate crates
  (`ferrum-sql`/`ferrum-core`), each bench exercises one pure function.
- JSON/msgpack boundary is profiled at the correct layer (Rust serde_json for
  the PyO3-boundary copy; Python `FERRUM_WIRE_FORMAT` toggle for msgpack).
- Regression reporting is soft; the meta-check programmatically enforces the
  no-flaky-hard-gate invariant. Only hard gates are deterministic (compile
  success, nonzero throughput, structural field presence).
- §2 constraints are honored: Rust benches are pure and synchronous; Python
  owns I/O; no raw SQL in the Ferrum path; no per-request mutable Rust state.

The three findings are non-blocking notes (follow-up workload, coordinator
gitignore action, baseline-tightening after variance characterization). None
require changes to the W4-B owned paths before the task transitions to
`verified`.

This record grants only the ChiefArchitect gate. It does not substitute for
the CodeReviewer gate or independent verification.
