"""Migration history ledger: append-only record of applied migrations.

The ledger table (``ferrum_migrations``) is append-only. Rows are never updated
or deleted by Ferrum tooling. Each row records the plan digest, timestamp, and
environment — never bound values or credentials (CRED-1).
"""

from __future__ import annotations

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


async def ensure_ledger() -> None:
    """Create the ledger table if it does not exist."""
    raise NotImplementedError("ensure_ledger() implementation pending connection layer")


async def record_applied(
    digest: str,
    *,
    environment: str = "development",
    description: str = "",
) -> None:
    """Append a record for an applied migration.

    Raises if the digest has already been recorded (token-replay guard, MIG-8).
    """
    raise NotImplementedError("record_applied() implementation pending connection layer")


async def is_applied(digest: str) -> bool:
    """Return True if a migration with this digest has already been applied."""
    raise NotImplementedError("is_applied() implementation pending connection layer")
