"""Driver protocol for async database I/O.

The Python connection layer owns all awaitable database I/O. QuerySet terminals
depend only on this minimal protocol, while SQL compilation remains in Rust.
PostgreSQL is the canonical backend; secondary drivers implement this shape for
thin parity and may intentionally omit higher-level features such as transactions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

_COMPILED_QUERY_TOKEN = object()


class CompiledQuery:
    """Opaque output of Ferrum's validated query compiler.

    The constructor is intentionally unavailable to application code. QuerySet
    creates instances through the private ``_compiled_query`` integration seam
    after native compilation and bound-parameter decoding.
    """

    __slots__ = ("bound_params", "sql_text")

    def __init__(
        self,
        sql_text: str,
        bound_params: tuple[object, ...],
        *,
        _token: object,
    ) -> None:
        if _token is not _COMPILED_QUERY_TOKEN:
            raise TypeError("CompiledQuery values are created by Ferrum's query compiler.")
        self.sql_text = sql_text
        self.bound_params = bound_params


def _compiled_query(sql_text: str, bound_params: list[object]) -> CompiledQuery:
    """Create the opaque execution value consumed by ConnectionLike streaming."""
    return CompiledQuery(sql_text, tuple(bound_params), _token=_COMPILED_QUERY_TOKEN)


@runtime_checkable
class ChunkStreamProtocol(Protocol):
    """Closeable, pull-facing stream of bounded row chunks."""

    def __aiter__(self) -> AsyncIterator[list[Any]]: ...
    async def __anext__(self) -> list[Any]: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class RowProtocol(Protocol):
    """Duck-typed row: mapping interface or dict-like access."""

    def keys(self) -> Any: ...
    def __getitem__(self, key: str) -> Any: ...


@runtime_checkable
class QueryExecutorProtocol(Protocol):
    """Minimal async SQL surface QuerySet terminals call on ``_require_driver()``.

    Implementations must execute already-compiled SQL with positional bound
    parameters. They must not perform SQL string construction from user input.
    """

    async def fetch(self, sql: str, *params: object) -> list[Any]: ...
    async def fetchrow(self, sql: str, *params: object) -> Any | None: ...
    async def fetchval(self, sql: str, *params: object) -> Any: ...
    async def execute(self, sql: str, *params: object) -> str: ...


@runtime_checkable
class DriverProtocol(Protocol):
    """Uniform async driver surface for QuerySet and migrations.

    Concrete drivers map native exceptions into Ferrum's sanitized error
    taxonomy before exceptions reach application code.
    """

    dialect: str  # "postgres" | "mysql" | "sqlite"

    async def fetch(self, sql: str, *params: object) -> list[Any]: ...
    async def fetchrow(self, sql: str, *params: object) -> Any | None: ...
    async def fetchval(self, sql: str, *params: object) -> Any: ...
    async def execute(self, sql: str, *params: object) -> str: ...
    async def open(self) -> None: ...
    async def close(self) -> None: ...


# A ``ConnectionInitializer`` consumes the Ferrum ``Connection`` surface only —
# it must never receive a raw driver/pool connection. ``ConnectionLike`` is a
# structural alias (duck-typed) so the protocol module does not import
# ``ferrum.connection`` (preserves the import boundary). Implementations read
# ``conn.dialect`` for backend validation; the canonical
# ``PgVectorInitializer`` reaches the asyncpg pool directly
# (``conn._driver._pool.execute``) to preserve the legacy
# ``register_vector_codecs`` behavior, while consumer-defined initializers
# may use ``conn._require_driver()``.
ConnectionLike = Any


@runtime_checkable
class ConnectionInitializer(Protocol):
    """Declarative hook that prepares every new pooled connection.

    Connection initializers run uniformly for current and future pooled
    connections. A driver/pool that supports initializers invokes
    ``initialize(conn)`` from its pool ``init`` callback so the registration
    survives pool growth, reconnect/failover, and ``expire_connections()``.

    Initializers must:

    - Consume the ``Connection`` surface for backend validation
      (``conn.dialect``). The canonical ``PgVectorInitializer`` reaches the
      asyncpg pool directly (``conn._driver._pool.execute``) to preserve the
      legacy ``register_vector_codecs`` behavior; consumer-defined
      initializers may use ``conn._require_driver()``. Never expose the raw
      asyncpg/aiomysql connection to consumer code.
    - Be idempotent: pool reconnection, concurrent startup paths, and consumer
      re-registration must not raise. ``DuplicateObjectError`` from
      ``CREATE EXTENSION`` and repeat codec registration are explicitly
      tolerated by the pgvector initializer.
    - Fail closed on permanent errors: if a required extension cannot be
      installed (e.g. the ``vector`` contrib is absent), propagate the
      exception. A pool that silently served queries against unregistered
      vector columns would produce non-deterministic ``DataError`` depending
      on which pooled connection served the query.

    Consumer-defined initializers (citext, custom codecs) implement this
    protocol. The pgvector initializer lives in
    :mod:`ferrum.ext.pgvector` (:class:`PgVectorInitializer`).

    ``name`` is a diagnostic label for logging and error messages; it is not
    enforced by ``@runtime_checkable`` (which only verifies method presence).
    """

    name: str

    async def initialize(self, conn: ConnectionLike) -> None: ...
