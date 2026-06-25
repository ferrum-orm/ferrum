"""Unit tests for ferrum.config: find_project_root, load_config."""

from __future__ import annotations

import os
import pathlib
import textwrap
from unittest.mock import patch

import pytest

from ferrum.config import (
    FerrumConfig,
    database_url_env_hint,
    find_project_root,
    load_config,
    resolve_database_url,
)


class TestFindProjectRoot:
    def test_finds_ferrum_toml_in_start_dir(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "ferrum.toml").write_text("[ferrum]\n")
        assert find_project_root(tmp_path) == tmp_path

    def test_finds_ferrum_toml_walking_up(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path
        (root / "ferrum.toml").write_text("[ferrum]\n")
        nested = root / "sub" / "deep"
        nested.mkdir(parents=True)
        assert find_project_root(nested) == root

    def test_falls_back_to_pyproject_toml(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        nested = tmp_path / "src"
        nested.mkdir()
        assert find_project_root(nested) == tmp_path

    def test_ferrum_toml_preferred_over_pyproject(self, tmp_path: pathlib.Path) -> None:
        """ferrum.toml in a child dir should be found before pyproject.toml higher up."""
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        child = tmp_path / "child"
        child.mkdir()
        (child / "ferrum.toml").write_text("[ferrum]\n")
        assert find_project_root(child) == child

    def test_returns_start_when_no_marker_found(self, tmp_path: pathlib.Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        # Ensure none of the intermediate dirs have markers — we work in a fresh tmp.
        result = find_project_root(nested)
        # Should be the resolved start (or an ancestor that happens to have pyproject.toml,
        # but since we're in tmp_path that shouldn't happen — return start).
        assert result == nested.resolve()


class TestLoadConfig:
    def test_returns_defaults_when_file_absent(self, tmp_path: pathlib.Path) -> None:
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()
        assert cfg.migrations_dir == "migrations"
        assert cfg.default_env == "development"
        assert cfg.env_file == ".env"
        assert cfg.settings is None

    def test_parses_all_keys(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "ferrum.toml").write_text(
            textwrap.dedent("""\
                [ferrum]
                settings = "myapp.settings"
                migrations_dir = "db/migrations"
                default_env = "staging"
                env_file = ".env.staging"
                database_url_env = "DATABASE_URL"
            """)
        )
        cfg = load_config(tmp_path)
        assert cfg.settings == "myapp.settings"
        assert cfg.migrations_dir == "db/migrations"
        assert cfg.default_env == "staging"
        assert cfg.env_file == ".env.staging"
        assert cfg.database_url_env == "DATABASE_URL"

    def test_partial_override_keeps_defaults_for_missing_keys(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "ferrum.toml").write_text('[ferrum]\nmigrations_dir = "custom"\n')
        cfg = load_config(tmp_path)
        assert cfg.migrations_dir == "custom"
        assert cfg.default_env == "development"  # default preserved
        assert cfg.settings is None

    def test_returns_defaults_on_malformed_toml(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "ferrum.toml").write_text("this is not valid toml ][[\n")
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "ferrum.toml" in captured.err

    def test_returns_defaults_on_non_table_ferrum_section(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "ferrum.toml").write_text('ferrum = "not a table"\n')
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_empty_ferrum_section_returns_defaults(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "ferrum.toml").write_text("[ferrum]\n")
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()

    def test_toml_without_ferrum_section_returns_defaults(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "ferrum.toml").write_text("[tool.something]\nkey = 1\n")
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()

    def test_loads_from_pyproject_toml_when_ferrum_toml_absent(
        self, tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
                [project]
                name = "myapp"

                [ferrum]
                settings = "myapp.settings"
                migrations_dir = "db/migrations"
                default_env = "staging"
                env_file = ".env.staging"
                database_url_env = "DATABASE_URL"
            """)
        )
        cfg = load_config(tmp_path)
        assert cfg.settings == "myapp.settings"
        assert cfg.migrations_dir == "db/migrations"
        assert cfg.default_env == "staging"
        assert cfg.env_file == ".env.staging"
        assert cfg.database_url_env == "DATABASE_URL"

    def test_pyproject_without_ferrum_section_returns_defaults(
        self, tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()

    def test_ferrum_toml_takes_precedence_over_pyproject(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\n\n[ferrum]\nsettings = "from_pyproject"\n'
        )
        (tmp_path / "ferrum.toml").write_text('[ferrum]\nsettings = "from_ferrum"\n')
        cfg = load_config(tmp_path)
        assert cfg.settings == "from_ferrum"

    def test_malformed_pyproject_returns_defaults(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "pyproject.toml").write_text("not valid toml ][[\n")
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()
        captured = capsys.readouterr()
        assert "pyproject.toml" in captured.err

    def test_non_table_ferrum_section_in_pyproject_returns_defaults(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "pyproject.toml").write_text('ferrum = "not a table"\n')
        cfg = load_config(tmp_path)
        assert cfg == FerrumConfig()
        captured = capsys.readouterr()
        assert "pyproject.toml" in captured.err

    def test_empty_database_url_env_treated_as_default(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "ferrum.toml").write_text('[ferrum]\ndatabase_url_env = ""\n')
        cfg = load_config(tmp_path)
        assert cfg.database_url_env is None


class TestResolveDatabaseUrl:
    def test_prefers_ferrum_database_url(self) -> None:
        dsn = "postgresql://ferrum@localhost/db"
        with patch.dict(
            os.environ,
            {"FERRUM_DATABASE_URL": dsn, "DATABASE_URL": "postgresql://other/db"},
        ):
            assert resolve_database_url() == dsn

    def test_falls_back_to_database_url(self) -> None:
        dsn = "postgresql://fallback@localhost/db"
        env = {k: v for k, v in os.environ.items() if k != "FERRUM_DATABASE_URL"}
        env["DATABASE_URL"] = dsn
        with patch.dict(os.environ, env, clear=True):
            assert resolve_database_url() == dsn

    def test_custom_env_var_only(self) -> None:
        dsn = "postgresql://custom@localhost/db"
        with patch.dict(os.environ, {"MY_DB_URL": dsn}, clear=True):
            assert resolve_database_url(database_url_env="MY_DB_URL") == dsn

    def test_custom_env_var_does_not_fall_back(self) -> None:
        with patch.dict(
            os.environ,
            {"FERRUM_DATABASE_URL": "postgresql://ferrum/db", "DATABASE_URL": "postgresql://db/db"},
            clear=True,
        ):
            assert resolve_database_url(database_url_env="MY_DB_URL") is None

    def test_empty_env_values_are_ignored(self) -> None:
        with patch.dict(
            os.environ,
            {"FERRUM_DATABASE_URL": "", "DATABASE_URL": "postgresql://fallback/db"},
            clear=True,
        ):
            assert resolve_database_url() == "postgresql://fallback/db"

    def test_database_url_env_hint_default_chain(self) -> None:
        assert "FERRUM_DATABASE_URL" in database_url_env_hint()
        assert "DATABASE_URL" in database_url_env_hint()

    def test_database_url_env_hint_custom(self) -> None:
        assert database_url_env_hint(database_url_env="MY_DB_URL") == "MY_DB_URL"
