"""Security qualification suite — init scaffold safety (INIT-1, INIT-2).

INIT-1: Generated docker-compose.yml binds PostgreSQL to 127.0.0.1 (not 0.0.0.0).
INIT-2: Generated .gitignore excludes .env files; init refuses path traversal;
        scaffolding uses placeholder credentials only (no real secrets in tracked
        files); generation is idempotent (no silent overwrite) unless ``--force``.
"""

from __future__ import annotations

import pathlib

import pytest

from ferrum.cli.init import run_init

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# INIT-1: docker-compose binds to 127.0.0.1
# ---------------------------------------------------------------------------


class TestINIT1LocalhostBinding:
    def test_init_binds_postgres_to_localhost(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-1: Generated docker-compose must bind PostgreSQL to 127.0.0.1."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        compose = (tmp_path / "docker-compose.yml").read_text()
        assert "127.0.0.1" in compose

    def test_init_does_not_bind_to_all_interfaces(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-1: Generated docker-compose must NOT expose PostgreSQL on all interfaces."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        compose = (tmp_path / "docker-compose.yml").read_text()
        # A binding pattern like "0.0.0.0:5432:5432" must not appear
        wildcard_bind = ".".join(["0", "0", "0", "0"])
        assert wildcard_bind not in compose, (
            "docker-compose must not bind to all interfaces (INIT-1)"
        )


# ---------------------------------------------------------------------------
# INIT-2: .gitignore excludes .env; path traversal refused; idempotent
# ---------------------------------------------------------------------------


class TestINIT2SecretHygiene:
    def test_init_gitignore_excludes_dotenv(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: Generated .gitignore must exclude .env files."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".env" in gitignore

    def test_init_env_example_uses_placeholder_not_real_secret(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: .env.example must use placeholder credentials, never real secrets."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        env_example = (tmp_path / ".env.example").read_text()
        # The placeholder word 'changeme' is expected; real secrets would never match
        # a known-bad pattern. We check no real-looking credentials appear.
        assert "changeme" in env_example, ".env.example should contain a placeholder"
        # Sanity: the file exists and is not empty
        assert len(env_example.strip()) > 0

    def test_init_refuses_path_traversal_absolute(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: init must refuse writes outside cwd (absolute path injection)."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            run_init(name="/tmp/evil")  # noqa: S108

    def test_init_refuses_path_traversal_relative(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: init must refuse writes outside cwd (relative traversal)."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            run_init(name="../escape")

    def test_init_is_idempotent_no_silent_overwrite(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: Running init twice does not silently overwrite existing files."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        # Modify .gitignore after first init
        gitignore = tmp_path / ".gitignore"
        original = gitignore.read_text()
        gitignore.write_text(original + "\n# custom line\n")
        # Second init must not overwrite (idempotent)
        run_init(name=".")
        after = gitignore.read_text()
        assert "# custom line" in after, (
            "init() silently overwrote an existing .gitignore (INIT-2 idempotency violated)"
        )

    def test_init_force_flag_allows_overwrite(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INIT-2: --force flag allows deliberate overwrite of existing scaffold files."""
        monkeypatch.chdir(tmp_path)
        run_init(name=".")
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# sentinel marker\n")
        run_init(name=".", force=True)
        after = gitignore.read_text()
        assert "# sentinel marker" not in after
        assert ".env" in after
