"""Optional PostgreSQL shard routing over independently configured pools.

Ratified by AGENTS.md §5a "Schema tenancy and sharding boundaries". This module
is OPTIONAL: QuerySet stays shard-unaware and connection-explicit, and most
applications never need it.

Binding contract (AGENTS.md §5a):

- A :class:`ConnectionRegistry` owns independently configured **PostgreSQL**
  pools (not a multi-database or dialect-switching Session). Non-PostgreSQL
  DSNs are rejected at registration time so the constraint is enforced
  structurally, not by convention.
- A :class:`ShardRouter` resolves a **trusted** shard key chosen by
  caller/router code and returns an explicit :class:`Connection` /
  :class:`Transaction`. The router never inspects model metadata, tenant ids,
  or schema names to pick a connection — the caller supplies the trusted key
  and the resolver function.
- There is no ``platform_scoped`` model flag, no implicit QuerySet connection
  selection, and no implicit multi-DB behavior. Access control stays at the
  transaction/session boundary (see :mod:`ferrum.session`).
- mysql/sqlite/mssql extras are out of scope; the router is PostgreSQL-only.

Lifecycle is bounded and parallel: ``start()`` opens all pools with a bounded
``asyncio.Semaphore``; ``close()`` drains and closes all pools in parallel
with the same bound; ``health_check()`` probes every pool concurrently.
Per-shard :class:`~ferrum.drivers.postgres.PoolStats` are available via
``stats()``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar
from urllib.parse import urlparse

from ferrum.connection import Connection, Transaction
from ferrum.errors import FerrumConfigError

if TYPE_CHECKING:
    from ssl import SSLContext

    from ferrum.drivers.postgres import PoolStats
    from ferrum.runtime import RetryPolicy

__all__ = ["ConnectionRegistry", "PoolConfig", "ShardRouter"]

ShardKeyT = TypeVar("ShardKeyT")

_POSTGRES_SCHEMES: frozenset[str] = frozenset({"postgresql", "postgres"})


def _ensure_postgres_dsn(dsn: str) -> None:
    """Raise FerrumConfigError if ``dsn`` is not a PostgreSQL DSN.

    The shard router is PostgreSQL-only by ratified contract; rejecting
    non-postgres schemes at registration time makes the constraint structural.
    """
    scheme = urlparse(dsn).scheme.lower()
    if scheme not in _POSTGRES_SCHEMES:
        raise FerrumConfigError(
            f"ConnectionRegistry is PostgreSQL-only; got DSN scheme {scheme!r}. "
            "mysql/sqlite/mssql pools are not supported by the shard router. "
            "[FERR-C001]",
            category="config",
        )


@dataclass(frozen=True)
class PoolConfig:
    """Configuration for one PostgreSQL pool in a :class:`ConnectionRegistry`.

    Mirrors the :class:`~ferrum.connection.Connection` constructor so each
    shard owns an independently sized, independently tuned PostgreSQL pool.
    """

    dsn: str
    min_size: int = 1
    max_size: int = 10
    acquire_timeout: float | None = None
    query_timeout: float | None = None
    statement_timeout: int | None = None
    max_lifetime: float | None = None
    max_idle_lifetime: float | None = None
    max_connection_age: float | None = None
    command_timeout: float | None = None
    statement_cache_size: int | None = None
    ssl: bool | str | SSLContext | None = None  # type: ignore[valid-type]
    server_settings: Mapping[str, str] | None = None
    application_name: str | None = None
    retry: RetryPolicy | None = None
    drain_timeout: float = 30.0
    echo: bool | str = False


class ConnectionRegistry:
    """Owns a set of independently configured PostgreSQL pools.

    The registry opens (``start()``), probes (``health_check()``), and closes
    (``close()``) every pool with bounded parallelism. It does NOT select a
    pool implicitly — callers (or a :class:`ShardRouter`) ask for a named pool
    via :meth:`get`. There is no dialect switching, no implicit multi-DB
    behavior, and no model-metadata-driven routing.

    Example::

        registry = ConnectionRegistry({
            "shard_0": PoolConfig(dsn=dsn0, max_size=20),
            "shard_1": PoolConfig(dsn=dsn1, max_size=20),
        })
        await registry.start()
        try:
            conn = registry.get("shard_0")
            async with conn.transaction() as tx:
                ...
        finally:
            await registry.close()
    """

    def __init__(
        self,
        configs: Mapping[str, PoolConfig],
        *,
        parallelism: int = 8,
    ) -> None:
        if parallelism < 1:
            raise FerrumConfigError(
                f"parallelism must be >= 1, got {parallelism}. [FERR-C001]",
                category="config",
            )
        if not configs:
            raise FerrumConfigError(
                "ConnectionRegistry requires at least one PoolConfig. [FERR-C001]",
                category="config",
            )
        for name, cfg in configs.items():
            if not isinstance(name, str) or not name:
                raise FerrumConfigError(
                    "Shard names must be non-empty strings. [FERR-C001]",
                    category="config",
                )
            _ensure_postgres_dsn(cfg.dsn)
        # Preserve insertion order for deterministic stats()/names().
        self._configs: dict[str, PoolConfig] = dict(configs)
        self._conns: dict[str, Connection] = {}
        self._parallelism = parallelism
        self._closed = False

    @property
    def names(self) -> frozenset[str]:
        """The set of registered shard names."""
        return frozenset(self._configs)

    def get(self, name: str) -> Connection:
        """Return the open :class:`Connection` for shard ``name``.

        Raises FerrumConfigError if ``name`` is not registered or the registry
        has not been started (or has been closed).
        """
        if self._closed:
            raise FerrumConfigError("ConnectionRegistry is closed. [FERR-C001]", category="config")
        if name not in self._configs:
            raise FerrumConfigError(
                f"Shard {name!r} is not registered. Known shards: "
                f"{', '.join(sorted(self._configs))}. [FERR-C001]",
                category="config",
            )
        conn = self._conns.get(name)
        if conn is None:
            raise FerrumConfigError(
                f"Shard {name!r} is registered but not started. "
                "Call await registry.start() first. [FERR-C001]",
                category="config",
            )
        return conn

    async def start(self) -> None:
        """Open all registered pools with bounded parallelism.

        If any pool fails to open, already-opened pools are closed and the
        original error is re-raised (no partial registry is left behind).
        """
        if self._conns:
            raise FerrumConfigError(
                "ConnectionRegistry already started. [FERR-C001]",
                category="config",
            )
        sem = asyncio.Semaphore(self._parallelism)
        opened: list[tuple[str, Connection]] = []

        async def _open_one(name: str) -> None:
            async with sem:
                cfg = self._configs[name]
                conn = Connection(
                    cfg.dsn,
                    min_size=cfg.min_size,
                    max_size=cfg.max_size,
                    acquire_timeout=cfg.acquire_timeout,
                    query_timeout=cfg.query_timeout,
                    statement_timeout=cfg.statement_timeout,
                    max_lifetime=cfg.max_lifetime,
                    max_idle_lifetime=cfg.max_idle_lifetime,
                    max_connection_age=cfg.max_connection_age,
                    command_timeout=cfg.command_timeout,
                    statement_cache_size=cfg.statement_cache_size,
                    ssl=cfg.ssl,
                    server_settings=dict(cfg.server_settings) if cfg.server_settings else None,
                    application_name=cfg.application_name,
                    retry=cfg.retry,
                    drain_timeout=cfg.drain_timeout,
                    echo=cfg.echo,
                )
                await conn.open()
                opened.append((name, conn))

        try:
            await asyncio.gather(*(_open_one(n) for n in self._configs))
        except BaseException:
            # Roll back any pools that already opened so we never leak.
            await asyncio.gather(*(c.close() for _, c in opened), return_exceptions=True)
            self._conns.clear()
            raise
        for name, conn in opened:
            self._conns[name] = conn

    async def health_check(self, *, timeout: float | None = 5.0) -> dict[str, bool]:
        """Probe every pool concurrently; return per-shard liveness.

        A pool that raises is reported as ``False`` rather than aborting the
        whole probe, so callers get a full health picture in one round-trip.
        """
        sem = asyncio.Semaphore(self._parallelism)
        results: dict[str, bool] = {}

        async def _probe(name: str) -> None:
            conn = self._conns.get(name)
            if conn is None:
                results[name] = False
                return
            async with sem:
                try:
                    await conn.health_check(timeout=timeout)
                    results[name] = True
                except Exception:
                    results[name] = False

        await asyncio.gather(*(_probe(n) for n in self._configs))
        return results

    async def close(self) -> None:
        """Drain and close every pool in parallel with bounded parallelism.

        Idempotent: a second call is a no-op. Exceptions from individual pool
        closes are aggregated so one failing pool does not mask others.
        """
        if self._closed:
            return
        self._closed = True
        conns = list(self._conns.items())
        self._conns.clear()
        if not conns:
            return
        sem = asyncio.Semaphore(self._parallelism)

        async def _close_one(_name: str, conn: Connection) -> None:
            async with sem:
                await conn.close()

        await asyncio.gather(*(_close_one(n, c) for n, c in conns), return_exceptions=True)

    def stats(self) -> dict[str, PoolStats | None]:
        """Return a per-shard :class:`~ferrum.drivers.postgres.PoolStats` snapshot.

        ``None`` for a shard whose driver does not expose pool stats or whose
        pool is not open.
        """
        return {name: conn.pool_stats() for name, conn in self._conns.items()}

    def items(self) -> list[tuple[str, Connection]]:
        """Return ``(name, Connection)`` pairs for every started shard, in registration order.

        Additive helper (W3-B) for callers that need to iterate every shard's
        open connection — e.g. building migration targets across all shards.
        Raises nothing; returns an empty list before ``start()`` or after
        ``close()``. The returned connections are the live pool handles; callers
        must not close them individually.
        """
        return [(name, conn) for name, conn in self._conns.items()]


class ShardRouter(Generic[ShardKeyT]):
    """Resolves a trusted shard key to an explicit Connection/Transaction.

    The router wraps a :class:`ConnectionRegistry` and a caller-supplied
    ``resolver`` that maps a trusted shard key to a registered shard name. It
    never inspects model metadata, tenant ids, or schema names to choose a
    connection — the caller supplies the trusted key, and the resolver is the
    single place where routing policy lives. QuerySet stays shard-unaware:
    the caller passes the returned :class:`Connection` / :class:`Transaction`
    to QuerySet terminals explicitly.

    Example::

        registry = ConnectionRegistry({"a": PoolConfig(dsn=dsn_a), ...})
        await registry.start()
        router = ShardRouter(registry, resolver=lambda tenant: "a" if tenant < "m" else "b")
        try:
            conn = router.connection_for("alice")
            async with router.transaction_for("alice") as tx:
                rows = await MyModel.objects.all(tx)
        finally:
            await registry.close()
    """

    def __init__(
        self,
        registry: ConnectionRegistry,
        resolver: Callable[[ShardKeyT], str],
    ) -> None:
        self._registry = registry
        self._resolver = resolver

    @property
    def registry(self) -> ConnectionRegistry:
        """The underlying :class:`ConnectionRegistry`."""
        return self._registry

    @property
    def names(self) -> frozenset[str]:
        """The registered shard names (delegated to the registry)."""
        return self._registry.names

    def connection_for(self, shard_key: ShardKeyT) -> Connection:
        """Resolve ``shard_key`` to a registered :class:`Connection`.

        The shard key is a trusted caller value; ``resolver`` maps it to a
        registered shard name. Raises FerrumConfigError if the resolver
        returns an unknown shard name.
        """
        name = self._resolver(shard_key)
        return self._registry.get(name)

    @contextlib.asynccontextmanager
    async def transaction_for(
        self,
        shard_key: ShardKeyT,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
        deadline: float | None = None,
    ) -> AsyncIterator[Transaction]:
        """Open a transaction on the shard resolved from ``shard_key``.

        The transaction runs on the resolved shard's pinned connection and
        commits/rolls back exactly like :meth:`Connection.transaction`. Use
        the yielded :class:`Transaction` explicitly with QuerySet terminals.
        """
        conn = self.connection_for(shard_key)
        async with conn.transaction(
            isolation=isolation,
            readonly=readonly,
            deferrable=deferrable,
            deadline=deadline,
        ) as tx:
            yield tx

    def stats(self) -> dict[str, PoolStats | None]:
        """Per-shard pool stats (delegated to the registry)."""
        return self._registry.stats()

    async def health_check(self, *, timeout: float | None = 5.0) -> dict[str, bool]:
        """Per-shard liveness (delegated to the registry)."""
        return await self._registry.health_check(timeout=timeout)
