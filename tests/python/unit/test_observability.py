"""Unit tests for observability metrics, OTel bridge, and telemetry redaction.

Covers W4-A acceptance criteria:
- Low-cardinality query/transaction/pool/migration/retry/timeout/error metrics.
- Query fingerprints NOT in default metric labels (opt-in via enable_exemplars).
- One span around actual query execution (start → success/failure pairing).
- No values, DSNs, credentials, or row data under default telemetry.
- W1-D Tier-A hook contract preserved (redaction still strips non-Tier-A keys).
"""

from __future__ import annotations

import sys
import types

import pytest

from ferrum.hooks import clear_hooks, dispatch
from ferrum.observability import (
    _active_query_span,
    disable_exemplars,
    disable_metrics,
    disable_opentelemetry,
    enable_exemplars,
    enable_metrics,
    enable_opentelemetry,
    exemplars_enabled,
    get_metric_label_sets,
    get_metrics,
    render_prometheus,
    reset_metrics,
    set_gauge,
)


@pytest.fixture(autouse=True)
def _clean_observability() -> None:
    clear_hooks()
    reset_metrics()
    disable_metrics()
    disable_opentelemetry()
    _active_query_span.set(None)
    disable_exemplars()
    yield
    clear_hooks()
    reset_metrics()
    disable_metrics()
    disable_opentelemetry()
    _active_query_span.set(None)
    disable_exemplars()


# ---------------------------------------------------------------------------
# Query metrics — Tier-A-safe labels, fingerprint opt-in.
# ---------------------------------------------------------------------------


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
    assert any(k.startswith("ferrum.query.duration_ms.sum") for k in metrics)


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


def test_fingerprint_not_in_default_labels() -> None:
    """Criterion 3: query fingerprints MUST NOT appear in default metric labels."""
    enable_metrics()
    dispatch(
        {
            "event": "query_success",
            "fingerprint": "fp:User:select:where_active_eq",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
        }
    )
    metrics = get_metrics()
    serialized = str(metrics)
    assert "fp:User:select:where_active_eq" not in serialized, (
        "Fingerprint must not appear in default metric labels (cardinality)"
    )


def test_fingerprint_in_labels_when_exemplars_enabled() -> None:
    """Opt-in: enable_exemplars() allows fingerprint labels."""
    enable_metrics()
    enable_exemplars()
    assert exemplars_enabled() is True
    dispatch(
        {
            "event": "query_success",
            "fingerprint": "fp:User:select",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
        }
    )
    metrics = get_metrics()
    serialized = str(metrics)
    assert "fp:User:select" in serialized, "Fingerprint should appear when exemplars are enabled"


def test_disable_exemplars_removes_fingerprint_labels() -> None:
    enable_metrics()
    enable_exemplars()
    dispatch(
        {
            "event": "query_success",
            "fingerprint": "fp:User:select",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
        }
    )
    reset_metrics()
    disable_exemplars()
    assert exemplars_enabled() is False
    dispatch(
        {
            "event": "query_success",
            "fingerprint": "fp:User:select",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
        }
    )
    metrics = get_metrics()
    serialized = str(metrics)
    assert "fp:User:select" not in serialized


def test_label_cardinality_is_low() -> None:
    """Criterion 2: metric label-sets must be low-cardinality.

    Simulate many distinct query fingerprints — the label-set for
    ferrum.query.count must contain only operation/status pairs, not one entry
    per fingerprint.
    """
    enable_metrics()
    for i in range(50):
        dispatch(
            {
                "event": "query_success",
                "fingerprint": f"fp:User:select:variant_{i}",
                "operation": "select",
                "duration_ms": float(i),
                "status": "ok",
            }
        )
    label_sets = get_metric_label_sets()
    count_labels = label_sets.get("ferrum.query.count", set())
    assert len(count_labels) == 1, (
        f"Expected 1 label-set (operation=select,status=ok), got {count_labels}"
    )


def test_label_cardinality_with_exemplars_grows() -> None:
    """When exemplars are enabled, label-set grows with fingerprints (opt-in)."""
    enable_metrics()
    enable_exemplars()
    for i in range(10):
        dispatch(
            {
                "event": "query_success",
                "fingerprint": f"fp:variant_{i}",
                "operation": "select",
                "duration_ms": float(i),
                "status": "ok",
            }
        )
    label_sets = get_metric_label_sets()
    count_labels = label_sets.get("ferrum.query.count", set())
    assert len(count_labels) == 10, (
        f"Expected 10 label-sets with exemplars, got {len(count_labels)}"
    )


# ---------------------------------------------------------------------------
# Query failure / hydration failure metrics.
# ---------------------------------------------------------------------------


def test_query_failure_records_error_metric() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "query_failure",
            "fingerprint": "fp:User:insert",
            "duration_ms": 0.5,
            "failure_category": "FerrumIntegrityError",
            "category": "unique_violation",
            "status": "error",
        }
    )
    metrics = get_metrics()
    assert any(k.startswith("ferrum.query.errors") for k in metrics)
    assert any(k.startswith("ferrum.query.count") for k in metrics)
    serialized = str(metrics)
    assert "unique_violation" in serialized, "category label should be present"
    assert "FerrumIntegrityError" in serialized, "failure_category label should be present"


def test_hydration_failure_records_metric() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "hydration_failure",
            "fingerprint": "fp:Post:select",
            "failure_category": "FerrumHydrationError",
            "model": "Post",
            "category": "hydration",
            "status": "error",
        }
    )
    metrics = get_metrics()
    assert any(k.startswith("ferrum.hydration.errors") for k in metrics)


# ---------------------------------------------------------------------------
# Pool / transaction / migration / retry / timeout metrics.
# ---------------------------------------------------------------------------


def test_pool_acquire_metric() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "pool_acquire",
            "pool_size": 10,
            "pool_idle": 5,
            "pool_acquired_count": 5,
            "pool_waiters": 0,
        }
    )
    metrics = get_metrics()
    assert any("ferrum.pool.acquired" in k for k in metrics), metrics
    assert any("ferrum.pool.size" in k for k in metrics), metrics


def test_pool_release_metric() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "pool_release",
            "pool_size": 10,
            "pool_idle": 6,
            "pool_acquired_count": 4,
            "pool_waiters": 0,
        }
    )
    metrics = get_metrics()
    assert any("ferrum.pool.released" in k for k in metrics), metrics


def test_pool_timeout_metric() -> None:
    enable_metrics()
    dispatch({"event": "pool_timeout", "pool_waiters": 3})
    metrics = get_metrics()
    assert any("ferrum.pool.timeout" in k for k in metrics), metrics


def test_pool_shutdown_metric() -> None:
    enable_metrics()
    dispatch({"event": "pool_shutdown", "pool_size": 2, "pool_acquired_count": 1})
    metrics = get_metrics()
    assert any("ferrum.pool.shutdown" in k for k in metrics), metrics


def test_transaction_start_end_metrics() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "transaction_start",
            "isolation": "serializable",
            "readonly": True,
            "deferrable": False,
        }
    )
    dispatch(
        {
            "event": "transaction_end",
            "duration_ms": 25.0,
            "status": "ok",
            "isolation": "serializable",
            "readonly": True,
            "deferrable": False,
        }
    )
    metrics = get_metrics()
    assert any("ferrum.transaction.count" in k for k in metrics), metrics
    assert any("ferrum.transaction.duration_ms.sum" in k for k in metrics), metrics


def test_migration_event_metrics() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "migration_end",
            "direction": "up",
            "status": "ok",
            "duration_ms": 120.0,
        }
    )
    metrics = get_metrics()
    assert any("ferrum.migration.count" in k for k in metrics), metrics
    assert any("ferrum.migration.duration_ms.sum" in k for k in metrics), metrics


def test_retry_attempt_metric() -> None:
    enable_metrics()
    dispatch({"event": "retry_attempt", "attempt": 2, "category": "deadlock"})
    metrics = get_metrics()
    assert any("ferrum.retry.count" in k for k in metrics), metrics
    serialized = str(metrics)
    assert "deadlock" in serialized


def test_timeout_event_metric() -> None:
    enable_metrics()
    dispatch({"event": "timeout_event", "category": "timeout", "duration_ms": 30.0})
    metrics = get_metrics()
    assert any("ferrum.timeout.count" in k for k in metrics), metrics
    assert any("ferrum.timeout.duration_ms.sum" in k for k in metrics), metrics


# ---------------------------------------------------------------------------
# Security: no values, DSNs, credentials, row data under default telemetry.
# ---------------------------------------------------------------------------


class TestTelemetryRedaction:
    """Security tests: telemetry must never leak secrets (§3, criterion 6)."""

    @pytest.mark.parametrize(
        "event,extra_keys",
        [
            (
                "query_success",
                {
                    "bound_params": ["secret_pw", "secret@example.com"],
                    "sql_text": "SELECT * FROM users WHERE password = $1",
                    "dsn": "postgresql://user:secret@host:5432/db",
                    "row_data": {"email": "row_leak@example.com"},
                },
            ),
            (
                "query_failure",
                {
                    "bound_params": ["secret_value"],
                    "sql_text": "INSERT INTO users VALUES ($1, $2)",
                    "raw_db_message": "DETAIL: Key (email)=(leak@example.com) exists.",
                    "dsn": "postgresql://user:secret_pw@host/db",
                },
            ),
        ],
    )
    def test_default_telemetry_has_no_secrets(
        self, event: str, extra_keys: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FERRUM_OBS", raising=False)
        monkeypatch.delenv("FERRUM_OBS_ALLOW_TIER_C", raising=False)
        enable_metrics()
        payload = {
            "event": event,
            "fingerprint": "fp:User:select",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
            "failure_category": "FerrumDatabaseError",
            "category": "unknown",
        }
        payload.update(extra_keys)
        dispatch(payload)
        metrics = get_metrics()
        serialized = str(metrics)
        # Sentinel values that must NEVER appear in telemetry.
        for sentinel in (
            "secret_pw",
            "secret@example.com",
            "secret@host",
            "row_leak@example.com",
            "leak@example.com",
            "postgresql://user:secret",
            "SELECT * FROM users",
        ):
            assert sentinel not in serialized, (
                f"Secret sentinel {sentinel!r} leaked into metrics: {serialized}"
            )

    def test_tier_b_promotion_does_not_add_fingerprint_to_default_labels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier B adds normalized SQL, but fingerprint still must not be a label."""
        monkeypatch.setenv("FERRUM_OBS", "B")
        enable_metrics()
        dispatch(
            {
                "event": "query_success",
                "fingerprint": "fp:tier_b_test",
                "operation": "select",
                "duration_ms": 1.0,
                "status": "ok",
                "sql_normalized": "SELECT id FROM users WHERE active = ?",
            }
        )
        metrics = get_metrics()
        serialized = str(metrics)
        # Fingerprint still excluded from labels by default.
        assert "fp:tier_b_test" not in serialized
        # sql_normalized may be in the hook payload (Tier B) but it is not a
        # metric label and not recorded by the metrics hook.
        assert "SELECT id FROM users" not in serialized


# ---------------------------------------------------------------------------
# Prometheus exposition.
# ---------------------------------------------------------------------------


def test_render_prometheus_emits_help_type() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "query_success",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
        }
    )
    out = render_prometheus()
    assert "# HELP ferrum_query_count" in out
    assert "# TYPE ferrum_query_count counter" in out
    assert "ferrum_query_count" in out


def test_render_prometheus_no_secrets() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "query_success",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
            "bound_params": ["leak_in_prom"],
        }
    )
    out = render_prometheus()
    assert "leak_in_prom" not in out


# ---------------------------------------------------------------------------
# OTel bridge — import-error path.
# ---------------------------------------------------------------------------


def test_enable_opentelemetry_requires_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    from ferrum.errors import FerrumConfigError

    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with pytest.raises(FerrumConfigError, match="otel"):
        enable_opentelemetry()


# ---------------------------------------------------------------------------
# Finding #1 regression: metric handlers must not cross-contaminate by event.
# ---------------------------------------------------------------------------


def test_query_dispatch_does_not_inflate_non_query_metrics() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "query_success",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
        }
    )
    metrics = get_metrics()
    assert not any("ferrum.transaction" in k for k in metrics), metrics
    assert not any("ferrum.migration" in k for k in metrics), metrics
    assert not any("ferrum.retry" in k for k in metrics), metrics
    assert not any("ferrum.timeout" in k for k in metrics), metrics


def test_pool_dispatch_does_not_inflate_query_metrics() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "pool_acquire",
            "pool_size": 10,
            "pool_idle": 5,
            "pool_acquired_count": 5,
            "pool_waiters": 0,
        }
    )
    metrics = get_metrics()
    assert not any("ferrum.query" in k for k in metrics), metrics
    assert not any("ferrum.transaction" in k for k in metrics), metrics
    assert not any("ferrum.hydration" in k for k in metrics), metrics


def test_transaction_dispatch_does_not_inflate_query_metrics() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "transaction_start",
            "isolation": "serializable",
            "readonly": True,
            "deferrable": False,
        }
    )
    metrics = get_metrics()
    assert not any("ferrum.query" in k for k in metrics), metrics
    assert not any("ferrum.migration" in k for k in metrics), metrics


# ---------------------------------------------------------------------------
# Finding #3 regression: HELP/TYPE emitted exactly once per metric family.
# ---------------------------------------------------------------------------


def test_render_prometheus_help_type_once_per_family() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "query_success",
            "operation": "select",
            "duration_ms": 1.0,
            "status": "ok",
        }
    )
    dispatch(
        {
            "event": "query_success",
            "operation": "insert",
            "duration_ms": 2.0,
            "status": "ok",
        }
    )
    out = render_prometheus()
    assert out.count("# HELP ferrum_query_count") == 1, out
    assert out.count("# TYPE ferrum_query_count counter") == 1, out
    assert out.count("ferrum_query_count{operation=select") == 1, out
    assert out.count("ferrum_query_count{operation=insert") == 1, out


# ---------------------------------------------------------------------------
# Finding #4 regression: pool gauges set (not accumulate).
# ---------------------------------------------------------------------------


def test_pool_gauge_sets_not_accumulates() -> None:
    enable_metrics()
    dispatch(
        {
            "event": "pool_acquire",
            "pool_size": 10,
            "pool_idle": 5,
            "pool_acquired_count": 5,
            "pool_waiters": 0,
        }
    )
    dispatch(
        {
            "event": "pool_acquire",
            "pool_size": 8,
            "pool_idle": 4,
            "pool_acquired_count": 4,
            "pool_waiters": 1,
        }
    )
    metrics = get_metrics()
    assert metrics.get("ferrum.pool.size") == 8.0, metrics
    assert metrics.get("ferrum.pool.idle") == 4.0, metrics
    assert metrics.get("ferrum.pool.acquired_gauge") == 4.0, metrics
    assert metrics.get("ferrum.pool.waiters") == 1.0, metrics


def test_set_gauge_overwrites_not_accumulates() -> None:
    set_gauge("test.gauge", 10.0)
    set_gauge("test.gauge", 8.0)
    metrics = get_metrics()
    assert metrics.get("test.gauge") == 8.0, metrics


# ---------------------------------------------------------------------------
# Finding #2: behavioral OTel span-lifecycle tests (fake in-memory tracer).
# ---------------------------------------------------------------------------


class _FakeStatusCode:
    ERROR = "ERROR"
    OK = "OK"


class _FakeStatus:
    def __init__(self, code: str, description: str = "") -> None:
        self.code = code
        self.description = description


class _FakeSpan:
    def __init__(self, name: str, attributes: dict | None = None) -> None:
        self.name = name
        self.attributes = dict(attributes or {})
        self.ended = False
        self.status: _FakeStatus | None = None

    def set_attributes(self, attrs: dict) -> None:
        self.attributes.update(attrs)

    def set_status(self, status: _FakeStatus) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True

    def __enter__(self) -> _FakeSpan:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.end()


class _FakeCounter:
    def add(self, n: float, attributes: dict | None = None) -> None:
        pass


class _FakeHistogram:
    def record(self, n: float, attributes: dict | None = None) -> None:
        pass


class _FakeMeter:
    def create_counter(self, name: str) -> _FakeCounter:
        return _FakeCounter()

    def create_histogram(self, name: str) -> _FakeHistogram:
        return _FakeHistogram()


class _FakeTracer:
    def __init__(self, spans: list[_FakeSpan]) -> None:
        self.spans = spans

    def start_span(
        self, name: str, attributes: dict | None = None, context: object = None
    ) -> _FakeSpan:
        span = _FakeSpan(name, attributes)
        self.spans.append(span)
        return span


def _install_fake_otel(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeTracer, list[_FakeSpan]]:
    tracer = _FakeTracer([])
    trace_mod = types.ModuleType("opentelemetry.trace")
    trace_mod.set_tracer_provider = lambda tp: None  # type: ignore[attr-defined]
    trace_mod.get_tracer = lambda name: tracer  # type: ignore[attr-defined]
    trace_mod.Status = _FakeStatus  # type: ignore[attr-defined]
    trace_mod.StatusCode = _FakeStatusCode  # type: ignore[attr-defined]
    metrics_mod = types.ModuleType("opentelemetry.metrics")
    metrics_mod.set_meter_provider = lambda mp: None  # type: ignore[attr-defined]
    metrics_mod.get_meter = lambda name: _FakeMeter()  # type: ignore[attr-defined]
    otel_mod = types.ModuleType("opentelemetry")
    otel_mod.trace = trace_mod  # type: ignore[attr-defined]
    otel_mod.metrics = metrics_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", otel_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.metrics", metrics_mod)
    return tracer, tracer.spans


class TestOTelSpanLifecycle:
    def test_query_success_creates_span_with_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tracer, spans = _install_fake_otel(monkeypatch)
        enable_opentelemetry()
        try:
            dispatch(
                {
                    "event": "query_start",
                    "model": "User",
                    "operation": "select",
                    "table": "users",
                }
            )
            dispatch(
                {
                    "event": "query_success",
                    "duration_ms": 5.0,
                    "operation": "select",
                    "status": "ok",
                }
            )
            assert len(spans) == 1, spans
            span = spans[0]
            assert span.name == "ferrum.query"
            assert span.ended is True
            assert "duration_ms" in span.attributes
            assert span.attributes["duration_ms"] == 5.0
            assert _active_query_span.get() is None
        finally:
            disable_opentelemetry()

    def test_query_failure_creates_span(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _tracer, spans = _install_fake_otel(monkeypatch)
        enable_opentelemetry()
        try:
            dispatch(
                {
                    "event": "query_start",
                    "model": "User",
                    "operation": "insert",
                    "table": "users",
                }
            )
            dispatch(
                {
                    "event": "query_failure",
                    "duration_ms": 0.5,
                    "operation": "insert",
                    "status": "error",
                    "failure_category": "FerrumIntegrityError",
                }
            )
            assert len(spans) == 1, spans
            span = spans[0]
            assert span.name == "ferrum.query"
            assert span.ended is True
            assert span.attributes.get("duration_ms") == 0.5
            assert _active_query_span.get() is None
        finally:
            disable_opentelemetry()

    def test_orphan_success_creates_fallback_span(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _tracer, spans = _install_fake_otel(monkeypatch)
        enable_opentelemetry()
        try:
            dispatch(
                {
                    "event": "query_success",
                    "duration_ms": 3.0,
                    "operation": "select",
                    "status": "ok",
                }
            )
            assert len(spans) == 1, spans
            span = spans[0]
            assert span.name == "ferrum.query"
            assert span.ended is True
            assert span.attributes.get("duration_ms") == 3.0
            assert _active_query_span.get() is None
        finally:
            disable_opentelemetry()
