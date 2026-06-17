#!/usr/bin/env python3
"""Compare criterion and pytest-benchmark results against encoded budgets.

Reads ``benches/budgets.json`` and fails when any metric exceeds its budget
multiplied by ``regression_factor`` (default 1.10 = 10% regression tolerance).

Rust: walks ``target/criterion/**/new/sample.json`` (produced by ``cargo bench``).
Python: reads pytest-benchmark JSON from ``--benchmark-json``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return math.nan
    idx = min(len(sorted_values) - 1, max(0, int(math.ceil(p / 100.0 * len(sorted_values)) - 1)))
    return sorted_values[idx]


def _load_budgets(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_criterion_samples(root: Path) -> dict[str, list[float]]:
    """Return map of criterion bench id -> sample times in nanoseconds."""
    results: dict[str, list[float]] = {}
    if not root.is_dir():
        return results

    for sample_path in root.rglob("new/sample.json"):
        try:
            data = json.loads(sample_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        times = data.get("times")
        iters = data.get("iters")
        if not isinstance(times, list):
            continue
        flat: list[float] = []
        if isinstance(iters, list) and len(iters) == len(times):
            for total_ns, iter_count in zip(times, iters, strict=True):
                count = float(iter_count)
                if count > 0:
                    flat.append(float(total_ns) / count)
        else:
            for row in times:
                if isinstance(row, list):
                    flat.extend(float(v) for v in row)
                else:
                    flat.append(float(row))
        if not flat:
            continue
        # Path: target/criterion/<group>/<function>/[<param>/]new/sample.json
        rel = sample_path.relative_to(root)
        parts = rel.parts
        if len(parts) >= 2 and parts[-2] == "new":
            bench_id = "/".join(parts[:-2])
        elif len(parts) >= 1:
            bench_id = parts[0]
        else:
            bench_id = sample_path.stem
        results[bench_id] = flat
    return results


def _check_rust(budgets: dict[str, Any], criterion_root: Path) -> list[str]:
    failures: list[str] = []
    factor = float(budgets.get("regression_factor", 1.1))
    samples_by_id = _collect_criterion_samples(criterion_root)
    rust_budgets: dict[str, Any] = budgets.get("rust", {})

    for name, spec in rust_budgets.items():
        criterion_id = spec.get("criterion_id")
        budget_ms = spec.get("p99_ms")
        if criterion_id is None or budget_ms is None:
            continue
        samples = samples_by_id.get(criterion_id)
        if samples is None:
            failures.append(f"rust/{name}: missing criterion samples for {criterion_id!r}")
            continue
        sorted_ns = sorted(samples)
        p50_ms = _percentile(sorted_ns, 50) / 1_000_000.0
        p95_ms = _percentile(sorted_ns, 95) / 1_000_000.0
        p99_ms = _percentile(sorted_ns, 99) / 1_000_000.0
        limit_ms = float(budget_ms) * factor
        print(
            f"rust/{name} ({criterion_id}): "
            f"p50={p50_ms:.4f}ms p95={p95_ms:.4f}ms p99={p99_ms:.4f}ms "
            f"(budget p99={budget_ms}ms, limit={limit_ms:.4f}ms)"
        )
        if p99_ms > limit_ms:
            failures.append(
                f"rust/{name}: p99 {p99_ms:.4f}ms exceeds limit {limit_ms:.4f}ms "
                f"(budget {budget_ms}ms × {factor})"
            )
    return failures


def _check_python(budgets: dict[str, Any], bench_json: Path) -> list[str]:
    failures: list[str] = []
    if not bench_json.is_file():
        print(f"python: no benchmark JSON at {bench_json}; skipping python threshold checks")
        return failures

    factor = float(budgets.get("regression_factor", 1.1))
    data = json.loads(bench_json.read_text(encoding="utf-8"))
    raw_benchmarks = data.get("benchmarks", [])
    if isinstance(raw_benchmarks, dict):
        benchmark_entries = list(raw_benchmarks.items())
    else:
        benchmark_entries = [
            (entry.get("name", ""), entry)
            for entry in raw_benchmarks
            if isinstance(entry, dict)
        ]
    py_budgets: dict[str, Any] = budgets.get("python", {})

    for name, spec in py_budgets.items():
        pytest_name = spec.get("pytest_name")
        budget_ms = spec.get("p99_ms")
        budget_us = spec.get("p99_us")
        if pytest_name is None:
            continue

        entry = None
        for key, val in benchmark_entries:
            if pytest_name in str(key):
                entry = val
                break
        if entry is None:
            print(f"python/{name}: skipped (benchmark {pytest_name!r} not in JSON)")
            continue

        stats = entry.get("stats", {})
        raw_data = stats.get("data")
        if isinstance(raw_data, list) and raw_data:
            sorted_s = sorted(float(v) for v in raw_data)
            p99_s = _percentile(sorted_s, 99)
            median_s = stats.get("median", sorted_s[len(sorted_s) // 2])
        else:
            median_s = stats.get("median", stats.get("mean", 0.0))
            q3 = stats.get("q3", median_s)
            iqr = stats.get("iqr", 0.0)
            p99_s = float(q3) + 1.5 * float(iqr)
            max_s = stats.get("max", p99_s)
            p99_s = min(float(max_s), max(p99_s, float(median_s)))

        if budget_us is not None:
            p99_us = p99_s * 1_000_000.0
            limit_us = float(budget_us) * factor
            print(
                f"python/{name} ({pytest_name}): "
                f"median={float(median_s) * 1_000_000:.1f}us p99={p99_us:.1f}us "
                f"(budget p99={budget_us}us, limit={limit_us:.1f}us)"
            )
            if p99_us > limit_us:
                failures.append(
                    f"python/{name}: estimated p99 {p99_us:.1f}us exceeds "
                    f"limit {limit_us:.1f}us (budget {budget_us}us × {factor})"
                )
        elif budget_ms is not None:
            p99_ms = p99_s * 1000.0
            limit_ms = float(budget_ms) * factor
            print(
                f"python/{name} ({pytest_name}): "
                f"median={float(median_s) * 1000:.3f}ms p99={p99_ms:.3f}ms "
                f"(budget p99={budget_ms}ms, limit={limit_ms:.3f}ms)"
            )
            if p99_ms > limit_ms:
                failures.append(
                    f"python/{name}: estimated p99 {p99_ms:.3f}ms exceeds "
                    f"limit {limit_ms:.3f}ms (budget {budget_ms}ms × {factor})"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Ferrum benchmark budgets.")
    parser.add_argument(
        "--budgets",
        type=Path,
        default=Path("benches/budgets.json"),
        help="Path to budgets JSON",
    )
    parser.add_argument(
        "--criterion-root",
        type=Path,
        default=Path("target/criterion"),
        help="Root of criterion output (sample.json files)",
    )
    parser.add_argument(
        "--python-json",
        type=Path,
        default=Path("bench-python.json"),
        help="pytest-benchmark JSON output",
    )
    args = parser.parse_args()

    budgets = _load_budgets(args.budgets)
    failures = _check_rust(budgets, args.criterion_root)
    failures.extend(_check_python(budgets, args.python_json))

    if failures:
        print("\nBenchmark regressions detected:", file=sys.stderr)
        for msg in failures:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print("\nAll benchmark thresholds satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
