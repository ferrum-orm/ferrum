"""Migration history ledger: append-only record of applied migrations."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

try:
    import asyncpg.exceptions as _asyncpg_exc  # type: ignore[import-untyped]

    _HAS_ASYNCPG: bool = True
except ImportError:
    _asyncpg_exc = None  # type: ignore
    _HAS_ASYNCPG = False

try:
    import asyncmy.errors as _asyncmy_exc  # type: ignore[import-untyped]

    _HAS_ASYNCMY: bool = True
except ImportError:
    _asyncmy_exc = None
    _HAS_ASYNCMY = False

try:
    import aiosqlite  # type: ignore[import-untyped]

    _HAS_AIOSQLITE: bool = True
except ImportError:
    aiosqlite = None
    _HAS_AIOSQLITE = False

from ferrum.errors import FerrumIntegrityError, FerrumMigrationError

if TYPE_CHECKING:
    from ferrum.connection import Connection

LEDGER_TABLE = "ferrum_migrations"


def _create_ledger_sql(dialect: str) -> str:
    if dialect == "mysql":
        return f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    digest      TEXT        NOT NULL UNIQUE,
    applied_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    environment TEXT        NOT NULL DEFAULT 'development',
    description TEXT
)
""".strip()
    if dialect == "sqlite":
        return f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    digest      TEXT        NOT NULL UNIQUE,
    applied_at  TEXT        NOT NULL DEFAULT (datetime('now')),
    environment TEXT        NOT NULL DEFAULT 'development',
    description TEXT
)
""".strip()
    return f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    id          BIGSERIAL PRIMARY KEY,
    digest      TEXT        NOT NULL UNIQUE,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    environment TEXT        NOT NULL DEFAULT 'development',
    description TEXT
)
""".strip()


def _insert_ledger_sql(dialect: str) -> str:
    if dialect in ("mysql", "sqlite"):
        return f"INSERT INTO {LEDGER_TABLE} (digest, environment, description) VALUES (?, ?, ?)"
    return f"INSERT INTO {LEDGER_TABLE} (digest, environment, description) VALUES ($1, $2, $3)"


def _select_digest_sql(dialect: str) -> str:
    if dialect in ("mysql", "sqlite"):
        return f"SELECT 1 FROM {LEDGER_TABLE} WHERE digest = ?"
    return f"SELECT 1 FROM {LEDGER_TABLE} WHERE digest = $1"


def _delete_digest_sql(dialect: str) -> str:
    if dialect in ("mysql", "sqlite"):
        return f"DELETE FROM {LEDGER_TABLE} WHERE digest = ?"
    return f"DELETE FROM {LEDGER_TABLE} WHERE digest = $1"


def _select_digest_by_description_sql(dialect: str) -> str:
    if dialect in ("mysql", "sqlite"):
        return f"SELECT digest FROM {LEDGER_TABLE} WHERE description = ?"
    return f"SELECT digest FROM {LEDGER_TABLE} WHERE description = $1"


def compute_digest(name: str, content: str) -> str:
    """Return a stable sha256 digest for a migration file."""
    return hashlib.sha256(f"{name}:{content}".encode()).hexdigest()


async def ensure_ledger(conn: Connection) -> None:
    """Create the ledger table if it does not exist."""
    driver = conn._require_driver()
    await driver.execute(_create_ledger_sql(conn.dialect))


async def record_applied(
    conn: Connection,
    digest: str,
    *,
    environment: str = "development",
    description: str = "",
) -> None:
    """Append a record for an applied migration."""
    driver = conn._require_driver()
    try:
        await driver.execute(
            _insert_ledger_sql(conn.dialect),
            digest,
            environment,
            description,
        )
    except FerrumIntegrityError:
        # Drivers map a duplicate-digest unique violation to FerrumIntegrityError.
        # The ledger's only unique column is `digest`, so this means a replay.
        raise FerrumMigrationError(
            f"Migration {description!r} has already been applied. [FERR-M003]"
        ) from None
    except Exception as exc:
        # Defensive fallback for raw driver integrity errors that bypass mapping.
        if (
            _HAS_ASYNCPG
            and _asyncpg_exc is not None
            and isinstance(exc, _asyncpg_exc.UniqueViolationError)
        ):
            raise FerrumMigrationError(
                f"Migration {description!r} has already been applied. [FERR-M003]"
            ) from None
        integrity_cls = getattr(_asyncmy_exc, "IntegrityError", None) if _HAS_ASYNCMY else None
        if integrity_cls is not None and isinstance(exc, integrity_cls):
            raise FerrumMigrationError(
                f"Migration {description!r} has already been applied. [FERR-M003]"
            ) from None
        if _HAS_AIOSQLITE and aiosqlite is not None and isinstance(exc, aiosqlite.IntegrityError):
            raise FerrumMigrationError(
                f"Migration {description!r} has already been applied. [FERR-M003]"
            ) from None
        raise


async def find_applied_digest_by_name(conn: Connection, migration_name: str) -> str | None:
    """Return the ledger digest recorded for *migration_name*, if any."""
    driver = conn._require_driver()
    row = await driver.fetchrow(
        _select_digest_by_description_sql(conn.dialect),
        migration_name,
    )
    if row is None:
        return None
    if isinstance(row, dict):
        return str(row.get("digest", "")) or None
    return str(row[0]) if row[0] else None


async def verify_checksum(conn: Connection, migration_name: str, digest: str) -> None:
    """Raise ``FerrumMigrationError`` when an applied migration file was edited."""
    stored = await find_applied_digest_by_name(conn, migration_name)
    if stored is not None and stored != digest:
        raise FerrumMigrationError(
            f"Migration {migration_name!r} checksum mismatch: the on-disk file "
            "does not match the version that was applied. "
            "Revert or restore the original file before migrating. [FERR-M005]"
        )


async def is_applied(conn: Connection, digest: str) -> bool:
    """Return True if a migration with this digest has already been applied."""
    driver = conn._require_driver()
    row = await driver.fetchrow(_select_digest_sql(conn.dialect), digest)
    return row is not None


async def delete_applied(conn: Connection, digest: str) -> None:
    """Remove a migration record from the ledger (used by revert only)."""
    driver = conn._require_driver()
    await driver.execute(_delete_digest_sql(conn.dialect), digest)
