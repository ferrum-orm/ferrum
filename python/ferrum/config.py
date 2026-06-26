"""Ferrum project configuration: ``ferrum.toml`` / ``pyproject.toml`` parser and discovery.

This module is deliberately **import-free** of all other ``ferrum.*`` sub-packages
(``cli``, ``queryset``, ``connection``, ``migrations``) so it can be safely imported
by any layer without creating dependency cycles.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tomllib
from pathlib import Path

DEFAULT_DATABASE_URL_ENV = "FERRUM_DATABASE_URL"
FALLBACK_DATABASE_URL_ENV = "DATABASE_URL"

WIRE_FORMAT_ENV = "FERRUM_WIRE_FORMAT"
DEFAULT_WIRE_FORMAT = "json"
_VALID_WIRE_FORMATS: frozenset[str] = frozenset({"json", "msgpack"})


@dataclasses.dataclass(frozen=True)
class FerrumConfig:
    """Resolved Ferrum project configuration.

    All fields have safe defaults so the absence of project config never breaks
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

    database_url_env: str | None = None
    """Environment variable holding the database URL.

    When ``None`` or empty, :func:`resolve_database_url` tries
    ``FERRUM_DATABASE_URL`` then ``DATABASE_URL``."""

    wire_format: str = DEFAULT_WIRE_FORMAT
    """Serialization for the Python↔Rust IR/hydration boundary: ``"json"``
    (default) or ``"msgpack"``. The ``FERRUM_WIRE_FORMAT`` environment variable
    overrides this value. ``msgpack`` additionally requires ``ferrum-orm[msgpack]``."""


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
    """Read Ferrum config from ``root`` and return a :class:`FerrumConfig`.

    Lookup order:

    1. ``root/ferrum.toml`` — dedicated Ferrum config file (preferred).
    2. ``root/pyproject.toml`` — ``[ferrum]`` table when ``ferrum.toml`` is absent.

    Falls back to all-defaults when neither file exists or neither defines ``[ferrum]``.
    On malformed TOML prints a warning to ``stderr`` and returns defaults — the CLI
    should never crash because of a bad config file.

    Args:
        root: Project root directory (typically returned by :func:`find_project_root`).

    Returns:
        A :class:`FerrumConfig` populated from the ``[ferrum]`` table, with defaults
        for any absent key.
    """
    ferrum_toml = root / "ferrum.toml"
    if ferrum_toml.exists():
        return _load_config_from_file(ferrum_toml)

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        return _load_config_from_file(pyproject)

    return FerrumConfig()


def _load_config_from_file(config_path: Path) -> FerrumConfig:
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        print(
            f"Warning: {config_path.name} is malformed ({exc}); using defaults.",
            file=sys.stderr,
        )
        return FerrumConfig()

    section = data.get("ferrum", {})
    if not isinstance(section, dict):
        print(
            f"Warning: [ferrum] section in {config_path.name} is not a table; using defaults.",
            file=sys.stderr,
        )
        return FerrumConfig()

    return _config_from_section(section)


def _config_from_section(section: dict[str, object]) -> FerrumConfig:
    raw_database_url_env = section.get("database_url_env")
    database_url_env: str | None
    if raw_database_url_env is None:
        database_url_env = None
    else:
        stripped = str(raw_database_url_env).strip()
        database_url_env = stripped or None

    raw_wire_format = section.get("wire_format")
    wire_format = (
        _str_with_default(raw_wire_format, DEFAULT_WIRE_FORMAT).strip().lower()
        if raw_wire_format is not None
        else DEFAULT_WIRE_FORMAT
    )
    if wire_format not in _VALID_WIRE_FORMATS:
        print(
            f"Warning: [ferrum] wire_format={wire_format!r} is invalid "
            f"(expected one of {', '.join(sorted(_VALID_WIRE_FORMATS))}); using "
            f"{DEFAULT_WIRE_FORMAT!r}.",
            file=sys.stderr,
        )
        wire_format = DEFAULT_WIRE_FORMAT

    return FerrumConfig(
        settings=_optional_str(section.get("settings")),
        migrations_dir=_str_with_default(section.get("migrations_dir"), "migrations"),
        default_env=_str_with_default(section.get("default_env"), "development"),
        env_file=_str_with_default(section.get("env_file"), ".env"),
        database_url_env=database_url_env,
        wire_format=wire_format,
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _str_with_default(value: object, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _env_get(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value


def resolve_database_url(*, database_url_env: str | None = None) -> str | None:
    """Resolve a database URL from environment variables.

    When *database_url_env* is set (non-empty), only that variable is read.
    Otherwise tries ``FERRUM_DATABASE_URL``, then ``DATABASE_URL``.
    """
    custom = (database_url_env or "").strip()
    if custom:
        return _env_get(custom)
    dsn = _env_get(DEFAULT_DATABASE_URL_ENV)
    if dsn is not None:
        return dsn
    return _env_get(FALLBACK_DATABASE_URL_ENV)


def database_url_env_hint(*, database_url_env: str | None = None) -> str:
    """Human-readable list of env vars consulted for a missing database URL."""
    custom = (database_url_env or "").strip()
    if custom:
        return custom
    return f"{DEFAULT_DATABASE_URL_ENV} (or {FALLBACK_DATABASE_URL_ENV})"


def resolve_database_url_for_cwd() -> tuple[str | None, str | None]:
    """Load project config from the cwd project root and resolve the database URL.

    Returns:
        ``(dsn, database_url_env)`` where *database_url_env* is the configured
        env var name from project config (``None`` when using the default chain).
    """
    root = find_project_root(Path.cwd())
    cfg = load_config(root)
    return resolve_database_url(database_url_env=cfg.database_url_env), cfg.database_url_env


def resolve_wire_format() -> str:
    """Resolve the IR/hydration wire format: env var first, then project config.

    Precedence: ``FERRUM_WIRE_FORMAT`` (when set to a valid value), then the
    ``[ferrum] wire_format`` config key, defaulting to ``"json"``. An unknown
    env value falls back to ``"json"`` rather than raising — selection is a
    performance knob, not a correctness gate.
    """
    env_value = _env_get(WIRE_FORMAT_ENV)
    if env_value is not None:
        normalized = env_value.strip().lower()
        if normalized in _VALID_WIRE_FORMATS:
            return normalized
        print(
            f"Warning: {WIRE_FORMAT_ENV}={env_value!r} is invalid "
            f"(expected one of {', '.join(sorted(_VALID_WIRE_FORMATS))}); using "
            f"{DEFAULT_WIRE_FORMAT!r}.",
            file=sys.stderr,
        )
        return DEFAULT_WIRE_FORMAT
    root = find_project_root(Path.cwd())
    return load_config(root).wire_format
