"""Database driver abstraction for Ferrum."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ferrum.drivers.protocol import DriverProtocol, RowProtocol
from ferrum.errors import FerrumConfigError

__all__ = ["DriverProtocol", "RowProtocol", "get_driver_for_dsn"]


def get_driver_for_dsn(dsn: str, **kwargs: Any) -> DriverProtocol:
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
                "PostgreSQL driver not installed. Install with: uv add 'ferrum-orm[pg]' [FERR-C001]"
            ) from exc
        return AsyncpgDriver(dsn, **kwargs)
    if scheme in ("mysql", "mysql+asyncmy"):
        try:
            from ferrum.drivers.mysql import AsyncmyDriver
        except ImportError as exc:
            raise FerrumConfigError(
                "MySQL driver not installed. Install with: uv add 'ferrum-orm[mysql]' [FERR-C001]"
            ) from exc
        return AsyncmyDriver(dsn, **kwargs)
    if scheme in ("sqlite", "sqlite+aiosqlite"):
        try:
            from ferrum.drivers.sqlite import AiosqliteDriver
        except ImportError as exc:
            raise FerrumConfigError(
                "SQLite driver not installed. Install with: uv add 'ferrum-orm[sqlite]' [FERR-C001]"
            ) from exc
        return AiosqliteDriver(dsn, **kwargs)
    if scheme in ("mssql", "sqlserver"):
        try:
            from ferrum.drivers.mssql import AioodbcDriver
        except ImportError as exc:
            raise FerrumConfigError(
                "MSSQL driver not installed. Install with: uv add 'ferrum-orm[mssql]' [FERR-C001]"
            ) from exc
        return AioodbcDriver(dsn, **kwargs)
    raise FerrumConfigError(
        f"Unknown database scheme {scheme!r}. Install ferrum-orm[pg] for PostgreSQL, "
        "ferrum-orm[mysql] for MySQL, ferrum-orm[sqlite] for SQLite, or "
        "ferrum-orm[mssql] for SQL Server. [FERR-C001]"
    )
