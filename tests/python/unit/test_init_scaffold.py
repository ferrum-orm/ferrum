"""Unit tests for the init scaffold safety properties (INIT-1, INIT-2)."""

from __future__ import annotations

import pathlib

import pytest

from ferrum.cli.init import run_init


class TestInitScaffold:
    def test_writes_gitignore_excluding_env(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".env" in gitignore

    def test_docker_compose_binds_to_localhost(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-1: generated compose must bind postgres to 127.0.0.1."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        compose = (tmp_path / "docker-compose.yml").read_text()
        assert "127.0.0.1" in compose
        # Must NOT expose to all interfaces
        assert '"0.0.0.0:5432:5432"' not in compose
        assert "'0.0.0.0:5432:5432'" not in compose

    def test_env_example_is_created(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        env_example = tmp_path / ".env.example"
        assert env_example.exists()
        # Must be a template, not real credentials
        assert "changeme" in env_example.read_text()

    def test_refuses_path_outside_cwd(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: init must refuse writes outside the current working directory."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            run_init(name="/tmp/escape_attempt")  # noqa: S108

    def test_does_not_overwrite_existing_files(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / ".gitignore"
        existing.write_text("# my custom gitignore\n")
        run_init(name=".")
        assert "# my custom gitignore" in existing.read_text()
