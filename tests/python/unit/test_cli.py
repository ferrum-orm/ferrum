"""Unit tests for the Typer CLI app and entrypoint guard."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from ferrum.cli import _require_cli_deps
from ferrum.cli import app as app_module
from ferrum.cli.app import cli

runner = CliRunner()

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip Rich/Click ANSI codes so help assertions work in CI."""
    return _ANSI_ESCAPE.sub("", text)


def test_root_help_renders() -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Ferrum ORM CLI" in result.stdout
    assert "init" in result.stdout
    assert "migrate" in result.stdout


def test_makemigrations_help_lists_flags() -> None:
    result = runner.invoke(cli, ["makemigrations", "--help"])
    assert result.exit_code == 0
    help_text = _plain(result.stdout)
    assert "--name" in help_text
    assert "--migrations-dir" in help_text


def test_migrations_subcommand_help() -> None:
    result = runner.invoke(cli, ["migrations", "--help"])
    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert "apply" in result.stdout


def test_missing_typer_guard_exits_with_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import builtins

    real_import = builtins.__import__

    def _import(name: str, *args: object, **kwargs: object) -> object:
        if name == "typer":
            raise ImportError("No module named 'typer'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _import)

    with pytest.raises(SystemExit) as exc_info:
        _require_cli_deps()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "ferrum[cli]" in captured.out


def test_main_calls_app_when_typer_present(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, bool] = {"bootstrap": False, "app": False}

    def _fake_bootstrap() -> None:
        called["bootstrap"] = True

    def _fake_app() -> None:
        called["app"] = True

    monkeypatch.setattr("ferrum.cli.bootstrap._bootstrap_project", _fake_bootstrap)
    monkeypatch.setattr(app_module, "app", _fake_app)

    from ferrum.cli import main

    main()

    assert called["bootstrap"] is True
    assert called["app"] is True
