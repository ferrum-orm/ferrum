"""FastAPI / Starlette lifespan helpers for Ferrum connection pool management.

Provides a ``ferrum_lifespan`` async context manager that opens and closes the
Ferrum connection pool in sync with the ASGI application lifespan.

Usage::

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from ferrum.contrib.fastapi import ferrum_lifespan

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with ferrum_lifespan(database_url=app.state.db_url):
            yield

    app = FastAPI(lifespan=lifespan)
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator


@contextlib.asynccontextmanager
async def ferrum_lifespan(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncGenerator[None, None]:
    """Async context manager that manages the Ferrum pool for an ASGI lifespan.

    The DSN is never logged (CRED-1). Opens the pool on enter; closes on exit.
    """
    from ferrum.connection import connect

    async with connect(database_url, min_size=min_size, max_size=max_size):
        yield
