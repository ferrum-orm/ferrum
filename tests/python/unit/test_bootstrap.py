"""Unit tests for ferrum.cli.bootstrap: _bootstrap_project."""

from __future__ import annotations

import pathlib
import sys
import textwrap
import types

import pytest

from ferrum.errors import FerrumConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_bootstrap(monkeypatch: pytest.MonkeyPatch, cwd: pathlib.Path) -> None:
    """Run _bootstrap_project with cwd patched to *cwd*."""
    monkeypatch.chdir(cwd)
    # Re-import to get a fresh call (module is already loaded, just call the fn)
    from ferrum.cli.bootstrap import _bootstrap_project

    _bootstrap_project()


# ---------------------------------------------------------------------------
# Dotenv behaviour
# ---------------------------------------------------------------------------


class TestDotenvLoading:
    def test_silently_skips_when_dotenv_not_installed(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If python-dotenv is not installed, _bootstrap_project must not raise."""
        # Make dotenv unimportable for this test
        monkeypatch.setitem(sys.modules, "dotenv", None)  # type: ignore[assignment]
        (tmp_path / ".env").write_text("FERRUM_DATABASE_URL=postgresql://test\n")
        _run_bootstrap(monkeypatch, tmp_path)  # should not raise

    def test_calls_load_dotenv_with_correct_path_and_no_override(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_dotenv is called with the env_file path and override=False."""
        calls: list[tuple] = []

        def fake_load_dotenv(path: pathlib.Path, *, override: bool) -> None:
            calls.append((path, override))

        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = fake_load_dotenv  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

        env_file = tmp_path / ".env"
        env_file.write_text("X=1\n")
        _run_bootstrap(monkeypatch, tmp_path)

        assert len(calls) == 1
        assert calls[0][0] == env_file
        assert calls[0][1] is False  # override=False

    def test_skips_load_dotenv_when_env_file_missing(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the .env file does not exist, load_dotenv should NOT be called."""
        calls: list[tuple] = []

        def fake_load_dotenv(path: pathlib.Path, *, override: bool) -> None:
            calls.append((path, override))

        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = fake_load_dotenv  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

        # No .env file in tmp_path
        _run_bootstrap(monkeypatch, tmp_path)
        assert calls == []


# ---------------------------------------------------------------------------
# Settings module import
# ---------------------------------------------------------------------------


class TestSettingsImport:
    def test_imports_module_from_ferrum_settings_env_var(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FERRUM_SETTINGS env var is respected."""
        # Create a simple settings module on sys.path
        settings_file = tmp_path / "my_settings.py"
        settings_file.write_text("IMPORTED = True\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv("FERRUM_SETTINGS", "my_settings")
        # Remove from cache if present
        sys.modules.pop("my_settings", None)

        _run_bootstrap(monkeypatch, tmp_path)

        assert "my_settings" in sys.modules
        assert sys.modules["my_settings"].IMPORTED is True  # type: ignore[attr-defined]

    def test_autodiscovers_ferrum_conf_in_project_root(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ferrum_conf.py in project root is autodiscovered and imported."""
        conf_file = tmp_path / "ferrum_conf.py"
        conf_file.write_text("AUTODISCOVERED = True\n")
        sys.modules.pop("ferrum_conf", None)

        _run_bootstrap(monkeypatch, tmp_path)

        assert "ferrum_conf" in sys.modules
        assert sys.modules["ferrum_conf"].AUTODISCOVERED is True  # type: ignore[attr-defined]

    def test_calls_configure_if_defined(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the settings module defines configure(), it is called."""
        conf_file = tmp_path / "ferrum_conf.py"
        conf_file.write_text(
            textwrap.dedent("""\
                _called = False
                def configure():
                    global _called
                    _called = True
            """)
        )
        sys.modules.pop("ferrum_conf", None)

        _run_bootstrap(monkeypatch, tmp_path)

        assert sys.modules["ferrum_conf"]._called is True  # type: ignore[attr-defined]

    def test_skips_silently_when_no_settings_found(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ferrum_conf.py, no FERRUM_SETTINGS, no ferrum.toml — silently skip."""
        monkeypatch.delenv("FERRUM_SETTINGS", raising=False)
        _run_bootstrap(monkeypatch, tmp_path)  # must not raise

    def test_import_failure_of_explicit_module_raises_config_error(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Specifying a non-existent module via FERRUM_SETTINGS raises FerrumConfigError."""
        monkeypatch.setenv("FERRUM_SETTINGS", "no_such_module_xyz_abc")
        sys.modules.pop("no_such_module_xyz_abc", None)

        with pytest.raises(FerrumConfigError, match="FERR-C001"):
            _run_bootstrap(monkeypatch, tmp_path)

    def test_ferrum_toml_settings_takes_effect(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[ferrum].settings in ferrum.toml is imported when FERRUM_SETTINGS is absent."""
        settings_file = tmp_path / "toml_settings.py"
        settings_file.write_text("FROM_TOML = True\n")
        (tmp_path / "ferrum.toml").write_text('[ferrum]\nsettings = "toml_settings"\n')
        monkeypatch.delenv("FERRUM_SETTINGS", raising=False)
        sys.modules.pop("toml_settings", None)

        _run_bootstrap(monkeypatch, tmp_path)

        assert "toml_settings" in sys.modules
        assert sys.modules["toml_settings"].FROM_TOML is True  # type: ignore[attr-defined]

    def test_pyproject_toml_settings_takes_effect(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[ferrum].settings in pyproject.toml is imported when ferrum.toml is absent."""
        settings_file = tmp_path / "pyproject_settings.py"
        settings_file.write_text("FROM_PYPROJECT = True\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\n\n[ferrum]\nsettings = "pyproject_settings"\n'
        )
        monkeypatch.delenv("FERRUM_SETTINGS", raising=False)
        sys.modules.pop("pyproject_settings", None)

        _run_bootstrap(monkeypatch, tmp_path)

        assert "pyproject_settings" in sys.modules
        assert sys.modules["pyproject_settings"].FROM_PYPROJECT is True  # type: ignore[attr-defined]
