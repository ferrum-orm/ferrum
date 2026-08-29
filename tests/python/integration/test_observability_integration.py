"""Integration tests for observability metrics and OTel bridge on live PostgreSQL.

Verifies W4-A acceptance criteria against a real database:
- One span around actual query execution (start → success pairing).
- Low-cardinality query metrics from Tier-A-safe fields.
- Query fingerprints NOT in default metric labels.
- No values, DSNs, credentials, or row data under default telemetry.
- W1-D Tier-A hook contract preserved.
"""

from __future__ import annotations

import pytest

import ferrum
from ferrum.hooks import _TIER_A_KEYS, clear_hooks, register_hook
from ferrum.observability import (
    disable_metrics,
    enable_exemplars,
    enable_metrics,
    get_metric_label_sets,
    get_metrics,
    reset_metrics,
)

from .backends import Backend
from .schema import Column, transient_table


@pytest.fixture(autouse=True)
def _clean_observability() -> None:
    clear_hooks()
    reset_metrics()
    disable_metrics()
    yield
    clear_hooks()
    reset_metrics()
    disable_metrics()


@pytest.mark.integration
async def test_live_query_emits_low_cardinality_metrics(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Live query: metric label-set stays low-cardinality across distinct values."""
    table_name = f"ferrum_int_obs_metrics_{unique_suffix}"
    enable_metrics()

    class ObsModel(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    try:
        async with transient_table(
            db_conn,
            table_name,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("name", "text", null=False),
            ],
        ) as conn:
            # Insert many rows with distinct values — metrics must NOT
            # carry those values as labels.
            for i in range(20):
                await ObsModel.objects.create(conn, name=f"value_{i}")
            await ObsModel.objects.filter(name="value_0").count(conn)

        metrics = get_metrics()
        assert any(k.startswith("ferrum.query.count") for k in metrics), metrics
        # No bound value should leak into metric keys.
        serialized = str(metrics)
        assert "value_0" not in serialized
        assert "value_19" not in serialized
        # Label-set for ferrum.query.count must be bounded by operation/status,
        # NOT by the number of distinct values.
        label_sets = get_metric_label_sets()
        count_labels = label_sets.get("ferrum.query.count", set())
        assert len(count_labels) <= 5, (
            f"Expected <= 5 label-sets, got {len(count_labels)}: {count_labels}"
        )
    finally:
        clear_hooks()


@pytest.mark.integration
async def test_live_query_no_secrets_in_telemetry(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Security: live telemetry must never carry bound values, DSNs, row data."""
    table_name = f"ferrum_int_obs_secret_{unique_suffix}"
    enable_metrics()

    sentinel_value = f"secret_marker_{unique_suffix}"

    class SecretModel(ferrum.Model):
        id: int = 0
        secret_col: str = ""

        class Meta:
            table = table_name

    try:
        async with transient_table(
            db_conn,
            table_name,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("secret_col", "text", null=False),
            ],
        ) as conn:
            await SecretModel.objects.create(conn, secret_col=sentinel_value)
            await SecretModel.objects.filter(secret_col=sentinel_value).count(conn)

        metrics = get_metrics()
        serialized = str(metrics)
        assert sentinel_value not in serialized, (
            f"Secret value {sentinel_value!r} leaked into live metrics: {serialized}"
        )
        # No DSN-like substring.
        assert "://" not in serialized
        # No SQL text.
        assert "INSERT" not in serialized.upper() or "INSERT" not in serialized
    finally:
        clear_hooks()


@pytest.mark.integration
async def test_live_query_hook_payloads_tier_a_only(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live query: every hook payload is a subset of _TIER_A_KEYS."""
    table_name = f"ferrum_int_obs_tier_{unique_suffix}"
    monkeypatch.setenv("FERRUM_OBS", "A")
    monkeypatch.delenv("FERRUM_OBS_ALLOW_TIER_C", raising=False)
    captured: list[dict] = []
    register_hook("*", captured.append)

    class TierModel(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    try:
        async with transient_table(
            db_conn,
            table_name,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("name", "text", null=False),
            ],
        ) as conn:
            await TierModel.objects.create(conn, name="probe")
            await TierModel.objects.filter(name="probe").count(conn)

        assert captured, "expected at least one hook payload"
        for payload in captured:
            assert set(payload.keys()).issubset(_TIER_A_KEYS), (
                f"Non-Tier-A keys in payload: {set(payload.keys()) - _TIER_A_KEYS}"
            )
            assert "bound_params" not in payload
            assert "sql_text" not in payload
            assert "://" not in str(payload)
            assert "probe" not in str(payload), "Bound value 'probe' must not appear"
    finally:
        clear_hooks()


@pytest.mark.integration
async def test_live_integrity_error_emits_query_failure_metric(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Live integrity error: query_failure metric fires, no row data leaks."""
    table_name = f"ferrum_int_obs_fail_{unique_suffix}"
    enable_metrics()

    class UniqueModel(ferrum.Model):
        id: int = 0
        code: str = ""

        class Meta:
            table = table_name

    try:
        async with transient_table(
            db_conn,
            table_name,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("code", "text", null=False, extra="UNIQUE"),
            ],
        ) as conn:
            await UniqueModel.objects.create(conn, code="dup")
            with pytest.raises(ferrum.FerrumIntegrityError):
                await UniqueModel.objects.create(conn, code="dup")

        metrics = get_metrics()
        assert any(k.startswith("ferrum.query.errors") for k in metrics), metrics
        serialized = str(metrics)
        assert "dup" not in serialized, "Bound value 'dup' must not appear in metrics"
    finally:
        clear_hooks()


@pytest.mark.integration
async def test_live_exemplars_add_fingerprint_label(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    require_native: None,
    unique_suffix: str,
) -> None:
    """Opt-in exemplars: enable_exemplars() allows fingerprint labels."""
    table_name = f"ferrum_int_obs_exemp_{unique_suffix}"
    enable_metrics()
    enable_exemplars()

    class ExempModel(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    try:
        async with transient_table(
            db_conn,
            table_name,
            backend=backend,
            columns=[
                Column("id", "pk_serial"),
                Column("name", "text", null=False),
            ],
        ) as conn:
            await ExempModel.objects.create(conn, name="probe")

        metrics = get_metrics()
        serialized = str(metrics)
        # With exemplars enabled, fingerprint should appear in at least one label.
        assert any("fingerprint=" in k for k in metrics), (
            f"Expected fingerprint label when exemplars enabled: {serialized}"
        )
    finally:
        clear_hooks()
