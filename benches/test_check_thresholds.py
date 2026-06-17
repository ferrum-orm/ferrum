"""Unit tests for benches/check_thresholds.py budget enforcement."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BENCHES_DIR = Path(__file__).resolve().parent
CHECK_THRESHOLDS = BENCHES_DIR / "check_thresholds.py"


def _write_criterion_sample(
    root: Path,
    criterion_id: str,
    per_iter_ns: float,
    *,
    sample_count: int = 100,
) -> None:
    sample_path = root / criterion_id / "new" / "sample.json"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "iters": [1] * sample_count,
        "times": [[per_iter_ns] * sample_count],
    }
    sample_path.write_text(json.dumps(payload), encoding="utf-8")


def _write_python_benchmark_json(path: Path, name: str, samples_s: list[float]) -> None:
    path.write_text(
        json.dumps(
            {
                "benchmarks": [
                    {
                        "name": name,
                        "stats": {
                            "data": samples_s,
                            "median": sorted(samples_s)[len(samples_s) // 2],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_budgets(path: Path, *, rust_p99_ms: float = 1.0, python_p99_us: float = 500.0) -> None:
    path.write_text(
        json.dumps(
            {
                "regression_factor": 1.1,
                "rust": {
                    "compile_query": {
                        "p99_ms": rust_p99_ms,
                        "criterion_id": "compile_query/select_filtered/representative",
                    }
                },
                "python": {
                    "hook_dispatch": {
                        "p99_us": python_p99_us,
                        "pytest_name": "test_hook_dispatch_overhead",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_check_thresholds_passes_when_rust_and_python_within_budget(tmp_path: Path) -> None:
    budgets = tmp_path / "budgets.json"
    criterion_root = tmp_path / "criterion"
    python_json = tmp_path / "bench-python.json"

    _write_budgets(budgets)
    _write_criterion_sample(
        criterion_root,
        "compile_query/select_filtered/representative",
        per_iter_ns=500_000.0,
    )
    _write_python_benchmark_json(
        python_json,
        "test_hook_dispatch_overhead",
        [0.0001] * 100,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(CHECK_THRESHOLDS),
            "--budgets",
            str(budgets),
            "--criterion-root",
            str(criterion_root),
            "--python-json",
            str(python_json),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "All benchmark thresholds satisfied." in result.stdout


def test_check_thresholds_fails_when_rust_p99_exceeds_budget(tmp_path: Path) -> None:
    budgets = tmp_path / "budgets.json"
    criterion_root = tmp_path / "criterion"
    python_json = tmp_path / "bench-python.json"

    _write_budgets(budgets, rust_p99_ms=1.0)
    _write_criterion_sample(
        criterion_root,
        "compile_query/select_filtered/representative",
        per_iter_ns=2_000_000.0,
    )
    _write_python_benchmark_json(
        python_json,
        "test_hook_dispatch_overhead",
        [0.0001] * 100,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(CHECK_THRESHOLDS),
            "--budgets",
            str(budgets),
            "--criterion-root",
            str(criterion_root),
            "--python-json",
            str(python_json),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "rust/compile_query" in result.stderr
    assert "exceeds limit" in result.stderr


def test_check_thresholds_fails_when_criterion_samples_missing(tmp_path: Path) -> None:
    budgets = tmp_path / "budgets.json"
    criterion_root = tmp_path / "criterion"
    python_json = tmp_path / "bench-python.json"

    _write_budgets(budgets)
    criterion_root.mkdir()
    _write_python_benchmark_json(
        python_json,
        "test_hook_dispatch_overhead",
        [0.0001] * 100,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(CHECK_THRESHOLDS),
            "--budgets",
            str(budgets),
            "--criterion-root",
            str(criterion_root),
            "--python-json",
            str(python_json),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "missing criterion samples" in result.stderr


@pytest.mark.parametrize(
    ("per_iter_ns", "budget_ms", "should_fail"),
    [
        (900_000.0, 1.0, False),
        (1_200_000.0, 1.0, True),
    ],
)
def test_rust_regression_factor_applied(
    tmp_path: Path,
    per_iter_ns: float,
    budget_ms: float,
    should_fail: bool,
) -> None:
    budgets = tmp_path / "budgets.json"
    criterion_root = tmp_path / "criterion"
    python_json = tmp_path / "bench-python.json"

    _write_budgets(budgets, rust_p99_ms=budget_ms)
    _write_criterion_sample(
        criterion_root,
        "compile_query/select_filtered/representative",
        per_iter_ns=per_iter_ns,
    )
    python_json.write_text(json.dumps({"benchmarks": []}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(CHECK_THRESHOLDS),
            "--budgets",
            str(budgets),
            "--criterion-root",
            str(criterion_root),
            "--python-json",
            str(python_json),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    if should_fail:
        assert result.returncode == 1
    else:
        assert result.returncode == 0
