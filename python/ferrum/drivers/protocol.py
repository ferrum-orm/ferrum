"""Driver protocol for async database I/O.

The Python connection layer owns all awaitable database I/O. QuerySet terminals
depend only on this minimal protocol, while SQL compilation remains in Rust.
PostgreSQL is the canonical backend; secondary drivers implement this shape for
thin parity and may intentionally omit higher-level features such as transactions.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


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
