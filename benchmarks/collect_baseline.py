"""Collect a reproducible Ferrum performance/correctness baseline.

Merges Rust criterion raw samples, Python pytest-benchmark JSON, and pytest
JUnit XML correctness summaries into one machine-readable report with
p50/p95/p99 latency percentiles and environment metadata. This module never
runs benchmarks or tests itself — the ``bench-rust``/``bench-python``/
``test-consumer-contracts``/``smoke-wheel`` mise tasks do that separately, so
build/setup time never leaks into the measured samples this script reports.

Standard library only: this stays importable and runnable without the
``dev`` extras or a built ``ferrum._native`` extension.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parent.parent


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default method)."""
    if not values:
        raise ValueError("percentile() requires at least one value")
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * (pct / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return data[int(rank)]
    lower_val = data[lower] * (upper - rank)
    upper_val = data[upper] * (rank - lower)
    return lower_val + upper_val


def _stats(values: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def summarize_pytest_benchmark_json(path: Path) -> dict[str, dict[str, Any]]:
    """Recompute p50/p95/p99 from raw per-round samples in a pytest-benchmark report.

    pytest-benchmark's own summary stops at mean/stddev/median; ``stats.data``
    holds every raw round time in seconds, which is what the percentiles below
    are drawn from directly rather than approximating from mean/stddev.
    """
    payload = json.loads(path.read_text())
    out: dict[str, dict[str, Any]] = {}
    for bench in payload.get("benchmarks", []):
        data = bench.get("stats", {}).get("data", [])
        if not data:
            continue
        entry = _stats(data)
        entry["unit"] = "seconds"
        out[bench["fullname"]] = entry
    return out


def _criterion_bench_name(sample_path: Path, root: Path) -> str:
    rel = sample_path.relative_to(root)
    # <group>/<function>/new/sample.json -> "<group>::<function>"
    parts = rel.parts[:-2]
    return "::".join(parts)


def summarize_criterion_dir(root: Path) -> dict[str, dict[str, Any]]:
    """Recompute p50/p95/p99 (nanoseconds/iteration) from criterion raw samples.

    Criterion's own report gives mean/median/slope confidence intervals, not
    percentiles. ``new/sample.json`` holds the most recent run's raw
    ``(iters, times)`` pairs, which this divides out into a per-iteration
    time series before computing percentiles.
    """
    out: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return out
    for sample_path in sorted(root.glob("**/new/sample.json")):
        payload = json.loads(sample_path.read_text())
        iters = payload.get("iters", [])
        times = payload.get("times", [])
        per_iter = [t / i for t, i in zip(times, iters, strict=True) if i]
        if not per_iter:
            continue
        entry = _stats(per_iter)
        entry["unit"] = "ns"
        out[_criterion_bench_name(sample_path, root)] = entry
    return out


def parse_junit(path: Path) -> dict[str, Any]:
    """Extract pass/fail/skip counts and duration from a pytest JUnit XML report."""
    root = ElementTree.parse(path).getroot()  # noqa: S314 -- local, executor-generated report
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise ValueError(f"{path}: no <testsuite> element found")
    return {
        "tests": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "time_seconds": float(suite.get("time", 0.0)),
    }


def _run(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
            cmd, capture_output=True, text=True, check=True, cwd=REPO_ROOT
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def environment_metadata() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "git_commit": _run(["git", "rev-parse", "HEAD"]),
        "cargo_version": _run(["cargo", "--version"]),
        "uv_version": _run(["uv", "--version"]),
    }


def _load_timing_json(paths: list[Path]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for p in paths:
        merged.update(json.loads(p.read_text()))
    return merged


def build_report(
    *,
    label: str | None,
    python_benchmark_jsons: list[Path],
    criterion_dir: Path,
    junit_paths: list[Path],
    timing_jsons: list[Path],
) -> dict[str, Any]:
    python_benchmarks: dict[str, Any] = {}
    for p in python_benchmark_jsons:
        python_benchmarks.update(summarize_pytest_benchmark_json(p))

    correctness: dict[str, Any] = {p.stem: parse_junit(p) for p in junit_paths}

    return {
        "label": label,
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": environment_metadata(),
        "correctness": correctness,
        "timings": _load_timing_json(timing_jsons),
        "benchmarks": {
            "python": python_benchmarks,
            "rust": summarize_criterion_dir(criterion_dir),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--label", default=None)
    parser.add_argument(
        "--python-benchmark-json", action="append", default=[], type=Path, dest="python_json"
    )
    parser.add_argument("--criterion-dir", default=REPO_ROOT / "target" / "criterion", type=Path)
    parser.add_argument("--junit", action="append", default=[], type=Path)
    parser.add_argument("--timing-json", action="append", default=[], type=Path, dest="timing")
    args = parser.parse_args(argv)

    report = build_report(
        label=args.label,
        python_benchmark_jsons=args.python_json,
        criterion_dir=args.criterion_dir,
        junit_paths=args.junit,
        timing_jsons=args.timing,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Wrote baseline report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
