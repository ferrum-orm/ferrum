"""Database driver abstraction for Ferrum."""

from __future__ import annotations

from urllib.parse import urlparse

from ferrum.drivers.protocol import DriverProtocol, RowProtocol
from ferrum.errors import FerrumConfigError

__all__ = ["DriverProtocol", "RowProtocol", "get_driver_for_dsn"]


def get_driver_for_dsn(dsn: str, **kwargs: object) -> DriverProtocol:
    """Return a driver instance for the DSN scheme.

    Each backend is imported lazily; missing optional deps raise
    ``FerrumConfigError`` with an install hint.
    """
    scheme = urlparse(dsn).scheme.lower()
    if scheme in ("postgresql", "postgres"):
        try:
            from ferrum.drivers.postgres import AsyncpgDriver
        except ImportError as exc:
            raise FerrumConfigError(
                "PostgreSQL driver not installed. Install with: uv add 'ferrum-orm[pg]' "
                "[FERR-C001]"
            ) from exc
        return AsyncpgDriver(dsn, **kwargs)  # type: ignore[arg-type]
    if scheme in ("mysql", "mysql+asyncmy"):
        try:
            from ferrum.drivers.mysql import AsyncmyDriver
        except ImportError as exc:
            raise FerrumConfigError(
                "MySQL driver not installed. Install with: uv add 'ferrum-orm[mysql]' "
                "[FERR-C001]"
            ) from exc
        return AsyncmyDriver(dsn, **kwargs)  # type: ignore[arg-type]
    if scheme in ("sqlite", "sqlite+aiosqlite"):
        try:
            from ferrum.drivers.sqlite import AiosqliteDriver
        except ImportError as exc:
            raise FerrumConfigError(
                "SQLite driver not installed. Install with: uv add 'ferrum-orm[sqlite]' "
                "[FERR-C001]"
            ) from exc
        return AiosqliteDriver(dsn, **kwargs)  # type: ignore[arg-type]
    raise FerrumConfigError(
        f"Unknown database scheme {scheme!r}. Install ferrum-orm[pg] for PostgreSQL, "
        "ferrum-orm[mysql] for MySQL, or ferrum-orm[sqlite] for SQLite. [FERR-C001]"
    )
