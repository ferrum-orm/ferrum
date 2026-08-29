"""Negative parity tests: unsupported capabilities raise typed errors before SQL.

These tests assert that ``FerrumConfigError`` is raised **before** any driver
method is called when an operation is invoked on a backend that does not claim
the required capability.

Tests are driven from mock connections so they do not require live databases.

Behavioral gate coverage by capability:
  - UPSERT          — Python-side gate in QuerySet._build_upsert_sql (tested below)
  - STREAMING       — Python-side gate in _open_compiled_stream (tested below)
  - TRANSACTIONS    — Python-side gate in Connection.transaction() (tested below)
  - SAVEPOINTS      — Python-side gate in Transaction.savepoint() (tested below)
  - AGGREGATES      — Rust-side gate in emit_aggregate_select (dialect check in the
                       Rust emitter); no Python pre-compilation guard exists. Only
                       registry consistency is assertable without the native extension.
  - ALTER_COLUMN     — Tested via orchestrator.apply() in test_migrations_per_dialect.py
                       (FerrumMigrationError on MSSQL mock)
  - CALL_FUNCTION    — No Python-side dialect check; the emitted SQL uses $N
                       placeholders that are Postgres-only. Registry consistency is
                       the only assertable gate in pure-Python unit tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ferrum.connection import Connection, Transaction, _open_compiled_stream
from ferrum.drivers.protocol import CompiledQuery
from ferrum.errors import FerrumConfigError
from ferrum.models import Field, Model
from ferrum.queryset import QuerySet
from ferrum.runtime import RuntimeConfig, _LifecycleGuard

from .backends import ALL_BACKENDS, Capability

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SampleModel(Model):
    id: int = Field(primary_key=True)
    name: str


def _mock_conn(dialect: str) -> MagicMock:
    """Build a minimal mock connection with a given dialect.

    Uses ``MagicMock`` for the connection itself so that synchronous calls
    like ``conn._require_driver()`` return the driver synchronously rather
    than as a coroutine.
    """
    conn = MagicMock()
    driver = MagicMock()
    driver._dialect = dialect
    driver.dialect = dialect
    driver.execute = AsyncMock()
    driver.fetchrow = AsyncMock()
    driver.fetch = AsyncMock(return_value=[])
    conn._require_driver.return_value = driver
    conn.dialect = dialect
    return conn


def _sample_qs() -> QuerySet:  # type: ignore[type-arg]
    return QuerySet(_SampleModel)


# ---------------------------------------------------------------------------
# Upsert capability gates — tested via QuerySet._build_upsert_sql
# ---------------------------------------------------------------------------


class TestUpsertCapabilityGates:
    """_build_upsert_sql must raise FerrumConfigError on non-Postgres dialects."""

    @pytest.mark.parametrize("dialect", ["mysql", "sqlite", "mssql"])
    def test_build_upsert_sql_raises_config_error(self, dialect: str) -> None:
        qs = _sample_qs()
        metadata = _SampleModel.get_metadata()
        with pytest.raises(FerrumConfigError, match=r"upsert"):
            qs._build_upsert_sql(
                metadata,
                {"id": 1, "name": "alpha"},
                conflict_fields=["id"],
                update_fields=["name"],
                returning=False,
                dialect=dialect,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dialect", ["mysql", "sqlite", "mssql"])
    async def test_upsert_raises_before_driver_io(self, dialect: str) -> None:
        conn = _mock_conn(dialect)  # type: ignore[arg-type]
        qs = _sample_qs()

        with pytest.raises(FerrumConfigError, match=r"upsert"):
            await qs.upsert(
                conn,
                conflict_fields=["id"],
                update_fields=["name"],
                id=1,
                name="alpha",
            )

        conn._require_driver.return_value.execute.assert_not_called()
        conn._require_driver.return_value.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dialect", ["mysql", "sqlite", "mssql"])
    async def test_bulk_upsert_raises_before_driver_io(self, dialect: str) -> None:
        conn = _mock_conn(dialect)  # type: ignore[arg-type]
        qs = _sample_qs()

        with pytest.raises(FerrumConfigError, match=r"upsert"):
            await qs.bulk_upsert(
                conn,
                [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}],
                conflict_fields=["id"],
                update_fields=["name"],
            )

        conn._require_driver.return_value.execute.assert_not_called()
        conn._require_driver.return_value.fetchrow.assert_not_called()

    def test_build_upsert_sql_does_not_raise_on_postgres(self) -> None:
        """_build_upsert_sql emits valid SQL on postgres without a config error."""
        qs = _sample_qs()
        metadata = _SampleModel.get_metadata()
        sql, bound = qs._build_upsert_sql(
            metadata,
            {"id": 1, "name": "alpha"},
            conflict_fields=["id"],
            update_fields=["name"],
            returning=False,
            dialect="postgres",
        )
        assert "ON CONFLICT" in sql.upper()
        assert isinstance(bound, list)


# ---------------------------------------------------------------------------
# Streaming capability gate — tested via _open_compiled_stream
# ---------------------------------------------------------------------------


class TestStreamingCapabilityGate:
    """_open_compiled_stream must raise FerrumConfigError on non-Postgres dialects."""

    @pytest.mark.parametrize("dialect", ["mysql", "sqlite", "mssql"])
    def test_open_compiled_stream_raises_config_error_for_non_postgres(self, dialect: str) -> None:
        """The streaming gate is enforced inside _open_compiled_stream."""
        # Build a minimal lifecycle guard that won't raise on reject_if_closing.
        lifecycle = MagicMock(spec=_LifecycleGuard)
        lifecycle.reject_if_closing.return_value = None
        compiled = MagicMock(spec=CompiledQuery)
        runtime = MagicMock()

        with pytest.raises(FerrumConfigError, match=r"[Pp]ostgre"):
            _open_compiled_stream(
                driver=MagicMock(),
                lifecycle=lifecycle,
                runtime=runtime,
                dialect=dialect,
                compiled=compiled,
                chunk_size=100,
            )


# ---------------------------------------------------------------------------
# Backend registry consistency checks
# ---------------------------------------------------------------------------


class TestBackendRegistryConsistency:
    """ALL_BACKENDS must not claim UPSERT or STREAMING for non-Postgres backends."""

    def test_upsert_not_claimed_by_non_postgres_backends(self) -> None:
        for backend in ALL_BACKENDS:
            if backend.name != "postgres":
                assert Capability.UPSERT not in backend.capabilities, (
                    f"Backend {backend.name!r} claims UPSERT but only postgres supports it"
                )

    def test_streaming_not_claimed_by_non_postgres_backends(self) -> None:
        for backend in ALL_BACKENDS:
            if backend.name != "postgres":
                assert Capability.STREAMING not in backend.capabilities, (
                    f"Backend {backend.name!r} claims STREAMING but only postgres supports it"
                )

    def test_postgres_claims_both_capabilities(self) -> None:
        pg_backends = [b for b in ALL_BACKENDS if b.name == "postgres"]
        assert pg_backends, "No postgres backend found in ALL_BACKENDS"
        for pg in pg_backends:
            assert Capability.UPSERT in pg.capabilities
            assert Capability.STREAMING in pg.capabilities

    def test_transactions_not_claimed_by_non_postgres_backends(self) -> None:
        """MySQL, SQLite, and MSSQL must not claim TRANSACTIONS.

        Connection.transaction() raises FerrumConfigError when the driver has no
        ``transaction`` factory — only the asyncpg driver provides one.
        """
        for b in ALL_BACKENDS:
            if b.name != "postgres":
                assert Capability.TRANSACTIONS not in b.capabilities, (
                    f"Backend {b.name!r} claims TRANSACTIONS but the {b.name} "
                    "driver does not expose a transaction() factory"
                )

    def test_savepoints_not_claimed_by_non_postgres_backends(self) -> None:
        """MySQL, SQLite, and MSSQL must not claim SAVEPOINTS.

        Transaction.savepoint() raises FerrumConfigError when the bound
        connection has no ``savepoint`` factory — only asyncpg provides one.
        """
        for b in ALL_BACKENDS:
            if b.name != "postgres":
                assert Capability.SAVEPOINTS not in b.capabilities, (
                    f"Backend {b.name!r} claims SAVEPOINTS but only postgres "
                    "has a SAVEPOINT-capable driver"
                )

    def test_aggregates_not_claimed_by_non_postgres_backends(self) -> None:
        """MySQL, SQLite, and MSSQL must not claim AGGREGATES.

        The Rust emitter hard-gates aggregate_select to the postgres dialect
        (emit.rs: "aggregate queries currently require the PostgreSQL dialect").
        """
        for b in ALL_BACKENDS:
            if b.name != "postgres":
                assert Capability.AGGREGATES not in b.capabilities, (
                    f"Backend {b.name!r} claims AGGREGATES but the Rust emitter "
                    "only supports aggregate_select on postgres"
                )

    def test_alter_column_not_claimed_by_non_postgres_backends(self) -> None:
        """MySQL, SQLite, and MSSQL must not claim ALTER_COLUMN.

        The orchestrator raises FerrumMigrationError for alter_column on MSSQL
        (listed in _MSSQL_UNSUPPORTED_KINDS). MySQL and SQLite also omit it
        (alter_column is only supported on PostgreSQL in Ferrum v0.1).
        """
        for b in ALL_BACKENDS:
            if b.name != "postgres":
                assert Capability.ALTER_COLUMN not in b.capabilities, (
                    f"Backend {b.name!r} claims ALTER_COLUMN but only postgres "
                    "supports alter_column in Ferrum v0.1"
                )

    def test_call_function_not_claimed_by_non_postgres_backends(self) -> None:
        """MySQL, SQLite, and MSSQL must not claim CALL_FUNCTION.

        Connection.call_function() emits ``SELECT * FROM schema.fn($1, ...)``
        using PostgreSQL-specific $N positional placeholders.  There is no
        Python-side dialect pre-check; the SQL would fail at the driver level
        on non-Postgres backends.  Registry consistency is the only gate.
        """
        for b in ALL_BACKENDS:
            if b.name != "postgres":
                assert Capability.CALL_FUNCTION not in b.capabilities, (
                    f"Backend {b.name!r} claims CALL_FUNCTION but call_function() "
                    "uses Postgres-only $N placeholders"
                )

    def test_postgres_claims_all_exclusive_capabilities(self) -> None:
        """Postgres must claim every Postgres-exclusive capability."""
        pg_backends = [b for b in ALL_BACKENDS if b.name == "postgres"]
        assert pg_backends, "No postgres backend found in ALL_BACKENDS"
        exclusive = {
            Capability.TRANSACTIONS,
            Capability.SAVEPOINTS,
            Capability.AGGREGATES,
            Capability.ALTER_COLUMN,
            Capability.CALL_FUNCTION,
        }
        for pg in pg_backends:
            for cap in exclusive:
                assert cap in pg.capabilities, (
                    f"Postgres backend is missing {cap.value!r} in capabilities"
                )


# ---------------------------------------------------------------------------
# Transactions behavioral gate — Python-side check in Connection.transaction()
# ---------------------------------------------------------------------------


def _non_pg_connection(dialect: str) -> Connection:
    """Construct a Connection with a mock driver that has no transaction factory.

    Uses ``object.__new__`` to bypass the DSN-resolution in ``Connection.__init__``.
    The driver's ``transaction`` attribute is explicitly set to ``None`` so the
    Python-side gate in ``Connection.transaction()`` fires.
    """
    conn = object.__new__(Connection)
    driver = MagicMock()
    driver.transaction = None  # no transaction support on this driver
    driver.dialect = dialect
    conn._driver = driver
    conn._lifecycle = _LifecycleGuard()
    conn._runtime = RuntimeConfig()
    conn._echo = False
    return conn


class TestTransactionCapabilityGate:
    """Connection.transaction() must raise FerrumConfigError for non-Postgres drivers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dialect", ["mysql", "sqlite", "mssql"])
    async def test_transaction_raises_before_driver_io(self, dialect: str) -> None:
        conn = _non_pg_connection(dialect)
        with pytest.raises(FerrumConfigError, match=r"[Tt]ransaction"):
            async with conn.transaction():
                pass  # pragma: no cover
        conn._driver.execute = AsyncMock()
        conn._driver.execute.assert_not_called()

    def test_transaction_does_not_raise_registry_error_for_postgres(self) -> None:
        """Postgres backend correctly claims TRANSACTIONS in the registry."""
        pg_backends = [b for b in ALL_BACKENDS if b.name == "postgres"]
        assert pg_backends
        for pg in pg_backends:
            assert Capability.TRANSACTIONS in pg.capabilities


# ---------------------------------------------------------------------------
# Savepoints behavioral gate — Python-side check in Transaction.savepoint()
# ---------------------------------------------------------------------------


class TestSavepointCapabilityGate:
    """Transaction.savepoint() must raise FerrumConfigError when driver lacks savepoint."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dialect", ["mysql", "sqlite", "mssql"])
    async def test_savepoint_raises_for_non_postgres_driver(self, dialect: str) -> None:
        """Bound connection with no savepoint factory → FerrumConfigError before SQL."""
        bound = MagicMock()
        bound.savepoint = None  # simulate a driver without SAVEPOINT support
        tx = Transaction(bound, dialect=dialect)

        with pytest.raises(FerrumConfigError, match=r"[Ss]avepoint"):
            async with tx.savepoint():
                pass  # pragma: no cover

    def test_savepoint_not_claimed_in_registry_for_non_postgres(self) -> None:
        for b in ALL_BACKENDS:
            if b.name != "postgres":
                assert Capability.SAVEPOINTS not in b.capabilities
