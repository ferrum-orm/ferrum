# Command: Benchmark

Reusable prompt for measuring Ferrum performance with a defensible methodology.

## Use when

Validating a performance claim, guarding against regressions, or comparing approaches.

## Prompt

Benchmark this Ferrum path. Follow a measure-first methodology:

1. **Define the metric and workload.** State p50/p95 latency, throughput, or allocations, and the
   exact query/migration workload and concurrency level.
2. **Capture a baseline.** Record environment (machine, PostgreSQL version, build flags). Use
   `cProfile`/`py-spy` for Python and `cargo bench`/`criterion` for Rust. Profile the Python
   (async/pool/hook) and Rust (compile/hydrate) seams separately.
3. **Change one thing.** Apply a single change; re-measure against the baseline.
4. **Protect invariants.** Do not introduce per-request mutable state in Rust, release/reacquire
   the GIL for the compile, move I/O into Rust, or enable Tier C in shared environments to
   profile.
5. **Report honestly.** Before/after numbers, workload, environment, and variance. Note whether
   the change is worth its complexity (YAGNI).
6. **Guard.** Add a benchmark or regression check where feasible.

## Output

A benchmark report: metric, workload, environment, baseline vs result with variance, the single
change made, and a recommendation. Record results in the relevant plan/doc.
