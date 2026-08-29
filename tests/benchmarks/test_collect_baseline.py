"""Unit tests for the baseline-collection harness (benchmarks/collect_baseline.py).

Pure-function tests only: no live PostgreSQL, no built ``ferrum._native``
extension, no network. These validate the percentile math and report parsers
against small synthetic fixtures, independent of any real benchmark run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.collect_baseline import (
    build_report,
    parse_junit,
    percentile,
    summarize_criterion_dir,
    summarize_pytest_benchmark_json,
)

# ---------------------------------------------------------------------------
# percentile()
# ---------------------------------------------------------------------------


def test_percentile_p50_odd_length() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0


def test_percentile_p0_and_p100_are_min_and_max() -> None:
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 5.0


def test_percentile_interpolates_between_samples() -> None:
    # rank = (4 - 1) * 0.95 = 2.85 -> interpolate between index 2 and 3.
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 95) == pytest.approx(3.85)


def test_percentile_single_value() -> None:
    assert percentile([42.0], 99) == 42.0


def test_percentile_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one value"):
        percentile([], 50)


# ---------------------------------------------------------------------------
# summarize_pytest_benchmark_json()
# ---------------------------------------------------------------------------


def test_summarize_pytest_benchmark_json(tmp_path: Path) -> None:
    payload = {
        "benchmarks": [
            {
                "fullname": "tests/python/benchmark/test_x.py::test_one",
                "stats": {"data": [1.0, 2.0, 3.0, 4.0, 5.0]},
            },
            {
                # No samples recorded (e.g. benchmark() never invoked) -> skipped.
                "fullname": "tests/python/benchmark/test_x.py::test_skipped",
                "stats": {"data": []},
            },
        ]
    }
    path = tmp_path / "bench.json"
    path.write_text(json.dumps(payload))

    result = summarize_pytest_benchmark_json(path)

    assert set(result) == {"tests/python/benchmark/test_x.py::test_one"}
    entry = result["tests/python/benchmark/test_x.py::test_one"]
    assert entry["samples"] == 5
    assert entry["min"] == 1.0
    assert entry["max"] == 5.0
    assert entry["p50"] == 3.0
    assert entry["unit"] == "seconds"


# ---------------------------------------------------------------------------
# summarize_criterion_dir()
# ---------------------------------------------------------------------------


def test_summarize_criterion_dir(tmp_path: Path) -> None:
    root = tmp_path / "criterion"
    sample_dir = root / "compile" / "select_25" / "new"
    sample_dir.mkdir(parents=True)
    # 5 samples of 1 iteration each, times in ns -> per-iter == times.
    (sample_dir / "sample.json").write_text(
        json.dumps({"iters": [1, 1, 1, 1, 1], "times": [10.0, 20.0, 30.0, 40.0, 50.0]})
    )

    result = summarize_criterion_dir(root)

    assert set(result) == {"compile::select_25"}
    entry = result["compile::select_25"]
    assert entry["samples"] == 5
    assert entry["p50"] == 30.0
    assert entry["unit"] == "ns"


def test_summarize_criterion_dir_missing_root_returns_empty(tmp_path: Path) -> None:
    assert summarize_criterion_dir(tmp_path / "does-not-exist") == {}


# ---------------------------------------------------------------------------
# parse_junit()
# ---------------------------------------------------------------------------


def test_parse_junit_top_level_testsuite(tmp_path: Path) -> None:
    path = tmp_path / "report.xml"
    path.write_text(
        '<testsuite name="pytest" tests="10" failures="1" errors="0" '
        'skipped="2" time="3.5"></testsuite>'
    )

    result = parse_junit(path)

    assert result == {"tests": 10, "failures": 1, "errors": 0, "skipped": 2, "time_seconds": 3.5}


def test_parse_junit_wrapped_in_testsuites(tmp_path: Path) -> None:
    path = tmp_path / "report.xml"
    path.write_text(
        "<testsuites>"
        '<testsuite name="pytest" tests="4" failures="0" errors="0" skipped="0" time="0.2">'
        "</testsuite>"
        "</testsuites>"
    )

    result = parse_junit(path)

    assert result["tests"] == 4


# ---------------------------------------------------------------------------
# build_report() — end-to-end assembly
# ---------------------------------------------------------------------------


def test_build_report_merges_all_sections(tmp_path: Path) -> None:
    bench_json = tmp_path / "bench-python.json"
    bench_json.write_text(
        json.dumps(
            {
                "benchmarks": [
                    {"fullname": "test_a", "stats": {"data": [0.001, 0.002, 0.003]}},
                ]
            }
        )
    )

    junit = tmp_path / "consumer-contracts.xml"
    junit.write_text(
        '<testsuite name="pytest" tests="3" failures="0" errors="0" skipped="0" time="1.0">'
        "</testsuite>"
    )

    timing_json = tmp_path / "smoke-wheel-timing.json"
    timing_json.write_text(json.dumps({"smoke_wheel_build_seconds": 4.2}))

    report = build_report(
        label="test-run",
        python_benchmark_jsons=[bench_json],
        criterion_dir=tmp_path / "no-criterion-here",
        junit_paths=[junit],
        timing_jsons=[timing_json],
    )

    assert report["label"] == "test-run"
    assert "environment" in report
    assert report["correctness"]["consumer-contracts"]["tests"] == 3
    assert report["timings"]["smoke_wheel_build_seconds"] == 4.2
    assert "test_a" in report["benchmarks"]["python"]
    assert report["benchmarks"]["rust"] == {}
