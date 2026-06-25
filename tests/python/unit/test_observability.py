"""Unit tests for observability metrics and OTel bridge."""

from __future__ import annotations

import pytest

from ferrum.hooks import clear_hooks, dispatch
from ferrum.observability import disable_metrics, enable_metrics, get_metrics, reset_metrics


@pytest.fixture(autouse=True)
def _clean_observability() -> None:
    clear_hooks()
    reset_metrics()
    disable_metrics()
    yield
    clear_hooks()
    reset_metrics()
    disable_metrics()


def test_enable_metrics_records_query_success() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "query_success",
            "fingerprint": "select:User",
            "operation": "select",
            "duration_ms": 12.5,
            "status": "ok",
            "rows_affected": 3,
        }
    )
    metrics = get_metrics()
    assert any(k.startswith("ferrum.query.count") for k in metrics)
    assert any(k.startswith("ferrum.query.duration_ms") for k in metrics)


def test_metrics_never_receive_bound_params() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "query_success",
            "fingerprint": "select:User",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
            "bound_params": ["secret-value"],
            "sql_text": "SELECT * FROM users WHERE email = $1",
        }
    )
    metrics = get_metrics()
    serialized = str(metrics)
    assert "secret-value" not in serialized
    assert "SELECT" not in serialized


def test_enable_opentelemetry_requires_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    from ferrum.errors import FerrumConfigError
    from ferrum.observability import enable_opentelemetry

    monkeypatch.setitem(__import__("sys").modules, "opentelemetry", None)
    with pytest.raises(FerrumConfigError, match="otel"):
        enable_opentelemetry()
