"""Unit tests for SQLAlchemy-like SQL echo / verbose mode."""

from __future__ import annotations

import io

import pytest

import ferrum.echo as echo_mod
from ferrum.echo import disable_echo, echo_sql, enable_echo, resolve_echo_level


@pytest.fixture(autouse=True)
def _reset_echo() -> None:
    disable_echo()
    echo_mod._STREAM = echo_mod.sys.stderr
    yield
    disable_echo()
    echo_mod._STREAM = echo_mod.sys.stderr


class TestResolveEchoLevel:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERRUM_ECHO", raising=False)
        assert resolve_echo_level() == "off"

    def test_env_sql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERRUM_ECHO", "1")
        assert resolve_echo_level() == "sql"

    def test_env_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERRUM_ECHO", "debug")
        assert resolve_echo_level() == "debug"

    def test_debug_env_var_does_not_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERRUM_ECHO", raising=False)
        monkeypatch.setenv("DEBUG", "1")
        assert resolve_echo_level() == "off"

    def test_conn_echo_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERRUM_ECHO", "debug")
        assert resolve_echo_level(False) == "off"
        assert resolve_echo_level(True) == "sql"
        assert resolve_echo_level("debug") == "debug"

    def test_global_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERRUM_ECHO", raising=False)
        enable_echo()
        assert resolve_echo_level() == "sql"
        enable_echo(verbose=True)
        assert resolve_echo_level() == "debug"


class TestEchoSql:
    def test_sql_mode_prints_sql_not_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERRUM_ECHO", raising=False)
        buf = io.StringIO()
        enable_echo(stream=buf)
        echo_sql(
            sql='SELECT * FROM "docs" WHERE "id" = $1',
            bound_params=[42],
            param_type_summary=["id:int"],
            model="Doc",
            operation="select",
            duration_ms=1.25,
            row_count=1,
        )
        out = buf.getvalue()
        assert "SELECT * FROM" in out
        assert "param_types=" in out
        assert "params=" not in out
        assert "42" not in out

    def test_debug_mode_prints_bound_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERRUM_ECHO", raising=False)
        buf = io.StringIO()
        enable_echo(verbose=True, stream=buf)
        echo_sql(
            sql="SELECT 1",
            bound_params=["secret-value"],
            model="Doc",
            operation="select",
        )
        out = buf.getvalue()
        assert "params=" in out
        assert "secret-value" in out

    def test_off_prints_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FERRUM_ECHO", raising=False)
        buf = io.StringIO()
        echo_mod._STREAM = buf
        echo_sql(sql="SELECT 1", model="Doc", operation="select")
        assert buf.getvalue() == ""
