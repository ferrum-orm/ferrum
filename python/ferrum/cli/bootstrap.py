"""CLI-only project bootstrap: dotenv loading and settings module import.

This module is called **once** at CLI entry (before any subcommand) and is never
imported by library code.  It is the only place where the CLI reads ``.env`` files
and auto-imports the application's settings/models module.

Separation of concerns
-----------------------
- ``ferrum.connect()`` stays env-var only — no dotenv, no auto-import (library code
  must not have side effects).
- This module handles the developer-facing ergonomics so that ``ferrum migrate``
  "just works" after ``cp .env.example .env``.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

from ferrum.config import FerrumConfig, find_project_root, load_config
from ferrum.errors import FerrumConfigError


def _bootstrap_project() -> None:
    """One-shot CLI bootstrap; called before any subcommand dispatches.

    Steps
    -----
    1. Locate the project root (walk up from cwd looking for ``ferrum.toml`` or
       ``pyproject.toml``).
    2. Load the dotenv file (``cfg.env_file``, default ``.env``) with
       ``override=False`` so that already-set env vars always win.  Silently skips
       if ``python-dotenv`` is not installed or the file does not exist.
    3. Import the settings module (auto-discover ``ferrum_conf.py`` if no explicit
       module is configured).  If a module is found, import it; if it exposes a
       ``configure()`` callable, call it.  If an explicit module path is given but
       import fails, raise :class:`~ferrum.errors.FerrumConfigError`.
    """
    root = find_project_root(Path.cwd())
    cfg = load_config(root)
    _load_dotenv(root / cfg.env_file)
    _import_settings(cfg, root)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_dotenv(env_path: Path) -> None:
    """Load *env_path* into ``os.environ`` with ``override=False``.

    Silently skips if:
    - ``python-dotenv`` is not installed (optional dependency).
    - The file does not exist (normal in production / CI environments).
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        return

    if not env_path.exists():
        return

    load_dotenv(env_path, override=False)


def _import_settings(cfg: FerrumConfig, project_root: Path) -> None:
    """Resolve and import the settings module.

    Discovery order
    ---------------
    1. ``FERRUM_SETTINGS`` environment variable.
    2. ``[ferrum].settings`` in ``ferrum.toml`` or ``pyproject.toml``.
    3. ``ferrum_conf.py`` in ``project_root`` (file-based autodiscovery via
       ``importlib``).

    If a module name is found via (1) or (2), failure to import it is a developer
    bug and raises :class:`~ferrum.errors.FerrumConfigError`.

    If nothing is found, skip silently (backwards-compatible).
    """
    module_name: str | None = None

    env_override = os.environ.get("FERRUM_SETTINGS")
    if env_override:
        module_name = env_override
    elif cfg.settings:
        module_name = cfg.settings

    if module_name is not None:
        _ensure_project_root_on_path(project_root)
        _import_by_name(module_name, explicit=True)
        return

    # Autodiscovery: look for ferrum_conf.py in the project root.
    candidate = project_root / "ferrum_conf.py"
    if candidate.exists():
        _import_from_file("ferrum_conf", candidate, explicit=False)


def _ensure_project_root_on_path(project_root: Path) -> None:
    """Prepend *project_root* to ``sys.path`` so local settings modules resolve."""
    root_str = str(project_root.resolve())
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _import_by_name(module_name: str, *, explicit: bool) -> None:
    """Import *module_name* via the standard import system."""
    try:
        mod = importlib.import_module(module_name)
    except ImportError as exc:
        if explicit:
            raise FerrumConfigError(
                f"Cannot import settings module {module_name!r}: {exc}. "
                "Check FERRUM_SETTINGS or [ferrum].settings in ferrum.toml / pyproject.toml. "
                "[FERR-C001]"
            ) from exc
        return
    _call_configure(mod)


def _import_from_file(name: str, path: Path, *, explicit: bool) -> None:
    """Import a settings module from an explicit *path* on disk."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        if explicit:
            raise FerrumConfigError(
                f"Cannot load settings file {path}: invalid module spec. [FERR-C001]"
            )
        return
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        del sys.modules[name]
        if explicit:
            raise FerrumConfigError(
                f"Error while loading settings file {path}: {exc}. [FERR-C001]"
            ) from exc
        return
    _call_configure(mod)


def _call_configure(mod: object) -> None:
    """Call ``mod.configure()`` if it is a callable, otherwise skip."""
    configure = getattr(mod, "configure", None)
    if callable(configure):
        configure()
