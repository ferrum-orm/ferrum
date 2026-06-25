"""Observability bridge: hooks → metrics and optional OpenTelemetry.

All exported telemetry derives from Tier-A hook fields only (ADR-006). Bound
values, DSNs, and row data never enter spans or metrics.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ferrum.errors import FerrumConfigError
from ferrum.hooks import HookPayload, register_hook, unregister_hook

# In-process counters/histograms for tests and lightweight instrumentation.
_METRICS: dict[str, float] = defaultdict(float)
_METRICS_HOOKS: list[Any] = []
_OTEL_ENABLED = False


def _metric_key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"


def record_metric(name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
    """Record a Tier-A-safe metric sample (no bound values or secrets)."""
    key = _metric_key(name, labels or {})
    _METRICS[key] += value


def get_metrics() -> dict[str, float]:
    """Return a snapshot of in-process metric counters (test/dev helper)."""
    return dict(_METRICS)


def reset_metrics() -> None:
    """Clear in-process metrics (test teardown only)."""
    _METRICS.clear()


def _on_query_success(payload: HookPayload) -> None:
    labels = {
        "operation": str(payload.get("operation", "unknown")),
        "status": "ok",
    }
    fingerprint = payload.get("fingerprint")
    if fingerprint:
        labels["fingerprint"] = str(fingerprint)
    record_metric("ferrum.query.count", 1.0, labels=labels)
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        record_metric("ferrum.query.duration_ms", float(duration), labels=labels)


def _on_query_failure(payload: HookPayload) -> None:
    labels = {
        "operation": str(payload.get("operation", "unknown")),
        "status": "error",
        "failure_category": str(payload.get("failure_category", "unknown")),
    }
    record_metric("ferrum.query.errors", 1.0, labels=labels)
    record_metric("ferrum.query.count", 1.0, labels=labels)


def _on_pool_event(payload: HookPayload) -> None:
    event = str(payload.get("event", ""))
    if event == "pool_acquired":
        record_metric("ferrum.pool.acquired", 1.0)
    elif event == "pool_released":
        record_metric("ferrum.pool.released", 1.0)


def enable_metrics() -> None:
    """Register Tier-A metrics hooks for query and pool events."""
    for fn in (_on_query_success, _on_query_failure, _on_pool_event):
        register_hook("*", fn)
        _METRICS_HOOKS.append(fn)


def disable_metrics() -> None:
    """Unregister metrics hooks (test teardown)."""
    for fn in _METRICS_HOOKS:
        unregister_hook(fn)
    _METRICS_HOOKS.clear()


def enable_opentelemetry(
    *,
    tracer_provider: Any = None,  # noqa: ANN401
    meter_provider: Any = None,  # noqa: ANN401
) -> None:
    """Bridge Ferrum hooks to OpenTelemetry using Tier-A fields only.

    Requires ``opentelemetry-api`` (``ferrum-orm[otel]`` extra). Providers are
    optional; when omitted, the global OTel providers are used.
    """
    global _OTEL_ENABLED
    try:
        from opentelemetry import metrics, trace  # type: ignore[import-untyped]
    except ImportError as exc:
        raise FerrumConfigError(
            "OpenTelemetry is not installed. Install with: uv add 'ferrum-orm[otel]' [FERR-C001]"
        ) from exc

    if tracer_provider is not None:
        trace.set_tracer_provider(tracer_provider)
    if meter_provider is not None:
        metrics.set_meter_provider(meter_provider)

    tracer = trace.get_tracer("ferrum")
    meter = metrics.get_meter("ferrum")
    query_counter = meter.create_counter("ferrum.query.count")
    error_counter = meter.create_counter("ferrum.query.errors")
    duration_hist = meter.create_histogram("ferrum.query.duration_ms")

    def _otel_hook(payload: HookPayload) -> None:
        event = str(payload.get("event", ""))
        if event not in ("query_success", "query_failure", "query_start"):
            return
        attrs = {
            k: str(v)
            for k, v in payload.items()
            if k
            in (
                "event",
                "model",
                "table",
                "operation",
                "fingerprint",
                "status",
                "failure_category",
                "rows_affected",
            )
            and v is not None
        }
        span = tracer.start_span(f"ferrum.{event}", attributes=attrs)
        try:
            if event == "query_success":
                query_counter.add(1, attributes=attrs)
                duration = payload.get("duration_ms")
                if isinstance(duration, (int, float)):
                    duration_hist.record(float(duration), attributes=attrs)
            elif event == "query_failure":
                error_counter.add(1, attributes=attrs)
                query_counter.add(1, attributes=attrs)
        finally:
            span.end()

    register_hook("*", _otel_hook)
    _METRICS_HOOKS.append(_otel_hook)
    _OTEL_ENABLED = True


def opentelemetry_enabled() -> bool:
    """Return whether ``enable_opentelemetry()`` has been called."""
    return _OTEL_ENABLED
