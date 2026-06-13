# Ferrum Benchmarks

Performance benchmarks for the Ferrum ORM core.

## Rust (criterion)

Benchmarks live in `crates/ferrum-core/benches/` and `crates/ferrum-sql/benches/`
(added when the compile + hydrate paths land).

Run with:

```bash
cargo bench --workspace
```

## Python (pytest-benchmark)

Python benchmarks live in `tests/python/` tagged with `@pytest.mark.benchmark`.

Run with:

```bash
pytest tests/python/ -m benchmark --benchmark-json=bench.json
```

## Performance budgets (ARCHITECTURE.md §14)

| Operation | Budget (p99) |
|-----------|-------------|
| `compile_query` (Rust) | < 1 ms |
| `hydrate_rows` (Rust, 100 rows) | < 5 ms |

Benchmark regressions > 10% above budget open an issue in CI (nightly workflow).
