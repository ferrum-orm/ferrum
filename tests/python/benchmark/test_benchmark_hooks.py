"""Benchmark: Tier A hook dispatch overhead (no database).

Protects LOG-1 dispatch path latency — hooks run synchronously on the query path.
"""

from __future__ import annotations

import pytest

from ferrum.hooks import clear_hooks, dispatch, register_hook


@pytest.fixture(autouse=True)
def _clean_hooks() -> None:
    clear_hooks()
    yield
    clear_hooks()


@pytest.mark.benchmark
def test_hook_dispatch_overhead(benchmark: pytest.BenchmarkFixture) -> None:
    """Measure synchronous hook dispatch with Tier A redaction."""

    def noop(_: dict[str, object]) -> None:
        return None

    register_hook("query_success", noop)
    register_hook("*", noop)

    payload = {
        "event": "query_success",
        "model": "User",
        "table": "users",
        "operation": "select",
        "fingerprint": "select:User",
        "duration_ms": 1.25,
        "status": "ok",
        # Must be stripped by redaction — included to exercise the filter path.
        "bound_params": [{"type": "text", "value": "secret"}],
        "sql_text": "SELECT 1",
    }

    def run() -> None:
        dispatch(payload)

    benchmark(run)


@pytest.mark.benchmark
def test_hook_dispatch_many_listeners(benchmark: pytest.BenchmarkFixture) -> None:
    """Dispatch with ten registered listeners — worst-case hook fan-out."""

    def make_hook() -> None:
        return None

    for i in range(10):
        register_hook("query_start", lambda _p, _i=i: make_hook())

    payload = {
        "event": "query_start",
        "model": "Post",
        "table": "posts",
        "operation": "select",
        "fingerprint": "select:Post",
        "status": "ok",
    }

    benchmark(lambda: dispatch(payload))
