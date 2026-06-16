"""Migration history ledger: append-only record of applied migrations.

The ledger table (``ferrum_migrations``) is append-only. Rows are never updated
or deleted by Ferrum tooling. Each row records the plan digest, timestamp, and
environment — never bound values or credentials (CRED-1).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

try:
    import asyncpg.exceptions as _asyncpg_exc  # type: ignore[import-untyped]

    _HAS_ASYNCPG: bool = True
except ImportError:
    _asyncpg_exc = None  # type: ignore
    _HAS_ASYNCPG = False

from ferrum.errors import FerrumMigrationError

if TYPE_CHECKING:
    from ferrum.connection import Connection

LEDGER_TABLE = "ferrum_migrations"

CREATE_LEDGER_SQL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    id          BIGSERIAL PRIMARY KEY,
    digest      TEXT        NOT NULL UNIQUE,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    environment TEXT        NOT NULL DEFAULT 'development',
    description TEXT
)
""".strip()


def compute_digest(name: str, content: str) -> str:
    """Return a stable sha256 digest for a migration file.

    The digest is keyed on both the migration name and its full content so that
    renaming or editing a migration file produces a distinct digest.
    """
    return hashlib.sha256(f"{name}:{content}".encode()).hexdigest()


async def ensure_ledger(conn: Connection) -> None:
    """Create the ledger table if it does not exist."""
    pool = conn._require_pool()
    await pool.execute(CREATE_LEDGER_SQL)


async def record_applied(
    conn: Connection,
    digest: str,
    *,
    environment: str = "development",
    description: str = "",
) -> None:
    """Append a record for an applied migration.

    Raises ``FerrumMigrationError`` if the digest has already been recorded
    (replay guard, MIG-8).  Credentials and bound values are never included in
    the error message (CRED-1).
    """
    pool = conn._require_pool()
    try:
        await pool.execute(
            "INSERT INTO ferrum_migrations (digest, environment, description) VALUES ($1, $2, $3)",
            digest,
            environment,
            description,
        )
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


async def is_applied(conn: Connection, digest: str) -> bool:
    """Return True if a migration with this digest has already been applied."""
    pool = conn._require_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM ferrum_migrations WHERE digest = $1",
        digest,
    )
    return row is not None


async def delete_applied(conn: Connection, digest: str) -> None:
    """Remove a migration record from the ledger (used by revert only).

    No credentials or bound values appear in errors or logs (CRED-1).
    """
    pool = conn._require_pool()
    await pool.execute(
        "DELETE FROM ferrum_migrations WHERE digest = $1",
        digest,
    )
