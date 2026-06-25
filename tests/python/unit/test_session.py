"""Unit tests for ferrum.session — transaction-scoped GUC helpers.

Invariants covered:
- set_config with a disallowed GUC name raises FerrumCompileError.
- current_setting with a disallowed GUC name raises FerrumCompileError.
- set_config calls driver.execute with the GUC name hardcoded and value as bound param.
- current_setting calls driver.fetchval with missing_ok as bound param.
- current_setting returns None when the result is None or empty string.
- tenant_transaction opens a transaction, calls set_config, and yields tx.
- Admin mode sets both tenant GUC and admin GUC.
- Rollback path: GUC uses transaction_local=true (verified via SQL text).
- tenant_transaction validates guc_name before opening transaction.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ferrum.errors import FerrumCompileError
from ferrum.session import (
    ALLOWED_GUC_NAMES,
    _validate_guc_name,
    current_setting,
    set_config,
    tenant_transaction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tx(execute_return: str = "", fetchval_return: Any = None) -> MagicMock:
    """Build a minimal Transaction mock with a pre-configured driver."""
    driver = AsyncMock()
    driver.execute = AsyncMock(return_value=execute_return)
    driver.fetchval = AsyncMock(return_value=fetchval_return)

    tx = MagicMock()
    tx._require_driver = MagicMock(return_value=driver)
    return tx


# ---------------------------------------------------------------------------
# _validate_guc_name
# ---------------------------------------------------------------------------


class TestValidateGucName:
    def test_allowed_names_pass(self) -> None:
        for name in ALLOWED_GUC_NAMES:
            _validate_guc_name(name)  # must not raise

    def test_disallowed_name_raises_compile_error(self) -> None:
        with pytest.raises(FerrumCompileError) as exc_info:
            _validate_guc_name("random.user_input")
        assert "random.user_input" in str(exc_info.value)
        assert "FERR-C102" in str(exc_info.value)
        assert exc_info.value.category == "guc_name_not_allowed"

    def test_empty_name_raises(self) -> None:
        with pytest.raises(FerrumCompileError):
            _validate_guc_name("")

    def test_injection_attempt_raises(self) -> None:
        with pytest.raises(FerrumCompileError):
            _validate_guc_name("app.team_id'; DROP TABLE users;--")


# ---------------------------------------------------------------------------
# set_config
# ---------------------------------------------------------------------------


class TestSetConfig:
    @pytest.mark.asyncio
    async def test_set_config_calls_execute_with_bound_value(self) -> None:
        tx = _make_tx()
        driver = tx._require_driver()

        await set_config(tx, "app.team_id", "tenant-123")

        driver.execute.assert_awaited_once()
        sql, value = driver.execute.call_args[0]
        # GUC name is hardcoded in the SQL (allowlist-validated), not a parameter.
        assert "app.team_id" in sql
        # Value is a bound parameter, not interpolated.
        assert "$1" in sql
        assert value == "tenant-123"

    @pytest.mark.asyncio
    async def test_set_config_uses_transaction_local_true(self) -> None:
        """Verify the SQL uses set_config(..., true) for transaction-local semantics."""
        tx = _make_tx()
        driver = tx._require_driver()

        await set_config(tx, "app.team_id", "abc")

        sql = driver.execute.call_args[0][0]
        # The SQL must specify transaction_local=true (third arg to set_config).
        assert "true" in sql.lower()
        assert "set_config" in sql

    @pytest.mark.asyncio
    async def test_set_config_disallowed_name_raises_before_execute(self) -> None:
        tx = _make_tx()
        driver = tx._require_driver()

        with pytest.raises(FerrumCompileError):
            await set_config(tx, "pg_toast_user_input", "value")

        driver.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_config_all_allowed_names(self) -> None:
        for name in ALLOWED_GUC_NAMES:
            tx = _make_tx()
            await set_config(tx, name, "test-value")  # must not raise

    @pytest.mark.asyncio
    async def test_set_config_value_is_bound_not_interpolated(self) -> None:
        """Value containing SQL metacharacters must not appear in the SQL string itself."""
        tx = _make_tx()
        driver = tx._require_driver()
        dangerous_value = "'; DROP TABLE tenants; --"

        await set_config(tx, "app.team_id", dangerous_value)

        sql = driver.execute.call_args[0][0]
        # The dangerous value must NOT be in the SQL string.
        assert dangerous_value not in sql
        # It must be a bound parameter.
        assert "$1" in sql
        # The actual dangerous value must be the bound argument.
        bound_arg = driver.execute.call_args[0][1]
        assert bound_arg == dangerous_value


# ---------------------------------------------------------------------------
# current_setting
# ---------------------------------------------------------------------------


class TestCurrentSetting:
    @pytest.mark.asyncio
    async def test_current_setting_returns_value(self) -> None:
        tx = _make_tx(fetchval_return="team-abc")

        result = await current_setting(tx, "app.team_id")

        assert result == "team-abc"

    @pytest.mark.asyncio
    async def test_current_setting_returns_none_when_result_is_none(self) -> None:
        tx = _make_tx(fetchval_return=None)

        result = await current_setting(tx, "app.team_id", missing_ok=True)

        assert result is None

    @pytest.mark.asyncio
    async def test_current_setting_returns_none_when_result_is_empty_string(self) -> None:
        tx = _make_tx(fetchval_return="")

        result = await current_setting(tx, "app.team_id", missing_ok=True)

        assert result is None

    @pytest.mark.asyncio
    async def test_current_setting_passes_missing_ok_as_bound_param(self) -> None:
        tx = _make_tx(fetchval_return=None)
        driver = tx._require_driver()

        await current_setting(tx, "ferrum.tenant_id", missing_ok=True)

        driver.fetchval.assert_awaited_once()
        sql, bool_arg = driver.fetchval.call_args[0]
        assert "current_setting" in sql
        assert "$1" in sql
        assert bool_arg is True

    @pytest.mark.asyncio
    async def test_current_setting_disallowed_name_raises(self) -> None:
        tx = _make_tx(fetchval_return=None)
        driver = tx._require_driver()

        with pytest.raises(FerrumCompileError):
            await current_setting(tx, "custom.injected_name")

        driver.fetchval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_current_setting_guc_name_not_interpolated_as_param(self) -> None:
        """GUC name is allowlist-validated and hardcoded in SQL — not a bound param."""
        tx = _make_tx(fetchval_return="val")
        driver = tx._require_driver()

        await current_setting(tx, "application_name")

        sql = driver.fetchval.call_args[0][0]
        assert "application_name" in sql


# ---------------------------------------------------------------------------
# tenant_transaction
# ---------------------------------------------------------------------------


class TestTenantTransaction:
    @pytest.mark.asyncio
    async def test_binds_tenant_guc_before_yield(self) -> None:
        """tenant_transaction must call set_config before yielding."""
        bound_tx = MagicMock()
        bound_driver = AsyncMock()
        bound_driver.execute = AsyncMock(return_value="")
        bound_tx._require_driver = MagicMock(return_value=bound_driver)

        conn = MagicMock()

        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_transaction(**_: Any):  # type: ignore[misc]
            yield bound_tx

        conn.transaction = _fake_transaction

        executed_sqls: list[str] = []

        async def _capture_execute(sql: str, *args: Any) -> str:
            executed_sqls.append(sql)
            return ""

        bound_driver.execute = _capture_execute

        async with tenant_transaction(conn, "tenant-xyz") as tx:
            assert tx is bound_tx
            # set_config must have been called before we get here.
            assert any("app.team_id" in s for s in executed_sqls)

    @pytest.mark.asyncio
    async def test_admin_mode_sets_both_gucs(self) -> None:
        """admin=True must set both the tenant GUC and the admin GUC."""
        bound_tx = MagicMock()
        bound_driver = AsyncMock()
        executed: list[tuple[str, ...]] = []

        async def _capture(sql: str, *args: Any) -> str:
            executed.append((sql, *args))
            return ""

        bound_driver.execute = _capture
        bound_tx._require_driver = MagicMock(return_value=bound_driver)

        conn = MagicMock()

        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_transaction(**_: Any):  # type: ignore[misc]
            yield bound_tx

        conn.transaction = _fake_transaction

        async with tenant_transaction(conn, "t1", admin=True):
            pass

        # Two set_config calls: one for app.team_id, one for app.platform_admin.
        assert len(executed) == 2
        sqls = [e[0] for e in executed]
        assert any("app.team_id" in s for s in sqls)
        assert any("app.platform_admin" in s for s in sqls)
        # Admin GUC value must be 'true'.
        admin_call = next(e for e in executed if "app.platform_admin" in e[0])
        assert admin_call[1] == "true"

    @pytest.mark.asyncio
    async def test_non_admin_sets_only_tenant_guc(self) -> None:
        bound_tx = MagicMock()
        bound_driver = AsyncMock()
        executed: list[str] = []

        async def _capture(sql: str, *args: Any) -> str:
            executed.append(sql)
            return ""

        bound_driver.execute = _capture
        bound_tx._require_driver = MagicMock(return_value=bound_driver)

        conn = MagicMock()

        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_transaction(**_: Any):  # type: ignore[misc]
            yield bound_tx

        conn.transaction = _fake_transaction

        async with tenant_transaction(conn, "t2", admin=False):
            pass

        assert len(executed) == 1
        assert "app.team_id" in executed[0]

    @pytest.mark.asyncio
    async def test_disallowed_guc_name_raises_before_transaction(self) -> None:
        """Allowlist validation must fail before opening the transaction."""
        conn = MagicMock()
        conn.transaction = AsyncMock()

        with pytest.raises(FerrumCompileError):
            async with tenant_transaction(conn, "t", guc_name="injected.name"):
                pass  # pragma: no cover

        conn.transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_uuid_tenant_id_converted_to_string(self) -> None:
        bound_tx = MagicMock()
        bound_driver = AsyncMock()
        captured_values: list[str] = []

        async def _capture(sql: str, value: str) -> str:
            captured_values.append(value)
            return ""

        bound_driver.execute = _capture
        bound_tx._require_driver = MagicMock(return_value=bound_driver)

        conn = MagicMock()

        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_transaction(**_: Any):  # type: ignore[misc]
            yield bound_tx

        conn.transaction = _fake_transaction

        uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        async with tenant_transaction(conn, uid):
            pass

        assert captured_values[0] == str(uid)

    @pytest.mark.asyncio
    async def test_transaction_kwargs_forwarded(self) -> None:
        """isolation and readonly must be forwarded to conn.transaction()."""
        conn = MagicMock()
        received_kwargs: dict[str, Any] = {}

        import contextlib

        bound_tx = MagicMock()
        bound_driver = AsyncMock()
        bound_driver.execute = AsyncMock(return_value="")
        bound_tx._require_driver = MagicMock(return_value=bound_driver)

        @contextlib.asynccontextmanager
        async def _fake_transaction(**kwargs: Any):  # type: ignore[misc]
            received_kwargs.update(kwargs)
            yield bound_tx

        conn.transaction = _fake_transaction

        async with tenant_transaction(
            conn, "t", isolation="serializable", readonly=True
        ):
            pass

        assert received_kwargs.get("isolation") == "serializable"
        assert received_kwargs.get("readonly") is True
