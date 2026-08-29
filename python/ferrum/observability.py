"""Observability bridge: hooks → metrics and optional OpenTelemetry.

All exported telemetry derives from Tier-A hook fields only (ADR-006, §3).
Bound values, DSNs, credentials, and row data never enter spans or metrics.

Prometheus example (in-process counters + text exposition)::

    from ferrum.observability import enable_metrics, render_prometheus

    enable_metrics()
    # ... run queries ...
    print(render_prometheus())  # expose via /metrics endpoint

OpenTelemetry example (one span per query, ambient parent context)::

    from opentelemetry.sdk.trace import TracerProvider
    from ferrum.observability import enable_opentelemetry

    enable_opentelemetry(tracer_provider=TracerProvider())
    # Spans are children of the ambient parent (e.g. FastAPI middleware span).
    # Each span covers exactly one query execution (start → success/failure),
    # NOT a zero-duration event span per dispatch.
"""

from __future__ import annotations

import contextvars
from collections import defaultdict
from typing import Any

from ferrum.errors import FerrumConfigError
from ferrum.hooks import HookPayload, register_hook, unregister_hook

# ---------------------------------------------------------------------------
# In-process metric registry (test/dev helper; Prometheus export reads this).
# ---------------------------------------------------------------------------

_METRICS: dict[str, float] = defaultdict(float)
_METRIC_LABELS: dict[str, set[tuple[tuple[str, str], ...]]] = defaultdict(set)
_METRICS_HOOKS: list[Any] = []
_OTEL_ENABLED = False
_OTEL_HOOKS: list[Any] = []
_EXEMPLARS_ENABLED = False

# Contextvar holding the active OTel span for the current task's query.
# query_start sets it; query_success/query_failure ends and clears it.
# Different asyncio tasks have independent contextvar values, so concurrent
# queries in different requests do not collide. Within one task, queryset
# dispatches query_start → await driver.fetch → query_success sequentially,
# so the span is naturally scoped to one execution.
_active_query_span: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "ferrum_active_query_span", default=None
)


def _metric_key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"


def record_metric(name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
    """Record a Tier-A-safe metric sample (no bound values or secrets)."""
    label_items = tuple(sorted((labels or {}).items()))
    key = _metric_key(name, labels or {})
    _METRICS[key] += value
    if label_items:
        _METRIC_LABELS[name].add(label_items)


def set_gauge(name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
    """Set a gauge metric value (overwrites; does not accumulate like a counter)."""
    label_items = tuple(sorted((labels or {}).items()))
    key = _metric_key(name, labels or {})
    _METRICS[key] = value
    if label_items:
        _METRIC_LABELS[name].add(label_items)


def get_metrics() -> dict[str, float]:
    """Return a snapshot of in-process metric counters (test/dev helper)."""
    return dict(_METRICS)


def get_metric_label_sets() -> dict[str, set[tuple[tuple[str, str], ...]]]:
    """Return the distinct label-sets observed per metric name (cardinality test)."""
    return {k: set(v) for k, v in _METRIC_LABELS.items()}


def reset_metrics() -> None:
    """Clear in-process metrics (test teardown only)."""
    _METRICS.clear()
    _METRIC_LABELS.clear()


def enable_exemplars() -> None:
    """Opt in to fingerprint metric labels / exemplar attributes.

    Default OFF: query fingerprints are high-cardinality and excluded from
    default metric labels to prevent cardinality explosions (§3, criterion 3).
    Call this only when you have a downstream sampler / exemplar reservoir
    (e.g. OTel exemplars, Prometheus exemplars). Never enable in unbounded
    multi-tenant deployments.
    """
    global _EXEMPLARS_ENABLED
    _EXEMPLARS_ENABLED = True


def disable_exemplars() -> None:
    """Disable fingerprint metric labels / exemplar attributes (back to default)."""
    global _EXEMPLARS_ENABLED
    _EXEMPLARS_ENABLED = False


def exemplars_enabled() -> bool:
    """Return whether exemplars / fingerprint labels are enabled."""
    return _EXEMPLARS_ENABLED


def _safe_labels(payload: HookPayload, *, include_fingerprint: bool = False) -> dict[str, str]:
    """Build low-cardinality metric labels from Tier-A-safe payload fields.

    Never includes ``fingerprint`` unless ``include_fingerprint`` is True (opt-in
    via :func:`enable_exemplars`). Never includes bound values, DSNs, row data,
    or any key not in the Tier-A allowlist.
    """
    labels: dict[str, str] = {}
    for key in ("operation", "status", "category", "failure_category", "isolation", "direction"):
        val = payload.get(key)
        if val is not None:
            labels[key] = str(val)
    if include_fingerprint:
        fp = payload.get("fingerprint")
        if fp is not None:
            labels["fingerprint"] = str(fp)
    return labels


# ---------------------------------------------------------------------------
# Query metrics (Tier-A-safe labels only; fingerprint opt-in via exemplars).
# ---------------------------------------------------------------------------


def _on_query_start(payload: HookPayload) -> None:
    # No metric on start; the OTel bridge uses this to open the span.
    return None


def _on_query_success(payload: HookPayload) -> None:
    labels = _safe_labels(payload, include_fingerprint=_EXEMPLARS_ENABLED)
    labels.setdefault("status", "ok")
    record_metric("ferrum.query.count", 1.0, labels=labels)
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        record_metric("ferrum.query.duration_ms.sum", float(duration), labels=labels)
        record_metric("ferrum.query.duration_ms.count", 1.0, labels=labels)


def _on_query_failure(payload: HookPayload) -> None:
    labels = _safe_labels(payload, include_fingerprint=_EXEMPLARS_ENABLED)
    labels.setdefault("status", "error")
    record_metric("ferrum.query.errors", 1.0, labels=labels)
    record_metric("ferrum.query.count", 1.0, labels=labels)
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        record_metric("ferrum.query.duration_ms.sum", float(duration), labels=labels)
        record_metric("ferrum.query.duration_ms.count", 1.0, labels=labels)


def _on_hydration_failure(payload: HookPayload) -> None:
    labels = _safe_labels(payload, include_fingerprint=_EXEMPLARS_ENABLED)
    labels.setdefault("status", "error")
    record_metric("ferrum.hydration.errors", 1.0, labels=labels)


# ---------------------------------------------------------------------------
# Pool / transaction / migration / retry / timeout metric handlers.
#
# These handlers fire when the corresponding hook helpers in hooks.py are
# dispatched. Dispatch sites in connection.py / runtime.py / migrations are
# owned by other workstreams (W1-E follow-up, W1-C). The handlers are ready
# and emit low-cardinality metrics from Tier-A-safe fields only.
# ---------------------------------------------------------------------------


def _on_pool_event(payload: HookPayload) -> None:
    event = str(payload.get("event", ""))
    labels: dict[str, str] = {}
    # Pool stats are integer snapshots; safe as metric values, not labels
    # (label cardinality is bounded by event type, not by pool size).
    metric_map = {
        "pool_acquire": "ferrum.pool.acquired",
        "pool_release": "ferrum.pool.released",
        "pool_wait": "ferrum.pool.wait",
        "pool_timeout": "ferrum.pool.timeout",
        "pool_shutdown": "ferrum.pool.shutdown",
    }
    metric_name = metric_map.get(event)
    if metric_name is None:
        return
    record_metric(metric_name, 1.0, labels=labels)
    # Gauges for pool snapshot fields (size/idle/acquired/waiters).
    for field, metric in (
        ("pool_size", "ferrum.pool.size"),
        ("pool_idle", "ferrum.pool.idle"),
        ("pool_acquired_count", "ferrum.pool.acquired_gauge"),
        ("pool_waiters", "ferrum.pool.waiters"),
    ):
        val = payload.get(field)
        if isinstance(val, int) and val >= 0:
            set_gauge(metric, float(val), labels={})


def _on_transaction_start(payload: HookPayload) -> None:
    labels = _safe_labels(payload)
    labels.setdefault("status", "ok")
    record_metric("ferrum.transaction.count", 1.0, labels=labels)


def _on_transaction_end(payload: HookPayload) -> None:
    labels = _safe_labels(payload)
    record_metric("ferrum.transaction.count", 1.0, labels=labels)
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        record_metric("ferrum.transaction.duration_ms.sum", float(duration), labels=labels)
        record_metric("ferrum.transaction.duration_ms.count", 1.0, labels=labels)


def _on_migration_event(payload: HookPayload) -> None:
    labels = _safe_labels(payload)
    record_metric("ferrum.migration.count", 1.0, labels=labels)
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        record_metric("ferrum.migration.duration_ms.sum", float(duration), labels=labels)
        record_metric("ferrum.migration.duration_ms.count", 1.0, labels=labels)


def _on_retry_attempt(payload: HookPayload) -> None:
    labels = _safe_labels(payload)
    record_metric("ferrum.retry.count", 1.0, labels=labels)


def _on_timeout_event(payload: HookPayload) -> None:
    labels = _safe_labels(payload)
    record_metric("ferrum.timeout.count", 1.0, labels=labels)
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        record_metric("ferrum.timeout.duration_ms.sum", float(duration), labels=labels)


# ---------------------------------------------------------------------------
# enable_metrics / disable_metrics — register all Tier-A metric handlers.
# ---------------------------------------------------------------------------


def enable_metrics() -> None:
    """Register Tier-A metrics hooks for query, pool, transaction, migration,
    retry, timeout, and hydration events.

    All metric labels are low-cardinality (enums, closed-enum categories,
    operation names). Query fingerprints are NOT in default labels — opt in
    via :func:`enable_exemplars` when a downstream sampler is configured.
    """
    event_bindings = (
        ("query_start", _on_query_start),
        ("query_success", _on_query_success),
        ("query_failure", _on_query_failure),
        ("hydration_failure", _on_hydration_failure),
        ("*", _on_pool_event),
        ("transaction_start", _on_transaction_start),
        ("transaction_end", _on_transaction_end),
        ("migration_start", _on_migration_event),
        ("migration_end", _on_migration_event),
        ("retry_attempt", _on_retry_attempt),
        ("timeout_event", _on_timeout_event),
    )
    for event, fn in event_bindings:
        register_hook(event, fn)
        _METRICS_HOOKS.append(fn)


def disable_metrics() -> None:
    """Unregister metrics hooks (test teardown)."""
    for fn in _METRICS_HOOKS:
        unregister_hook(fn)
    _METRICS_HOOKS.clear()


# ---------------------------------------------------------------------------
# OpenTelemetry bridge — ONE span per query execution (ambient parent context).
# ---------------------------------------------------------------------------


def enable_opentelemetry(
    *,
    tracer_provider: Any = None,  # noqa: ANN401
    meter_provider: Any = None,  # noqa: ANN401
) -> None:
    """Bridge Ferrum hooks to OpenTelemetry using Tier-A fields only.

    Creates ONE span per query execution (start → success/failure), NOT a
    zero-duration event span per dispatch. The span is a child of the ambient
    parent context (e.g. a FastAPI request span set by middleware) —
    ``tracer.start_span`` uses the current context by default.

    Requires ``opentelemetry-api`` (``ferrum-orm[otel]`` extra). Providers are
    optional; when omitted, the global OTel providers are used.

    Security: span attributes are limited to Tier-A-safe fields (operation,
    model, table, status, category, failure_category, rows_affected, duration).
    Bound values, DSNs, credentials, and row data NEVER appear in span
    attributes (LOG-1, §3). Query fingerprints are included only when
    :func:`enable_exemplars` has been called.
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
    tx_counter = meter.create_counter("ferrum.transaction.count")
    pool_counter = meter.create_counter("ferrum.pool.events")
    migration_counter = meter.create_counter("ferrum.migration.count")
    retry_counter = meter.create_counter("ferrum.retry.count")
    timeout_counter = meter.create_counter("ferrum.timeout.count")

    span_attr_keys = frozenset(
        {
            "event",
            "model",
            "table",
            "operation",
            "status",
            "failure_category",
            "category",
            "rows_affected",
            "isolation",
            "readonly",
            "deferrable",
            "direction",
            "attempt",
        }
    )

    def _span_attrs(payload: HookPayload) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for k in span_attr_keys:
            v = payload.get(k)
            if v is not None:
                attrs[k] = v
        duration = payload.get("duration_ms")
        if isinstance(duration, (int, float)):
            attrs["duration_ms"] = float(duration)
        if _EXEMPLARS_ENABLED:
            fp = payload.get("fingerprint")
            if fp is not None:
                attrs["fingerprint"] = str(fp)
        return attrs

    def _otel_hook(payload: HookPayload) -> None:
        event = str(payload.get("event", ""))

        if event == "query_start":
            # Open ONE span for the query execution. Use the ambient parent
            # context (start_span without context= uses the current context).
            attrs = _span_attrs(payload)
            span = tracer.start_span("ferrum.query", attributes=attrs)
            _active_query_span.set(span)
            return

        if event in ("query_success", "query_failure"):
            span = _active_query_span.get()
            _active_query_span.set(None)
            attrs = _span_attrs(payload)
            if span is None:
                # Defensive: success/failure without a matching query_start
                # (e.g. a dispatcher that bypasses query_start). Create a
                # zero-duration fallback span so the event is still observed.
                span = tracer.start_span("ferrum.query", attributes=attrs)
            else:
                span.set_attributes(attrs)
            try:
                if event == "query_success":
                    query_counter.add(1, attributes=attrs)
                    duration = payload.get("duration_ms")
                    if isinstance(duration, (int, float)):
                        duration_hist.record(float(duration), attributes=attrs)
                elif event == "query_failure":
                    error_counter.add(1, attributes=attrs)
                    query_counter.add(1, attributes=attrs)
                    duration = payload.get("duration_ms")
                    if isinstance(duration, (int, float)):
                        duration_hist.record(float(duration), attributes=attrs)
                # Record a span event for hydration failures paired with a query.
            finally:
                span.end()
            return

        if event == "hydration_failure":
            # Hydration failures happen after query_success; emit a zero-scope
            # error event on a fresh short span (no query span to attach to).
            attrs = _span_attrs(payload)
            with tracer.start_span("ferrum.hydration_failure", attributes=attrs) as span:
                span.set_status(trace.Status(trace.StatusCode.ERROR, "hydration_failure"))
            return

        if event == "transaction_start":
            with tracer.start_span("ferrum.transaction", attributes=_span_attrs(payload)):
                pass
            tx_counter.add(1, attributes=_span_attrs(payload))
            return

        if event == "transaction_end":
            tx_counter.add(1, attributes=_span_attrs(payload))
            return

        if event in (
            "pool_acquire",
            "pool_release",
            "pool_wait",
            "pool_timeout",
            "pool_shutdown",
        ):
            pool_counter.add(1, attributes={"event": event})
            return

        if event in ("migration_start", "migration_end"):
            migration_counter.add(1, attributes=_span_attrs(payload))
            return

        if event == "retry_attempt":
            retry_counter.add(1, attributes=_span_attrs(payload))
            return

        if event == "timeout_event":
            timeout_counter.add(1, attributes=_span_attrs(payload))
            return

    register_hook("*", _otel_hook)
    _OTEL_HOOKS.append(_otel_hook)
    _OTEL_ENABLED = True


def disable_opentelemetry() -> None:
    """Unregister OpenTelemetry hooks (test teardown)."""
    global _OTEL_ENABLED
    for fn in _OTEL_HOOKS:
        unregister_hook(fn)
    _OTEL_HOOKS.clear()
    _OTEL_ENABLED = False


def opentelemetry_enabled() -> bool:
    """Return whether ``enable_opentelemetry()`` has been called."""
    return _OTEL_ENABLED


# ---------------------------------------------------------------------------
# Prometheus text-exposition helper (test/dev; no production exporter built-in).
# ---------------------------------------------------------------------------


def render_prometheus() -> str:
    """Render the in-process metrics as Prometheus text-exposition format.

    Intended for testing and lightweight single-process exposition. For
    production multi-process setups, use the OpenTelemetry bridge with an
    OTel Collector Prometheus exporter (see module docstring example).

    Output example::

        # HELP ferrum_query_count Total queries dispatched.
        # TYPE ferrum_query_count counter
        ferrum_query_count{operation="select",status="ok"} 42.0
    """
    lines: list[str] = []
    help_map = {
        "ferrum.query.count": "Total queries dispatched.",
        "ferrum.query.errors": "Queries that failed.",
        "ferrum.query.duration_ms.sum": "Sum of query durations (ms).",
        "ferrum.query.duration_ms.count": "Count of query duration samples.",
        "ferrum.hydration.errors": "Row hydration failures.",
        "ferrum.pool.acquired": "Pool acquire events.",
        "ferrum.pool.released": "Pool release events.",
        "ferrum.pool.wait": "Pool wait events.",
        "ferrum.pool.timeout": "Pool acquire timeouts.",
        "ferrum.pool.shutdown": "Pool shutdown events.",
        "ferrum.pool.size": "Pool size gauge.",
        "ferrum.pool.idle": "Pool idle gauge.",
        "ferrum.pool.acquired_gauge": "Pool acquired gauge.",
        "ferrum.pool.waiters": "Pool waiters gauge.",
        "ferrum.transaction.count": "Transaction events.",
        "ferrum.transaction.duration_ms.sum": "Sum of transaction durations (ms).",
        "ferrum.transaction.duration_ms.count": "Count of transaction duration samples.",
        "ferrum.migration.count": "Migration events.",
        "ferrum.migration.duration_ms.sum": "Sum of migration durations (ms).",
        "ferrum.migration.duration_ms.count": "Count of migration duration samples.",
        "ferrum.retry.count": "Retry attempts.",
        "ferrum.timeout.count": "Timeout events.",
        "ferrum.timeout.duration_ms.sum": "Sum of timeout durations (ms).",
    }
    type_map = {
        "ferrum.query.count": "counter",
        "ferrum.query.errors": "counter",
        "ferrum.query.duration_ms.sum": "counter",
        "ferrum.query.duration_ms.count": "counter",
        "ferrum.hydration.errors": "counter",
        "ferrum.pool.acquired": "counter",
        "ferrum.pool.released": "counter",
        "ferrum.pool.wait": "counter",
        "ferrum.pool.timeout": "counter",
        "ferrum.pool.shutdown": "counter",
        "ferrum.pool.size": "gauge",
        "ferrum.pool.idle": "gauge",
        "ferrum.pool.acquired_gauge": "gauge",
        "ferrum.pool.waiters": "gauge",
        "ferrum.transaction.count": "counter",
        "ferrum.transaction.duration_ms.sum": "counter",
        "ferrum.transaction.duration_ms.count": "counter",
        "ferrum.migration.count": "counter",
        "ferrum.migration.duration_ms.sum": "counter",
        "ferrum.migration.duration_ms.count": "counter",
        "ferrum.retry.count": "counter",
        "ferrum.timeout.count": "counter",
        "ferrum.timeout.duration_ms.sum": "counter",
    }
    families: dict[str, list[tuple[str, float]]] = {}
    for key, value in sorted(_METRICS.items()):
        brace = key.find("{")
        family = key[:brace] if brace != -1 else key
        families.setdefault(family, []).append((key, value))
    for family in sorted(families):
        family_prom = family.replace(".", "_")
        if family in help_map:
            lines.append(f"# HELP {family_prom} {help_map[family]}")
        if family in type_map:
            lines.append(f"# TYPE {family_prom} {type_map[family]}")
        for key, value in families[family]:
            sample = key.replace(family, family_prom, 1)
            lines.append(f"{sample} {value}")
    return "\n".join(lines) + ("\n" if lines else "")
