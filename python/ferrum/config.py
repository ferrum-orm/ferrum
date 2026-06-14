"""Ferrum project configuration: ``ferrum.toml`` parser and project-root discovery.

This module is deliberately **import-free** of all other ``ferrum.*`` sub-packages
(``cli``, ``queryset``, ``connection``, ``migrations``) so it can be safely imported
by any layer without creating dependency cycles.
"""

from __future__ import annotations

import dataclasses
import sys
import tomllib
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class FerrumConfig:
    """Resolved Ferrum project configuration.

    All fields have safe defaults so the absence of ``ferrum.toml`` never breaks
    library operation.  Secrets (DSNs, passwords) must never be placed here — use
    the ``.env`` file or environment variables.
    """

    settings: str | None = None
    """Python module that imports the application models.  Enables ``makemigrations``
    auto-discovery.  Examples: ``"ferrum_conf"``, ``"myapp.settings"``."""

    migrations_dir: str = "migrations"
    """Directory (relative to project root) where migration files are stored."""

    default_env: str = "development"
    """Default environment name used by ``ferrum migrate``."""

    env_file: str = ".env"
    """Path (relative to project root) of the dotenv file loaded by the CLI."""


def find_project_root(start: Path) -> Path:
    """Walk up from *start* looking for ``ferrum.toml`` or ``pyproject.toml``.

    Stops at the filesystem root.  Returns *start* unchanged if no marker is found
    (backwards-compatible: an unmarked directory tree is a valid Ferrum project).

    Args:
        start: Directory to begin the search.  Typically ``Path.cwd()``.

    Returns:
        The first ancestor directory (inclusive) that contains ``ferrum.toml`` or
        ``pyproject.toml``, or *start* if neither is found.
    """
    current = start.resolve()
    while True:
        if (current / "ferrum.toml").exists() or (current / "pyproject.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            # Filesystem root — give up and return the original start directory.
            return start.resolve()
        current = parent


def load_config(root: Path) -> FerrumConfig:
    """Read ``root/ferrum.toml`` and return a :class:`FerrumConfig`.

    Falls back to all-defaults when the file is absent.  On malformed TOML prints a
    warning to ``stderr`` and returns defaults — the CLI should never crash because of
    a bad config file.

    Args:
        root: Project root directory (typically returned by :func:`find_project_root`).

    Returns:
        A :class:`FerrumConfig` populated from the ``[ferrum]`` table, with defaults
        for any absent key.
    """
    config_path = root / "ferrum.toml"
    if not config_path.exists():
        return FerrumConfig()

    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        print(
            f"Warning: ferrum.toml is malformed ({exc}); using defaults.",
            file=sys.stderr,
        )
        return FerrumConfig()

    section = data.get("ferrum", {})
    if not isinstance(section, dict):
        print(
            "Warning: [ferrum] section in ferrum.toml is not a table; using defaults.",
            file=sys.stderr,
        )
        return FerrumConfig()

    return FerrumConfig(
        settings=section.get("settings", None),
        migrations_dir=section.get("migrations_dir", "migrations"),
        default_env=section.get("default_env", "development"),
        env_file=section.get("env_file", ".env"),
    )
