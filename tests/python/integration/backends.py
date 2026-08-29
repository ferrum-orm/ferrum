"""Backend registry for the multi-driver integration suite.

This is the single source of truth for which DSN env var activates each
backend and which Ferrum capabilities that backend exposes.  Everything in
``conftest.py``, ``schema.py``, and per-backend test modules derives from here.

Two directions are tested:

* **Positive** — a capability a backend claims must work end-to-end on a live
  server.
* **Negative** — a capability the backend does NOT claim must raise a typed
  Ferrum error *before* invalid SQL reaches the server (asserted in the
  future ``test_capability_gates.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Capability(StrEnum):
    """Feature flags for behaviours Ferrum may support per backend."""

    TRANSACTIONS = "transactions"
    SAVEPOINTS = "savepoints"
    STREAMING = "streaming"
    BULK_UPDATE = "bulk_update"
    AGGREGATES = "aggregates"
    UPSERT = "upsert"
    RETURNING = "returning"
    RLS = "rls"
    PGVECTOR = "pgvector"
    FTS = "fts"
    JSON_OPS = "json_ops"
    ILIKE = "ilike"
    ARRAYS = "arrays"
    ALTER_COLUMN = "alter_column"
    COMPOSITE_PK = "composite_pk"
    CALL_FUNCTION = "call_function"


@dataclass(frozen=True)
class Backend:
    """Descriptor for one Ferrum-supported database backend."""

    name: str
    """Short identifier: ``"postgres"`` | ``"mysql"`` | ``"sqlite"`` | ``"mssql"``."""

    dsn_env: str
    """Environment variable that supplies the DSN for this backend."""

    capabilities: frozenset[Capability]
    """Ferrum capabilities confirmed to work end-to-end on this backend.

    The set is conservative: only capabilities with existing tests or a clear
    implementation path in the Ferrum emitter are listed.
    """

    types: Mapping[str, str]
    """Portable type-key to DDL fragment mapping used by ``schema.py``.

    Keys: ``"pk_serial"``, ``"text"``, ``"int"``, ``"bool"``.
    """

    quote: Callable[[str], str]
    """Wrap an identifier in the backend's quoting convention."""


# ---------------------------------------------------------------------------
# Identifier-quoting helpers
# ---------------------------------------------------------------------------


def _dq(name: str) -> str:
    """ANSI double-quote (PostgreSQL, SQLite, MSSQL standard)."""
    return f'"{name}"'


def _bt(name: str) -> str:
    """Backtick (MySQL)."""
    return f"`{name}`"


def _br(name: str) -> str:
    """Bracket (SQL Server legacy style)."""
    return f"[{name}]"


# ---------------------------------------------------------------------------
# Capability sets
# ---------------------------------------------------------------------------

_PG_CAPS: Final = frozenset(
    [
        Capability.TRANSACTIONS,
        Capability.SAVEPOINTS,
        Capability.STREAMING,
        Capability.BULK_UPDATE,
        Capability.AGGREGATES,
        Capability.UPSERT,
        Capability.RETURNING,
        Capability.RLS,
        Capability.PGVECTOR,
        Capability.FTS,
        Capability.JSON_OPS,
        Capability.ILIKE,
        Capability.ARRAYS,
        Capability.ALTER_COLUMN,
        Capability.COMPOSITE_PK,
        Capability.CALL_FUNCTION,
    ]
)

# MySQL: FTS via FULLTEXT indexes; composite PKs via standard SQL.
# TRANSACTIONS / SAVEPOINTS: Python raises at runtime —
#   "MySQL, SQLite, and MSSQL backends are thin-parity and omit them"
#   (connection.py Connection.transaction(), [FERR-C001]).
# AGGREGATES: Rust emitter hard-gates group_by/aggregate to Postgres only
#   (emit.rs emit_aggregate_select: "aggregate queries currently require the
#   PostgreSQL dialect").
# All remaining capabilities are Postgres-specific.
_MYSQL_CAPS: Final = frozenset(
    [
        Capability.FTS,
        Capability.COMPOSITE_PK,
    ]
)

# SQLite: RETURNING clause (SQLite ≥ 3.35, proven by dialect.supports_returning()
# in dialect.rs); FTS5 via virtual tables; composite PKs via standard SQL.
# TRANSACTIONS / SAVEPOINTS: Python raises at runtime — same thin-parity error
#   as MySQL (connection.py Connection.transaction(), [FERR-C001]).
# AGGREGATES: Rust emitter hard-gates to Postgres only (emit.rs).
# All remaining capabilities are Postgres-specific.
_SQLITE_CAPS: Final = frozenset(
    [
        Capability.RETURNING,
        Capability.FTS,
        Capability.COMPOSITE_PK,
    ]
)

# MSSQL: FTS via CONTAINS; composite PKs via standard SQL.
# TRANSACTIONS / SAVEPOINTS: Python raises at runtime — same thin-parity error
#   as MySQL/SQLite (connection.py Connection.transaction(), [FERR-C001]).
# AGGREGATES: Rust emitter hard-gates to Postgres only (emit.rs).
# ALTER_COLUMN: listed in _MSSQL_UNSUPPORTED_KINDS (orchestrator.py).
# CALL_FUNCTION: uses PostgreSQL $N placeholders on Transaction; Postgres only.
# RETURNING: OUTPUT INSERTED.* is emitted by dialect.uses_output_returning() but
#   no positive live driver round-trip test exists yet; omitted until covered.
# All remaining capabilities are Postgres-specific.
_MSSQL_CAPS: Final = frozenset(
    [
        Capability.FTS,
        Capability.COMPOSITE_PK,
    ]
)

# ---------------------------------------------------------------------------
# Backend singletons
# ---------------------------------------------------------------------------

POSTGRES: Final = Backend(
    name="postgres",
    dsn_env="FERRUM_TEST_DSN",
    capabilities=_PG_CAPS,
    types={
        "pk_serial": "SERIAL PRIMARY KEY",
        "text": "TEXT",
        "int": "INT",
        "bool": "BOOLEAN",
    },
    quote=_dq,
)

MYSQL: Final = Backend(
    name="mysql",
    dsn_env="FERRUM_TEST_MYSQL_DSN",
    capabilities=_MYSQL_CAPS,
    types={
        "pk_serial": "INT NOT NULL AUTO_INCREMENT PRIMARY KEY",
        "text": "TEXT",
        "int": "INT",
        "bool": "TINYINT(1)",
    },
    quote=_bt,
)

SQLITE: Final = Backend(
    name="sqlite",
    dsn_env="FERRUM_TEST_SQLITE_DSN",
    capabilities=_SQLITE_CAPS,
    types={
        "pk_serial": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "text": "TEXT",
        "int": "INTEGER",
        "bool": "INTEGER",
    },
    quote=_dq,
)

MSSQL: Final = Backend(
    name="mssql",
    dsn_env="FERRUM_TEST_MSSQL_DSN",
    capabilities=_MSSQL_CAPS,
    types={
        "pk_serial": "INT IDENTITY(1,1) PRIMARY KEY",
        "text": "NVARCHAR(MAX)",
        "int": "INT",
        "bool": "BIT",
    },
    quote=_br,
)

#: All backends in canonical order.  ``conftest.py`` iterates this to discover
#: which ones are active for the current test run.
ALL_BACKENDS: Final = (POSTGRES, MYSQL, SQLITE, MSSQL)
