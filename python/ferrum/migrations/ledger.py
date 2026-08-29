"""Migration history ledger: append-only record of applied migrations.

W1-C additions: raw-connection variants (``is_applied_on_conn`` /
``record_applied_on_conn``) so the orchestrator can run the ledger check and
write inside one pinned transaction, and a fixed advisory-lock key
(``ADVISORY_LOCK_KEY_*``) used by ``orchestrator.apply()`` to serialize
concurrent migrators on one connection.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

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


# W1-C: protocol for a raw driver connection that supports execute/fetchrow.
# Used by the _on_conn ledger variants so the orchestrator can run the
# ledger check and write on the same pinned transaction connection.
@runtime_checkable
class _RawConn(Protocol):
    """Minimal interface for a raw asyncpg connection used by ledger helpers."""

    async def execute(self, sql: str, *args: object) -> None: ...
    async def fetchrow(self, sql: str, *args: object) -> Any | None: ...  # noqa: ANN401
    async def fetch(self, sql: str, *args: object) -> list[Any]: ...


LEDGER_TABLE = "ferrum_migrations"

# W1-C: advisory lock key for ``pg_advisory_xact_lock(int4, int4)``.
# Two 32-bit keys derived from the stable namespace ``b"ferrum.migrations"``;
# held inside the apply transaction so commit/rollback auto-releases it.
_ADVISORY_LOCK_NAMESPACE = b"ferrum.migrations"
_h = hashlib.sha256(_ADVISORY_LOCK_NAMESPACE).digest()
ADVISORY_LOCK_KEY_1: int = int.from_bytes(_h[0:4], "big")
ADVISORY_LOCK_KEY_2: int = int.from_bytes(_h[4:8], "big")


def advisory_lock_sql() -> str:
    """Return the ``pg_advisory_xact_lock`` statement used by ``apply()``.

    Uses two int32 keys so the lock is scoped to the Ferrum migration
    namespace and held for the life of the transaction (auto-released on
    commit/rollback). PostgreSQL only.
    """
    return "SELECT pg_advisory_xact_lock($1, $2)"


def _create_ledger_sql(dialect: str) -> str:
    if dialect == "mysql":
        return f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    digest      VARCHAR(255) NOT NULL UNIQUE,
    applied_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    environment VARCHAR(255) NOT NULL DEFAULT 'development',
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
    if dialect == "mssql":
        # NVARCHAR(MAX) cannot be UNIQUE/indexed in SQL Server, so digest and
        # environment use bounded NVARCHAR. CREATE TABLE IF NOT EXISTS is not
        # valid T-SQL — guard with an OBJECT_ID existence check instead.
        return f"""
IF OBJECT_ID(N'{LEDGER_TABLE}', N'U') IS NULL
CREATE TABLE {LEDGER_TABLE} (
    id          BIGINT IDENTITY(1,1) PRIMARY KEY,
    digest      NVARCHAR(255)    NOT NULL UNIQUE,
    applied_at  DATETIMEOFFSET   NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    environment NVARCHAR(255)    NOT NULL DEFAULT 'development',
    description NVARCHAR(MAX)
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
    if dialect in ("mysql", "sqlite", "mssql"):
        return f"INSERT INTO {LEDGER_TABLE} (digest, environment, description) VALUES (?, ?, ?)"
    return f"INSERT INTO {LEDGER_TABLE} (digest, environment, description) VALUES ($1, $2, $3)"


def _select_digest_sql(dialect: str) -> str:
    if dialect in ("mysql", "sqlite", "mssql"):
        return f"SELECT 1 FROM {LEDGER_TABLE} WHERE digest = ?"
    return f"SELECT 1 FROM {LEDGER_TABLE} WHERE digest = $1"


def _delete_digest_sql(dialect: str) -> str:
    if dialect in ("mysql", "sqlite", "mssql"):
        return f"DELETE FROM {LEDGER_TABLE} WHERE digest = ?"
    return f"DELETE FROM {LEDGER_TABLE} WHERE digest = $1"


def _select_digest_by_description_sql(dialect: str) -> str:
    if dialect in ("mysql", "sqlite", "mssql"):
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


# ---------------------------------------------------------------------------
# W1-C: raw-connection variants — run on a pinned transaction connection
# ---------------------------------------------------------------------------


async def is_applied_on_conn(
    raw_conn: _RawConn,
    digest: str,
    *,
    dialect: str = "postgres",
) -> bool:
    """Return True if *digest* is recorded on the pinned *raw_conn*.

    Used by ``orchestrator.apply()`` inside the advisory-locked transaction
    so the ledger check runs on the same connection that will write the
    ledger row — no race between check and mutate.
    """
    row = await raw_conn.fetchrow(_select_digest_sql(dialect), digest)
    return row is not None


async def record_applied_on_conn(
    raw_conn: _RawConn,
    digest: str,
    *,
    environment: str = "development",
    description: str = "",
    dialect: str = "postgres",
) -> None:
    """Append a ledger row on the pinned *raw_conn* (inside the transaction).

    A duplicate-digest unique violation maps to ``FerrumMigrationError`` so a
    concurrent second runner that slipped past the advisory lock still fails
    closed rather than silently double-applying.
    """
    try:
        await raw_conn.execute(
            _insert_ledger_sql(dialect),
            digest,
            environment,
            description,
        )
    except FerrumIntegrityError:
        raise FerrumMigrationError(
            f"Migration {description!r} has already been applied. [FERR-M003]"
        ) from None
    except Exception as exc:
        if (
            _HAS_ASYNCPG
            and _asyncpg_exc is not None
            and isinstance(exc, _asyncpg_exc.UniqueViolationError)
        ):
            raise FerrumMigrationError(
                f"Migration {description!r} has already been applied. [FERR-M003]"
            ) from None
        raise


async def find_applied_digest_by_name_on_conn(
    raw_conn: _RawConn,
    migration_name: str,
    *,
    dialect: str = "postgres",
) -> str | None:
    """Return the ledger digest for *migration_name* on a pinned connection."""
    row = await raw_conn.fetchrow(_select_digest_by_description_sql(dialect), migration_name)
    if row is None:
        return None
    if isinstance(row, dict):
        return str(row.get("digest", "")) or None
    return str(row[0]) if row[0] else None


async def verify_checksum_on_conn(
    raw_conn: _RawConn,
    migration_name: str,
    digest: str,
    *,
    dialect: str = "postgres",
) -> None:
    """Race-safe checksum check on a pinned connection (W1-C)."""
    stored = await find_applied_digest_by_name_on_conn(raw_conn, migration_name, dialect=dialect)
    if stored is not None and stored != digest:
        raise FerrumMigrationError(
            f"Migration {migration_name!r} checksum mismatch: the on-disk file "
            "does not match the version that was applied. "
            "Revert or restore the original file before migrating. [FERR-M005]"
        )


async def ensure_ledger_on_conn(raw_conn: _RawConn, *, dialect: str = "postgres") -> None:
    """Create the ledger table on a pinned connection (W1-C)."""
    await raw_conn.execute(_create_ledger_sql(dialect))
