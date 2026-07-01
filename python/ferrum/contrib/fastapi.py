"""FastAPI / Starlette lifespan helpers for Ferrum connection pool management.

Provides ``ferrum_lifespan`` (pool lifecycle) and ``get_ferrum_conn`` (route
dependency) for wiring Ferrum into a FastAPI application.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Protocol

from ferrum.connection import Connection


class _ASGIAppWithState(Protocol):
    state: object


class FerrumConnRequest(Protocol):
    """Structural type for ASGI requests passed to :func:`get_ferrum_conn`.

    Satisfied by Starlette/FastAPI ``Request`` and any object exposing
    ``app.state`` (where lifespan setup stores ``ferrum_conn``).
    """

    app: _ASGIAppWithState


@contextlib.asynccontextmanager
async def ferrum_lifespan(
    database_url: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncGenerator[Connection, None]:
    """Async context manager that manages the Ferrum pool for an ASGI lifespan.

    Yields an open :class:`~ferrum.connection.Connection`. Assign it to
    ``app.state.ferrum_conn`` during lifespan setup so handlers can inject it
    via :func:`get_ferrum_conn`.

    The DSN is never logged (CRED-1). When ``database_url`` is omitted, the
    same environment resolution as :func:`ferrum.connect` applies
    (``FERRUM_DATABASE_URL``, then ``DATABASE_URL``).

    Usage::

        from contextlib import asynccontextmanager
        from fastapi import FastAPI
        from ferrum.contrib.fastapi import ferrum_lifespan

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            async with ferrum_lifespan() as conn:
                app.state.ferrum_conn = conn
                yield

        app = FastAPI(lifespan=lifespan)
    """
    from ferrum.connection import connect

    async with connect(database_url, min_size=min_size, max_size=max_size) as conn:
        yield conn


async def get_ferrum_conn(request: FerrumConnRequest) -> Connection:
    """FastAPI dependency returning the pool opened during app lifespan."""
    conn = getattr(request.app.state, "ferrum_conn", None)
    if not isinstance(conn, Connection):
        raise RuntimeError(
            "Ferrum connection is not initialized. In the app lifespan, open "
            "ferrum_lifespan and set app.state.ferrum_conn = conn."
        )
    return conn
