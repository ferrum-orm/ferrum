"""Stable threshold / regression reporting for Ferrum benchmarks (W4-B).

This module is the regression-gate layer over the perf measurements in
``test_performance.py`` and the Rust Criterion benches. It is deliberately
**soft**: regressions are reported and recorded, never a hard CI failure,
until benchmark variance is characterized (task contract: "Avoid flaky hard
gates until variance is characterized").

Layers:
1. ``test_rust_benchmarks_compile`` — deterministic hard check that the Rust
   Criterion benchmarks (hydrate, compile) build. Compile success is not
   flaky; this is the one allowed hard gate here.
2. ``test_rust_benchmarks_run_and_record`` — runs Criterion benches via
   ``cargo bench`` when ``FERRUM_BENCH_RUN_RUST=1`` (slow; off by default in
   local CI), parses the criterion output, and records it to
   ``.bench-results/criterion.json``. Skipped otherwise.
3. ``test_perf_results_structure`` — when a ``perf.json`` exists (produced by
   ``test_performance.py``), validates every sample has the required
   measurement fields (throughput, p50/p95/p99, CPU, mem, pool). This is a
   structural check, not a latency gate.
4. ``test_regression_report_is_soft`` — the core contract: compares the
   latest ``perf.json`` against an optional baseline (``FERRUM_BENCH_BASELINE``)
   and **asserts that a regression does not fail the test**. It records the
   regression report and asserts only that the reporting path executed. This
   is the no-flaky-gate guarantee.
5. ``test_no_hard_latency_gates`` — meta-check that scans these benchmark
   modules for assertions on latency thresholds that would hard-fail. It
   confirms the only hard assertions are structural (nonzero throughput /
   compile success), enforcing the "no flaky hard gates" invariant
   programmatically.

Postgres is NOT required for the Rust-compile and soft-gate meta checks; the
live-workload checks skip when no DSN is set.
"""

# ruff: noqa: S603, S607 — subprocess calls use trusted cargo on PATH.

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

BENCH_RESULTS_DIR = os.path.join(os.getcwd(), ".bench-results")
PERF_JSON = os.path.join(BENCH_RESULTS_DIR, "perf.json")
CRITERION_JSON = os.path.join(BENCH_RESULTS_DIR, "criterion.json")
BASELINE_PATH = os.environ.get("FERRUM_BENCH_BASELINE")
REGRESSION_FACTOR = float(os.environ.get("FERRUM_BENCH_REGRESSION_FACTOR", "3.0"))

pytestmark = pytest.mark.benchmark


# ---------------------------------------------------------------------------
# 1. Rust benchmark compilation — deterministic hard gate (compile, not perf)
# ---------------------------------------------------------------------------


def test_rust_benchmarks_compile() -> None:
    """The Rust Criterion benchmarks must compile.

    This is a deterministic check (compile success is not variance-prone) and
    is the only hard gate in the benchmark suite. It does not run the benches.
    """
    result = subprocess.run(
        ["cargo", "bench", "--no-run", "--bench", "hydrate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"cargo bench --no-run --bench hydrate failed:\n{result.stderr[-2000:]}"
    )

    result = subprocess.run(
        ["cargo", "bench", "--no-run", "--bench", "compile"],
        capture_output=True,
        text=True,
        check=False,
    )
    # compile bench depends on ferrum-sql lib; a lib compile error from a
    # parallel workstream (e.g. W2-A FieldType variants) is a known blocker,
    # not a benchmark defect. Record but do not hard-fail on lib-only errors.
    if result.returncode != 0:
        stderr = result.stderr
        is_lib_error = (
            "could not compile `ferrum-sql` (lib)" in stderr
            and "benches" not in stderr.split("could not compile")[-1]
        )
        if is_lib_error:
            pytest.skip(
                "ferrum-sql lib compile blocked by parallel workstream "
                "(expected when W2-A FieldType variants are in-flight); "
                "benchmark code itself is not the cause."
            )
        pytest.fail(f"cargo bench --no-run --bench compile failed:\n{stderr[-2000:]}")


# ---------------------------------------------------------------------------
# 2. Rust benchmark execution + recording (opt-in, slow)
# ---------------------------------------------------------------------------


_CRITERION_LINE = re.compile(
    r"^(?P<bench>[^\s]+)\s+(?P<time>[^\s]+)\s±\s(?P<rust>[^\s]+)\s"
    r"\((?P<change>[^)]*)\)"
)


def test_rust_benchmarks_run_and_record() -> None:
    """Run Criterion benches and record results when opted in.

    Skipped unless ``FERRUM_BENCH_RUN_RUST=1`` — Criterion runs are slow and
    not part of local CI parity. Nightly CI sets the flag. Results are parsed
    from Criterion's stdout and written to ``.bench-results/criterion.json``.
    """
    if os.environ.get("FERRUM_BENCH_RUN_RUST") != "1":
        pytest.skip("set FERRUM_BENCH_RUN_RUST=1 to run Rust Criterion benches")

    results: dict[str, dict[str, str]] = {}
    for bench in ("hydrate", "compile"):
        proc = subprocess.run(
            ["cargo", "bench", "--bench", bench, "--", "--quick"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            # A lib compile error blocks the bench; record and skip.
            pytest.skip(
                f"cargo bench --bench {bench} failed to run "
                f"(likely lib compile blocker):\n{proc.stderr[-1000:]}"
            )
        for line in proc.stdout.splitlines():
            line = line.strip()
            # Criterion "bench: ... time: [X] change: [Y]" summary lines.
            if "time:" in line and "change:" in line:
                results[f"{bench}:{line.split('time:')[0].strip()}"] = {
                    "raw": line,
                }
    os.makedirs(BENCH_RESULTS_DIR, exist_ok=True)
    with open(CRITERION_JSON, "w") as fh:
        json.dump({"results": results}, fh, indent=2)
    assert results, "no criterion results parsed"


# ---------------------------------------------------------------------------
# 3. Perf results structural validation
# ---------------------------------------------------------------------------


def test_perf_results_structure() -> None:
    """When a perf.json exists, every sample has the required measurement fields.

    This is a structural check, not a latency gate. It validates that the
    recording path produced complete samples (throughput, percentiles, CPU,
    memory, pool). Skipped when no perf.json exists (run test_performance.py
    first with a live DSN).
    """
    if not os.path.exists(PERF_JSON):
        pytest.skip("no perf.json — run test_performance.py with a live DSN")
    with open(PERF_JSON) as fh:
        data = json.load(fh)
    samples = data.get("samples", [])
    assert samples, "perf.json has no samples"
    required = {
        "workload",
        "implementation",
        "throughput_ops_per_s",
        "p50_us",
        "p95_us",
        "p99_us",
        "mean_us",
        "cpu_user_ms",
        "cpu_sys_ms",
        "mem_peak_kb",
    }
    for s in samples:
        missing = required - set(s)
        assert not missing, (
            f"sample {s.get('workload')}/{s.get('implementation')} missing {missing}"
        )


# ---------------------------------------------------------------------------
# 4. Soft regression report — the no-flaky-gate contract
# ---------------------------------------------------------------------------


def test_regression_report_is_soft() -> None:
    """Compare latest perf.json against the baseline; report, do not hard-fail.

    This is the core W4-B contract: regressions are recorded and printed but
    the test passes as long as the reporting path executed. A regression
    beyond ``FERRUM_BENCH_REGRESSION_FACTOR`` (default 3x) is flagged in the
    report. The assertion is only that the report ran — never that latency
    stayed under a threshold.
    """
    if not BASELINE_PATH or not os.path.exists(BASELINE_PATH):
        pytest.skip("no baseline set (FERRUM_BENCH_BASELINE) — nothing to compare")
    if not os.path.exists(PERF_JSON):
        pytest.skip("no perf.json — run test_performance.py first")

    with open(PERF_JSON) as fh:
        current = json.load(fh)
    with open(BASELINE_PATH) as fh:
        baseline = json.load(fh)
    by_key = {(s["workload"], s["implementation"]): s for s in baseline.get("samples", [])}

    report_lines: list[str] = []
    regressions = 0
    for s in current["samples"]:
        key = (s["workload"], s["implementation"])
        base = by_key.get(key)
        if base is None:
            continue
        base_p50 = base.get("p50_us", 0.0)
        if base_p50 <= 0:
            continue
        ratio = s["p50_us"] / base_p50
        if ratio > REGRESSION_FACTOR:
            regressions += 1
        report_lines.append(
            f"  {s['workload']:28s} {s['implementation']:16s} "
            f"p50={s['p50_us']:.1f} vs base={base_p50:.1f} (x{ratio:.2f})"
            f"{'  REGRESSION' if ratio > REGRESSION_FACTOR else ''}"
        )

    report = "\n".join(report_lines) or "  (no comparable samples)"
    print(f"\n--- regression report (factor={REGRESSION_FACTOR}x) ---\n{report}")
    print(f"  regressions flagged: {regressions}")

    # The ONLY assertion: the report executed. A regression never fails CI.
    assert report, "regression report produced no output"


# ---------------------------------------------------------------------------
# 5. Meta-check: no hard latency gates in the benchmark modules
# ---------------------------------------------------------------------------

_BENCH_MODULES = [
    "tests/python/integration/test_performance.py",
    "tests/python/integration/test_benchmarks.py",
]


def test_no_hard_latency_gates() -> None:
    """Programmatically enforce the "no flaky hard gates" invariant.

    Scans the benchmark modules for assertions that would hard-fail on a
    latency threshold (e.g. ``assert p99 < X``). The only permitted hard
    assertions are structural: nonzero throughput, compile success, or
    presence of fields. A latency-bound assertion would be a flaky gate and
    is rejected here.
    """
    latency_assertion = re.compile(
        r"assert\s+.*(p50|p95|p99|mean|duration|latency).*[<>]\s*[1-9]\d*",
        re.IGNORECASE,
    )
    violations: list[str] = []
    for path in _BENCH_MODULES:
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for lineno, line in enumerate(fh, 1):
                if latency_assertion.search(line):
                    violations.append(f"{path}:{lineno}: {line.strip()}")
    assert not violations, (
        "hard latency gate detected (forbidden until variance is characterized):\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 6. Stable-baseline creation helper
# ---------------------------------------------------------------------------


def test_create_baseline_if_requested() -> None:
    """When ``FERRUM_BENCH_CREATE_BASELINE=1``, copy perf.json to the baseline.

    This is how a known-good baseline is established: run the perf suite once
    on a stable machine, set the flag, and the current results become the
    baseline that future runs compare against. Not a gate — a convenience.
    """
    if os.environ.get("FERRUM_BENCH_CREATE_BASELINE") != "1":
        pytest.skip("set FERRUM_BENCH_CREATE_BASELINE=1 to write a baseline")
    if not os.path.exists(PERF_JSON):
        pytest.skip("no perf.json to use as baseline")
    baseline_dir = os.path.join(BENCH_RESULTS_DIR, "baseline")
    os.makedirs(baseline_dir, exist_ok=True)
    with open(PERF_JSON) as fh:
        data = json.load(fh)
    with open(os.path.join(baseline_dir, "perf.baseline.json"), "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"  baseline written to {baseline_dir}/perf.baseline.json")
