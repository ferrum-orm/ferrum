# Skill: Performance Profiling

> Expert behavior for measuring and protecting Ferrum's performance characteristics. Use when
> profiling the query path, validating the Rust-core speedup, or investigating regressions.

## When to use

- Profiling QuerySet → IR → compile → execute → hydrate latency.
- Validating that Rust compilation stays sub-millisecond and on the GIL-holding fast path.
- Investigating throughput, pool contention, or hydration cost regressions.

## Expert behaviors

- **Measure before optimizing.** Establish a baseline; never optimize on intuition. Cite numbers.
- **Profile the right layer.** Python-side async/pool/hook overhead and Rust-side compile/hydrate
  cost are distinct seams — profile them separately before attributing cost.
- **Protect the invariants.** Performance work must not introduce per-request mutable state in
  Rust, release/reacquire the GIL for the compile, or move I/O into Rust.
- **Hydration is hot.** Validate the construct-without-revalidate fast path (ADR-003) stays the
  default and measure its cost vs full validation when proposing changes.
- **Observability-first.** Use Tier A fingerprints/durations to find slow query shapes; never
  enable Tier C in shared/production environments to profile.

## Workflow

1. Define the metric (p50/p95 latency, throughput, allocations) and the workload.
2. Capture a baseline; use `cProfile`/`py-spy` (Python) and `cargo bench`/`criterion` (Rust).
3. Identify the dominant cost; change one thing; re-measure against baseline.
4. Record results (before/after, workload, environment) in the relevant plan/doc.
5. Add a benchmark/regression guard where feasible.

## Concurrency & failure modes (call these out)

- Pool exhaustion and queueing latency under concurrency.
- Event-loop stalls from accidental blocking work.
- Unbounded row buffering on streaming paths.

## Anti-patterns

- Optimizing without a baseline or a representative workload.
- Trading a security/architecture invariant for speed.
- Reporting micro-benchmarks without environment/workload context.
