"""Integration tests verifying Tier A hook payloads on live query execution."""

from __future__ import annotations

import pytest
from helpers import transient_table

import ferrum
from ferrum.hooks import _TIER_A_KEYS, clear_hooks, register_hook


@pytest.mark.integration
async def test_successful_query_emits_tier_a_hooks_only(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_name = f"ferrum_int_hooks_{unique_suffix}"
    captured: list[dict] = []

    def _capture(payload: dict) -> None:
        captured.append(dict(payload))

    register_hook("*", _capture)
    monkeypatch.setenv("FERRUM_OBS", "A")
    monkeypatch.delenv("FERRUM_OBS_ALLOW_TIER_C", raising=False)

    class HookTarget(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    try:
        async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
            await HookTarget.objects.create(pg_conn, name="probe")
            await HookTarget.objects.filter(name="probe").count(pg_conn)

        events = {p["event"] for p in captured}
        assert "query_start" in events
        assert "query_success" in events

        for payload in captured:
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
            assert "bound_params" not in payload
            assert "sql_text" not in payload
            text = str(payload)
            assert "postgresql://" not in text
            assert "probe" not in text
    finally:
        clear_hooks()


@pytest.mark.integration
async def test_integrity_failure_emits_query_failure_hook(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_hook_fail_{unique_suffix}"
    failures: list[dict] = []

    register_hook("query_failure", failures.append)

    class UniqueRow(ferrum.Model):
        id: int = 0
        code: str = ""

        class Meta:
            table = table_name

    create_sql = f"""
        CREATE TABLE "{table_name}" (
            id SERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE
        )
    """
    drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'

    try:
        async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
            await UniqueRow.objects.create(pg_conn, code="dup")
            with pytest.raises(ferrum.FerrumIntegrityError):
                await UniqueRow.objects.create(pg_conn, code="dup")

        assert failures, "expected query_failure hook after integrity error"
        assert failures[-1]["failure_category"] == "FerrumIntegrityError"
        assert failures[-1]["status"] == "error"
    finally:
        clear_hooks()
