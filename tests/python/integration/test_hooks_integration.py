"""Integration tests verifying Tier A hook payloads on live query execution.

Verifies the ratified §5a "Safe error fields" contract on live PostgreSQL:
hook payloads carry ``failure_category`` and bound values are absent.
"""

from __future__ import annotations

import pytest

import ferrum
from ferrum.errors import ERROR_CATEGORIES
from ferrum.hooks import _TIER_A_KEYS, clear_hooks, register_hook

from .backends import Backend
from .schema import Column, transient_table


@pytest.mark.integration
async def test_successful_query_emits_tier_a_hooks_only(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
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
            await HookTarget.objects.create(conn, name="probe")
            await HookTarget.objects.filter(name="probe").count(conn)

        events = {p["event"] for p in captured}
        assert "query_start" in events
        assert "query_success" in events

        for payload in captured:
            assert set(payload.keys()).issubset(_TIER_A_KEYS)
            assert "bound_params" not in payload
            assert "sql_text" not in payload
            text = str(payload)
            assert "://" not in text
            assert "probe" not in text
    finally:
        clear_hooks()


@pytest.mark.integration
async def test_integrity_failure_emits_query_failure_hook(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
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
            await UniqueRow.objects.create(conn, code="dup")
            with pytest.raises(ferrum.FerrumIntegrityError) as exc_info:
                await UniqueRow.objects.create(conn, code="dup")

        assert failures, "expected query_failure hook after integrity error"
        assert failures[-1]["failure_category"] == "FerrumIntegrityError"
        assert failures[-1]["status"] == "error"
        # §5a: Tier-A payload keys are a subset of _TIER_A_KEYS.
        assert set(failures[-1].keys()).issubset(_TIER_A_KEYS)
        # §5a: bound values never appear in default hook payloads.
        payload_str = str(failures[-1])
        assert "dup" not in payload_str, "Bound value 'dup' must not appear in hook payload"
        # §5a: the mapped exception carries structured category in the closed enum.
        assert exc_info.value.category in ERROR_CATEGORIES, (
            f"category={exc_info.value.category!r} not in closed enum"
        )
        if backend.name == "postgres":
            assert exc_info.value.sqlstate == "23505", (
                f"expected sqlstate=23505, got {exc_info.value.sqlstate!r}"
            )
    finally:
        clear_hooks()
