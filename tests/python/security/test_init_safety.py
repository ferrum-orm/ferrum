"""Security qualification suite — init scaffold safety (INIT-1, INIT-2)."""

from __future__ import annotations

import pathlib

import pytest

from ferrum.cli.init import run_init

pytestmark = pytest.mark.security


class TestInitSafety:
    def test_init_binds_postgres_to_localhost(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-1: Generated docker-compose must bind PostgreSQL to 127.0.0.1."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        compose = (tmp_path / "docker-compose.yml").read_text()
        assert "127.0.0.1" in compose

    def test_init_gitignore_excludes_dotenv(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: Generated .gitignore must exclude .env files."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".env" in gitignore

    def test_init_refuses_path_traversal(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: init must refuse writes outside cwd (path traversal guard)."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            run_init(name="/tmp/evil")  # noqa: S108
        with pytest.raises(SystemExit):
            run_init(name="../escape")
